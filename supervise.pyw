"""Keeps server.py running in the background. Start it at logon (e.g. a Task Scheduler task running venv/Scripts/pythonw.exe supervise.pyw).

Runs windowless (.pyw), allows only one copy of itself, and restarts the server if it ever exits,
backing off when it crashes repeatedly. Exit code 3 from the server means the HTTP port is already
taken by another instance, so the supervisor stands down instead of looping.
"""
import ctypes
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
PYTHON = Path(sys.executable).with_name("python.exe")  # console python, but launched with no window
sys.path.insert(0, str(HERE))
from config import CFG  # noqa: E402

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
    # one supervisor per user session
    ctypes.windll.kernel32.CreateMutexW(None, False, "Local\\meshtastic-dash-supervisor")
    if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        return
    _job = tie_children_to_self()  # noqa: F841 - must stay referenced

    backoff = 5
    while True:
        started = time.time()
        log("starting server")
        proc = subprocess.run(
            [str(PYTHON), "-u", str(HERE / "server.py")], cwd=HERE,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW,
            env={**os.environ, "LORAKEET_SUPERVISED": "1"},  # the server may exit to restart (setup page)
        )
        if proc.returncode == EXIT_PORT_IN_USE:
            log("port 5190 already in use by another instance; supervisor exiting")
            return
        ran = time.time() - started
        backoff = 5 if ran > 300 else min(backoff * 2, 300)  # crash loops back off to 5 min
        log(f"server exited with code {proc.returncode} after {ran:.0f} s; restarting in {backoff} s")
        time.sleep(backoff)


if __name__ == "__main__":
    main()
