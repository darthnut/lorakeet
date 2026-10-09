"""Keeps server.py running in the background. Start it at logon (e.g. a Task Scheduler task running venv/Scripts/pythonw.exe supervise.pyw).

Runs windowless (.pyw), allows only one copy of itself, and restarts the server if it ever exits,
backing off when it crashes repeatedly. Exit code 3 from the server means the HTTP port is already
taken by another instance, so the supervisor stands down instead of looping.

With pystray and Pillow installed it also shows the Lorakeet icon in the notification area (tray.py): status,
Open, Pause/Resume logging, Quit. LORAKEET_NO_TRAY=1 turns the icon off.
"""
import ctypes
import os
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
PYTHON = Path(sys.executable).with_name("python.exe")  # console python, but launched with no window
sys.path.insert(0, str(HERE))
try:
    from config import CFG  # noqa: E402
except Exception as e:  # noqa: BLE001 - a broken lorakeet.toml: there's no log folder to write to yet, so say it
    if sys.platform == "win32":
        ctypes.windll.user32.MessageBoxW(None, f"Lorakeet can't start: {e}\n\nFix lorakeet.toml (every option is "
                                         "explained in lorakeet.example.toml), then start Lorakeet again.", "Lorakeet", 0x10)
    raise SystemExit(f"lorakeet.toml: {e}") from None

LOG = CFG["storage"]["data_dir"] / "supervise.log"
EXIT_PORT_IN_USE = 3


def log(msg):
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}\n")


class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):  # noqa: N801 - Win32 name
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", ctypes.c_uint32), ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", ctypes.c_uint32),
                ("Affinity", ctypes.c_size_t), ("PriorityClass", ctypes.c_uint32),
                ("SchedulingClass", ctypes.c_uint32), ("IoInfo", ctypes.c_uint64 * 6),
                ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]


def tie_children_to_self():
    """Put this process in a kill-on-close job so the server dies with us.

    Stopping the scheduled task kills only the supervisor's process; without this the server
    (started via the venv launcher, which allows breakaway) is orphaned and keeps holding the serial port.
    """
    k32 = ctypes.windll.kernel32
    k32.CreateJobObjectW.restype = ctypes.c_void_p
    k32.GetCurrentProcess.restype = ctypes.c_void_p
    job = k32.CreateJobObjectW(None, None)
    info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    info.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    k32.SetInformationJobObject(ctypes.c_void_p(job), 9, ctypes.byref(info), ctypes.sizeof(info))
    if not k32.AssignProcessToJobObject(ctypes.c_void_p(job), ctypes.c_void_p(k32.GetCurrentProcess())):
        log(f"warning: could not create kill-on-close job (error {k32.GetLastError()})")
    return job  # keep the handle open for our lifetime; the OS closes it when we die


def main():
    # one supervisor per dashboard port in this user session (the server allows one Lorakeet per port too), so a
    # second install on another port (a test copy) runs alongside instead of quietly standing down
    port = CFG["http"]["port"]
    ctypes.windll.kernel32.CreateMutexW(None, False, f"Local\\lorakeet-supervisor-{port}")
    if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        log(f"another Lorakeet supervisor is already running for port {port}; this one is exiting")
        return
    _job = tie_children_to_self()  # noqa: F841 - must stay referenced

    quitting, restarting = threading.Event(), threading.Event()
    state = {"proc": None, "tray": None}

    def restart_server():
        """The tray's Restart: stop the server; serve() starts it again at once (it's not a crash)."""
        restarting.set()
        if state["proc"] and state["proc"].poll() is None:
            state["proc"].terminate()

    def stop_everything():
        quitting.set()
        if state["proc"] and state["proc"].poll() is None:
            state["proc"].terminate()  # the kill-on-close job takes any grandchildren when we exit

    def serve():
        backoff = 5
        while not quitting.is_set():
            started = time.time()
            log("starting server")
            state["proc"] = subprocess.Popen(
                [str(PYTHON), "-u", str(HERE / "server.py")], cwd=HERE,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW,
                env={**os.environ, "LORAKEET_SUPERVISED": "1"},  # the server may exit to restart (setup page)
            )
            code = state["proc"].wait()
            if quitting.is_set():
                log("quit from the tray icon")
                return
            if code == EXIT_PORT_IN_USE:
                log(f"port {port} already in use by another instance; supervisor exiting")
                quitting.set()  # the tray icon sees this and goes away too
                return
            ran = time.time() - started
            if restarting.is_set() or code == 0:  # asked for (tray, setup, Restart buttons): straight back, no back-off
                restarting.clear()
                backoff = 5
                log("restarting on request" if code != 0 else "server asked to be restarted")
                if quitting.wait(1):
                    return
                continue
            backoff = 5 if ran > 300 else min(backoff * 2, 300)  # crash loops back off to 5 min
            log(f"server exited with code {code} after {ran:.0f} s; restarting in {backoff} s")
            if quitting.wait(backoff):
                return

    import tray
    if tray.available() and not os.environ.get("LORAKEET_NO_TRAY"):
        state["tray"] = tray.Tray(port, on_quit=stop_everything, exit_when=quitting.is_set, on_restart=restart_server)
        server_loop = threading.Thread(target=serve, daemon=True, name="serve")
        server_loop.start()
        try:
            state["tray"].run()  # blocks until Quit (or the server stands down)
        except Exception as e:  # noqa: BLE001 - an icon problem must not stop the logging
            log(f"tray icon failed ({e!r}); logging carries on without it")
            while server_loop.is_alive():
                server_loop.join(1)
            return
        stop_everything()
    else:
        serve()


if __name__ == "__main__":
    main()
