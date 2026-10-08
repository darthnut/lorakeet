"""Lorakeet configuration: `lorakeet.toml`, with generic defaults for anything it doesn't set.

Looked up at $LORAKEET_CONFIG, else `lorakeet.toml` next to this file. See `lorakeet.example.toml` for
every option. The config file is per-install and git-ignored; nothing personal belongs in the code.
"""
import copy
import logging
import os
import sys
import tomllib
from pathlib import Path

log = logging.getLogger("lorakeet.config")
HERE = Path(__file__).resolve().parent


def default_data_dir():
    """Per-OS application data folder."""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "Lorakeet"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Lorakeet"
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "lorakeet"


DEFAULTS = {
    "radio": {
        "port": "",               # serial port, e.g. "COM7" or "/dev/ttyACM0"; empty = auto-detect
    },
    "storage": {
        "data_dir": "",           # empty = per-OS default (see default_data_dir)
        "db_warn_gb": 1.0,        # warn (banner + notification) when the database reaches this size
        "debug_log_days": 7,      # the radio's firmware debug log is kept this long
    },
    "logging": {
        "store_recipients": True,  # keep the recipient of directed packets overheard over the air
    },
    "backup": {
        "destinations": [],       # folders to copy nightly backups into; empty = no backups
        "time": "03:30",          # local time, daily
    },
    "http": {
        "port": 5190,
        "lan": "off",             # "off" = this computer only; "view" = others on the LAN get read-only access
    },
    "map": {
        "center": [],             # [lat, lon] used before any node has shared a position; empty = world view
        "zoom": 10,
        "tiles": "osm",           # "osm": OpenStreetMap-based (CARTO, OpenTopoMap); "esri": Esri's (their terms apply;
                                  # the only choice with satellite imagery)
    },
    "base": {
        "id": "",                 # a node to feature as the base station (e.g. "!1234abcd"); empty = none
        "name": "",               # name to show until that node announces its own
    },
    "alerts": {
        "auto_traceroute": False,  # default for NEW installs only (transmits); stored settings win after that
    },
    "station": {
        "name": "",               # what to call this listening station (default: its radio's name)
        "location": [],           # [lat, lon] or [lat, lon, altitude_m] of the ANTENNA; empty = not set
        "note": "",               # e.g. "attic window, facing north"
        "mobile": False,          # true = this station moves (a car): its location comes from its radio's GPS
                                  # fixes, logged over time, never from station.location
    },
    "sync": {
        "mode": "off",            # "off"; "collector" = send what this station logs to a hub; "hub" = accept them
        "hub_url": "",            # collector: the hub's address, e.g. "http://home-pc:5190" (over Tailscale)
        "token": "",              # shared secret, the same on both ends (16+ characters); keep it out of git
        "interval_s": 60,         # collector: seconds between sync passes
        "allow": ["100.64.0.0/10", "fd7a:115c:a1e0::/48"],  # hub: networks collectors may send from (Tailscale)
        "station_tokens": [],     # hub: per-station secrets, "!stationid:token"; a station listed here must use its own
        "require_station_tokens": False,  # hub: refuse the shared token entirely (once every station has its own)
    },
}


def _merge(defaults, user, path=""):
    out = copy.deepcopy(defaults)
    for k, v in user.items():
        where = f"{path}{k}"
        if k not in defaults:
            log.warning("lorakeet.toml: unknown setting '%s' (ignored)", where)
            continue
        if isinstance(defaults[k], dict):
            if not isinstance(v, dict):
                raise ValueError(f"lorakeet.toml: [{where}] must be a table")
            out[k] = _merge(defaults[k], v, where + ".")
        else:
            want = type(defaults[k])
            if want is float and isinstance(v, int) and not isinstance(v, bool):
                v = float(v)
            if not isinstance(v, want) or (want is int and isinstance(v, bool)):
                raise ValueError(f"lorakeet.toml: {where} must be {want.__name__}, got {type(v).__name__}")
            out[k] = v
    return out


def _validate(c):
    if c["http"]["lan"] not in ("off", "view"):
        raise ValueError('lorakeet.toml: http.lan must be "off" or "view"')
    h, _, m = c["backup"]["time"].partition(":")
    if not (h.isdigit() and m.isdigit() and 0 <= int(h) < 24 and 0 <= int(m) < 60):
        raise ValueError('lorakeet.toml: backup.time must be "HH:MM"')
    center = c["map"]["center"]
    if center and (len(center) != 2 or not all(isinstance(x, (int, float)) for x in center)):
        raise ValueError("lorakeet.toml: map.center must be [lat, lon]")
    if c["map"]["tiles"] not in ("osm", "esri"):
        raise ValueError('lorakeet.toml: map.tiles must be "osm" or "esri"')
    loc = c["station"]["location"]
    if loc and (len(loc) not in (2, 3) or not all(isinstance(x, (int, float)) for x in loc)
                or not (-90 <= loc[0] <= 90 and -180 <= loc[1] <= 180)):
        raise ValueError("lorakeet.toml: station.location must be [lat, lon] or [lat, lon, altitude_m]")
    if c["station"]["mobile"] and loc:
        raise ValueError("lorakeet.toml: a mobile station takes its location from GPS; remove station.location")
    s = c["sync"]
    if s["mode"] not in ("off", "collector", "hub"):
        raise ValueError('lorakeet.toml: sync.mode must be "off", "collector" or "hub"')
    if s["mode"] == "collector" and len(s["token"]) < 16:
        raise ValueError("lorakeet.toml: sync.token must be at least 16 characters (the same on both ends)")
    if s["mode"] == "hub" and len(s["token"]) < 16 and not s["station_tokens"]:
        raise ValueError("lorakeet.toml: a hub needs sync.token (16+ characters) or sync.station_tokens")
    if s["token"] and len(s["token"]) < 16:
        raise ValueError("lorakeet.toml: sync.token must be at least 16 characters")
    for entry in s["station_tokens"]:
        sid, _, tok = str(entry).partition(":")
        if not (len(sid) == 9 and sid.startswith("!")) or len(tok) < 16:
            raise ValueError('lorakeet.toml: sync.station_tokens entries look like "!1234abcd:<16+ character token>"')
    if s["mode"] == "collector" and not s["hub_url"].startswith(("http://", "https://")):
        raise ValueError('lorakeet.toml: sync.hub_url must be like "http://home-pc:5190"')
    if s["mode"] == "hub":
        import ipaddress
        for n in s["allow"]:
            try:
                ipaddress.ip_network(n)
            except ValueError:
                raise ValueError(f"lorakeet.toml: sync.allow: {n!r} is not a network") from None
    bid = c["base"]["id"]
    if bid and not (len(bid) == 9 and bid.startswith("!")):
        raise ValueError('lorakeet.toml: base.id must look like "!1234abcd"')


def load():
    path = Path(os.environ.get("LORAKEET_CONFIG") or HERE / "lorakeet.toml")
    user = {}
    if path.is_file():
        with open(path, "rb") as f:
            user = tomllib.load(f)
    c = _merge(DEFAULTS, user)
    _validate(c)
    c["storage"]["data_dir"] = Path(c["storage"]["data_dir"]) if c["storage"]["data_dir"] else default_data_dir()
    c["_path"] = str(path) if path.is_file() else None
    return c


CFG = load()


def public():
    """The subset the web pages may see."""
    return {"map": CFG["map"], "base": {"id": CFG["base"]["id"], "name": CFG["base"]["name"]},
            "lan": CFG["http"]["lan"]}
