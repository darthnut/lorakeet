"""Process-wide state other modules read at call time: paths, the version, the data folder (the demo moves it),
the demo's radio id and the featured base station (which follows a firmware 2.8 renumbering).
"""
import logging
import os
import threading
import time
from pathlib import Path

from config import CFG
import nodeids


log = logging.getLogger("meshdash")
EXIT_HOOKS = []  # called by exit_now() before the process ends: Store.flush writes rows still waiting to be committed


def exit_now(code=0):
    """End the process now (the restart paths, the demo's idle exit, SIGTERM) after flushing pending writes.
    os._exit skips atexit handlers and finally blocks, so anything held back must be written here first."""
    for hook in list(EXIT_HOOKS):
        try:
            hook()
        except Exception:  # noqa: BLE001 - exiting anyway
            log.exception("exit hook failed")
    os._exit(code)


HERE = Path(__file__).resolve().parent
STATIC = HERE / "static"
DATA_DIR = CFG["storage"]["data_dir"]
VERSION = (HERE / "VERSION").read_text(encoding="utf-8").strip()
RADIO_BACKUPS = DATA_DIR / "radio-backups"  # the Radio page's settings backups: private keys, this user only
RADIO_LOCK = threading.Lock()  # one change to a radio at a time
DEMO = None          # --demo: the made-up mesh's own radio id (demo.py); no radio, its own data folder and port
DEMO_IDLE_S = 3 * 3600  # a demo nobody has looked at for this long exits
LAST_REQUEST = [time.time()]

# Optional featured base station (lorakeet.toml [base]); empty = none.
BASE_ID = CFG["base"]["id"]
BASE_NAME = CFG["base"]["name"] or BASE_ID
BASE_BYTE = int(BASE_ID[-2:], 16) if BASE_ID else None   # the 1-byte relay ID it stamps on relayed packets


def follow_base():
    """[base] id follows a 2.8 renumbering of the base station (nodeids.py): its new number, and the 1-byte relay
    ID it now stamps on what it relays. Run at start and from the radio loop."""
    global BASE_ID, BASE_BYTE, BASE_NAME
    new = nodeids.info(DATA_DIR / "mesh.db")["aliases"].get(BASE_ID) if BASE_ID else None
    if new and new != BASE_ID:
        log.info("base station %s was renumbered by firmware 2.8: following it as %s", BASE_ID, new)
        if BASE_NAME == BASE_ID:
            BASE_NAME = new
        BASE_ID, BASE_BYTE = new, int(new[-2:], 16)
