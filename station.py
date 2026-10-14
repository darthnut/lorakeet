"""This listening station: its health and software fingerprint, the report it sends to a hub, its location
(configured or overridden), and the Radio page's station settings.
"""
import json
import logging
import os
import sys
import threading
import time
from pathlib import Path

import analytics
from channels import channel_fingerprints
from config import CFG
import radio_setup
import remote as remote_mod
import runtime


log = logging.getLogger("meshdash")


CLOCK_MAX_SKEW_S = 60  # a mobile Pi with no network sets its clock from the radio's GPS time past this


# ---- station reports: what this listening station tells the hub about itself (sync.py), and shows locally

def _software_version():
    """Short fingerprint of this install's code, so the hub can spot stations running different versions."""
    import hashlib
    h = hashlib.sha256()
    for f in sorted(Path(__file__).parent.glob("*.py")):
        h.update(f.read_bytes().replace(bytes([13, 10]), bytes([10])))  # CRLF -> LF: Windows checkouts must match
    return h.hexdigest()[:8]


SOFTWARE = _software_version()


def _uptime_s():
    try:
        if sys.platform == "win32":
            import ctypes
            return ctypes.windll.kernel32.GetTickCount64() / 1000
        return float(Path("/proc/uptime").read_text().split()[0])
    except Exception:  # noqa: BLE001
        return None


def _pi_health():
    """Raspberry Pi power flags and SoC temperature, when available (None elsewhere)."""
    import shutil
    import subprocess
    out = {}
    if shutil.which("vcgencmd"):
        try:
            r = subprocess.run(["vcgencmd", "get_throttled"], capture_output=True, text=True, timeout=5)
            out["throttled"] = r.stdout.strip().split("=")[-1]
        except Exception:  # noqa: BLE001
            pass
    try:
        out["tempC"] = round(int(Path("/sys/class/thermal/thermal_zone0/temp").read_text()) / 1000, 1)
    except Exception:  # noqa: BLE001
        pass
    return out


def power_flags(throttled):
    """A Raspberry Pi's vcgencmd get_throttled bits in words (None when unreadable)."""
    try:
        v = int(throttled, 16)
    except (TypeError, ValueError):
        return {}
    return {"underVoltageNow": bool(v & 0x1), "throttledNow": bool(v & 0x6), "tooHotNow": bool(v & 0x8),
            "underVoltageSinceBoot": bool(v & 0x10000), "throttledSinceBoot": bool(v & 0x60000)}


class HealthWatch:
    """A station's own power and network as events, written when they change: "power" (a Pi's throttled flags:
    under-voltage now / since boot) every POWER_S, "network" (the active connection, or "no network") every NET_S.
    Recorded on the station itself, so the timeline is exact even while it's offline, and syncs like any row.
    Its own thread: the radio thread can sit in a 45 s connect attempt. Linux stations only (vcgencmd, nmcli)."""
    POWER_S, NET_S = 10, 30

    def __init__(self, event, power=lambda: _pi_health().get("throttled"), network=lambda: remote_mod.network()):
        self.event, self.power, self.network = event, power, network
        self.last_p = self.last_n = None
        self.next_net = 0

    def start(self):
        threading.Thread(target=self._run, daemon=True, name="health").start()

    def step(self, now):
        p = self.power()
        if p is not None and p != self.last_p:
            self.event("power", throttled=p, prev=self.last_p, **power_flags(p))
            self.last_p = p
        if now >= self.next_net:
            self.next_net = now + self.NET_S
            n = self.network()
            if n is not None and n != self.last_n:
                self.event("network", network=n, prev=self.last_n)
                self.last_n = n

    def _run(self):
        while True:
            try:
                self.step(time.time())
            except Exception:  # noqa: BLE001 - a health probe must never stop
                log.exception("health watch error")
            time.sleep(self.POWER_S)


def station_report(mesh):
    import platform
    import shutil
    if runtime.DEMO:  # made up, like the rest of the demo: never this computer's platform, uptime or disk
        return {"name": "Demo Home", "version": runtime.VERSION, "platform": "demo", "radioHw": "HELTEC_V3",
                "radioFirmware": "2.7.26", "uptimeS": 3 * 86400, "backlog": 0}
    md = getattr(mesh.iface, "metadata", None) if mesh.iface is not None else None
    loc = CFG["station"]["location"]
    rep = {"name": CFG["station"]["name"] or (mesh.name(mesh.local_id) if mesh.local_id else ""),
           "note": CFG["station"]["note"], "software": SOFTWARE, "version": runtime.VERSION, "platform": f"{sys.platform} {platform.machine()}",
           "uptimeS": _uptime_s(), "diskFreeMB": round(shutil.disk_usage(runtime.DATA_DIR).free / 1048576),
           "radioFirmware": getattr(md, "firmware_version", None) or None,
           "radioHw": mesh.describe(mesh.local_id).get("hw") if mesh.local_id else None, **_pi_health(),
           **channel_fingerprints(mesh.store)}
    if loc:
        rep["location"] = [float(x) for x in loc]
    if CFG["station"]["mobile"]:
        rep["mobile"] = True
    return {k: v for k, v in rep.items() if v not in (None, "")}


def set_clock_from_gps(gps_ts, event):
    """A Raspberry Pi has no battery-backed clock: booted with no network (in a car), it runs from the last
    shutdown time and every row it logs is stamped wrong. If NTP hasn't synced and the radio's GPS time
    disagrees by more than CLOCK_MAX_SKEW_S, set the system clock from it (sudo date; the station user
    has passwordless sudo on our Pis). Linux only; never touches a clock NTP is keeping."""
    if not sys.platform.startswith("linux") or gps_ts < 1.7e9:
        return
    import subprocess
    try:
        synced = subprocess.run(["timedatectl", "show", "-p", "NTPSynchronized", "--value"],
                                capture_output=True, text=True, timeout=5).stdout.strip() == "yes"
    except Exception:  # noqa: BLE001
        synced = False
    skew = gps_ts - time.time()
    if synced or abs(skew) < CLOCK_MAX_SKEW_S:
        return
    r = subprocess.run(["sudo", "-n", "date", "-u", "-s", f"@{int(gps_ts)}"], capture_output=True, text=True, timeout=10)
    if r.returncode == 0:
        log.warning("system clock was %+.0f s off and NTP isn't synced: set from the radio's GPS", skew)
        event("clock_set_from_gps", skew_s=round(skew))
    else:
        log.warning("system clock is %+.0f s off but couldn't be set: %s", skew, r.stderr.strip()[:200])


def station_overrides(store):
    """The hub's own name / location for a station (the Stations page), {station: {name?, location?}}; these win
    over what the station reports about itself."""
    r = store.query("SELECT value FROM settings WHERE key='station_overrides'")
    return json.loads(r[0]["value"]) if r else {}


def _as_location(loc):
    return tuple(float(x) for x in loc) + ((None,) if len(loc) == 2 else ())


def load_station_locations(store, local_id):
    """analytics.STATION_LOCATIONS from this station's config, the collectors' latest reports and the hub's overrides."""
    r = store.query("SELECT value FROM settings WHERE key='station_meta'")
    for sid, m in (json.loads(r[0]["value"]) if r else {}).items():
        if m.get("location"):
            analytics.STATION_LOCATIONS[sid] = _as_location(m["location"])
    for sid, o in station_overrides(store).items():
        if o.get("location"):
            analytics.STATION_LOCATIONS[sid] = _as_location(o["location"])
    if local_id and CFG["station"]["location"]:
        loc = CFG["station"]["location"]
        analytics.STATION_LOCATIONS[local_id] = tuple(float(x) for x in loc) + ((None,) if len(loc) == 2 else ())


def supervised_now():
    """Something will start us again if we exit: the Windows runner (supervise.pyw) or systemd."""
    return bool(os.environ.get("LORAKEET_SUPERVISED") or os.environ.get("INVOCATION_ID"))


def heard_points(mesh):
    """Positions of the radios this station knows (not its own), for the location sanity check."""
    return [(p.get("lat"), p.get("lon")) for nid, p in list(mesh.pos.items()) if nid != mesh.local_id]


def station_settings(mesh):
    """The Radio page's "This station": what lorakeet.toml says now (re-read, so a saved change shows before the
    restart that applies it), the radio's own GPS fix, and whether the location looks wrong."""
    import config as cfgmod
    try:
        st = cfgmod.load()["station"]
    except Exception:  # noqa: BLE001 - a broken file: show what's running
        st = CFG["station"]
    loc = list(st["location"] or [])
    running = CFG["station"]
    return {"name": st["name"], "location": loc, "mobile": st["mobile"], "configPath": str(cfgmod.config_path()),
            "pendingRestart": (st["name"], loc) != (running["name"], list(running["location"] or [])),
            "supervised": supervised_now(), "radioFix": mesh.own_fix_now() if mesh.connected else None,
            "radioName": mesh.name(mesh.local_id) if mesh.local_id else None,
            "check": radio_setup.location_check(loc, heard_points(mesh)) if loc else None,
            "roughM": radio_setup.GEO_ROUGH_M}
