"""Open Lorakeet: what the desktop and Start-menu shortcuts run.

If Lorakeet is already running, this just opens it in the browser. If not, it starts it in the background first
(on Windows through supervise.pyw, which restarts it if it stops; elsewhere server.py directly), waits until it
answers, then opens the browser. Starting it twice is harmless: a second Lorakeet notices the first and exits.

    pythonw lorakeet.pyw            # what the shortcut does
    python lorakeet.pyw --no-browser   # start it (if needed) without opening a browser, e.g. for tests

The notification-area (tray) icon is tray.py, run by supervise.pyw.
"""
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
WAIT_S = 90  # first start on a slow machine: imports, database, radio search


def running(url):
    try:
        with urllib.request.urlopen(url + "/api/version", timeout=2) as r:
            return r.status == 200
    except Exception:  # noqa: BLE001
        return False


def start():
    if sys.platform == "win32":
        pyw = Path(sys.executable).with_name("pythonw.exe")
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        subprocess.Popen([str(pyw if pyw.exists() else sys.executable), str(HERE / "supervise.pyw")], cwd=HERE,
                         creationflags=flags, close_fds=True)
    else:
        log = open(HERE / "lorakeet-launch.log", "ab")  # noqa: SIM115 - handed to the child
        subprocess.Popen([sys.executable, str(HERE / "server.py")], cwd=HERE, stdout=log, stderr=log,
                         stdin=subprocess.DEVNULL, start_new_session=True)


def tell(msg):
    """A message box on Windows (no console under pythonw), else stderr."""
    if sys.platform == "win32":
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, msg, "Lorakeet", 0x30)
    else:
        print(msg, file=sys.stderr)


def main():
    try:
        from config import CFG
    except Exception as e:  # noqa: BLE001 - a broken lorakeet.toml: say so now, not after a 90 s wait
        tell(f"Lorakeet can't start: {e}\n\nFix lorakeet.toml (every option is explained in lorakeet.example.toml), "
             "then open Lorakeet again.")
        return 1
    url = f"http://127.0.0.1:{CFG['http']['port']}"
    if not running(url):
        start()
        t0 = time.time()
        while not running(url):
            if time.time() - t0 > WAIT_S:
                d = CFG["storage"]["data_dir"]
                tell(f"Lorakeet didn't start within {WAIT_S} seconds.\n\nIts logs may say why:\n"
                     f"{d / 'server.log'}\n{d / 'supervise.log'}")
                return 1
            time.sleep(1)
    if "--no-browser" not in sys.argv:
        webbrowser.open(url)
    return 0


if __name__ == "__main__":
    sys.exit(main())
