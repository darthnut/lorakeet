"""The Lorakeet icon in the Windows notification area (system tray), run by supervise.pyw.

It shows that Lorakeet is running and what it's doing, and gives three things to do:
- **Open Lorakeet** (also a click on the icon): the dashboard in the default browser.
- **Pause / Resume logging**: Lorakeet lets go of the radio, so its USB port is free for the Meshtastic app, the
  CLI or a flasher. The paused time is a gap in the log. The icon turns grey while paused.
- **Restart Lorakeet**: stops the server and starts it again at once (reconnects the radio, rereads settings), even
  when it isn't answering.
- **Quit Lorakeet**: after a confirmation, stops the server and the supervisor (until the shortcut, or the next
  sign-in with autostart, starts it again).

The menu's first line is a status read from the server every few seconds (`GET /api/brief`). Needs pystray and
Pillow (requirements.txt, Windows only); without them supervise.pyw runs exactly as before, just without an icon.
"""
import json
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

ICON = Path(__file__).resolve().parent / "static" / "icon-192.png"
POLL_S = 5


def status_text(brief):
    """The menu's status line and the icon's tooltip from /api/brief (None = the server isn't answering)."""
    if brief is None:
        return "Lorakeet is starting…"
    if brief.get("paused"):
        return "Logging paused: the radio is free for other programs"
    if brief.get("connected"):
        who = brief.get("name") or brief.get("id") or "the radio"
        return f"Logging from {who}" + (f" on {brief['port']}" if brief.get("port") else "")
    return "Waiting for a radio (plug one in over USB)"


def icon_image(paused=False, size=64):
    """The mesh-bird icon; grey and faded while paused, so the state shows at a glance."""
    from PIL import Image, ImageEnhance
    img = Image.open(ICON).convert("RGBA").resize((size, size), Image.LANCZOS)
    if paused:
        alpha = img.getchannel("A")
        img = ImageEnhance.Brightness(img.convert("L").convert("RGBA")).enhance(0.8)
        img.putalpha(alpha.point(lambda a: a * 0.65))
    return img


class Tray:
    def __init__(self, port, on_quit, exit_when=None, on_restart=None):
        self.url = f"http://127.0.0.1:{port}"
        self.on_quit = on_quit
        self.on_restart = on_restart
        self.exit_when = exit_when or (lambda: False)  # the supervisor stood down: the icon goes too
        self.brief = None
        self.icon = None
        self._stop = threading.Event()

    # -- talking to the server
    def _get(self):
        try:
            with urllib.request.urlopen(self.url + "/api/brief", timeout=3) as r:
                return json.loads(r.read())
        except Exception:  # noqa: BLE001 - starting, restarting, or stopped
            return None

    def _set_paused(self, paused):
        req = urllib.request.Request(self.url + "/api/logging", data=json.dumps({"paused": paused}).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        try:
            urllib.request.urlopen(req, timeout=5).read()
        except Exception:  # noqa: BLE001
            pass
        self.refresh()

    def refresh(self):
        b = self._get()
        changed = (b or {}).get("paused") != (self.brief or {}).get("paused")
        self.brief = b
        if self.icon is not None:
            self.icon.title = "Lorakeet: " + status_text(b)
            if changed:
                self.icon.icon = icon_image(bool((b or {}).get("paused")))
            self.icon.update_menu()

    def _poll(self):
        while not self._stop.wait(1):
            if self.exit_when():
                self.icon.stop()
                return
            if int(time.monotonic()) % POLL_S == 0:
                self.refresh()

    # -- menu actions
    def open(self, *_):
        webbrowser.open(self.url)

    def toggle_pause(self, *_):
        self._set_paused(not (self.brief or {}).get("paused"))

    def restart(self, *_):
        self.brief = None  # "starting…" until it answers again
        self.on_restart()
        if self.icon is not None:
            self.icon.title = "Lorakeet: restarting…"
            self.icon.update_menu()

    def quit(self, *_):
        import ctypes
        MB_YESNO, MB_ICONQUESTION, IDYES = 0x4, 0x20, 6
        if ctypes.windll.user32.MessageBoxW(None, "Quit Lorakeet?\n\nLogging stops until you open Lorakeet again.",
                                            "Lorakeet", MB_YESNO | MB_ICONQUESTION) != IDYES:
            return
        self._stop.set()
        self.on_quit()
        self.icon.stop()

    def menu(self):
        import pystray
        item = pystray.MenuItem
        return pystray.Menu(
            item(lambda _: status_text(self.brief), None, enabled=False),
            pystray.Menu.SEPARATOR,
            item("Open Lorakeet", self.open, default=True),
            item(lambda _: "Resume logging" if (self.brief or {}).get("paused") else "Pause logging",
                 self.toggle_pause, enabled=lambda _: self.brief is not None),
            pystray.Menu.SEPARATOR,
            *([item("Restart Lorakeet", self.restart)] if self.on_restart else []),
            item("Quit Lorakeet", self.quit),
        )

    def run(self):
        """Blocks until Quit (run it on the main thread)."""
        import pystray
        self.brief = self._get()
        self.icon = pystray.Icon("Lorakeet", icon_image(bool((self.brief or {}).get("paused"))),
                                 "Lorakeet: " + status_text(self.brief), self.menu())
        threading.Thread(target=self._poll, daemon=True, name="tray-poll").start()
        self.icon.run()


def available():
    try:
        import PIL  # noqa: F401
        import pystray  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False
