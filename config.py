"""Lorakeet configuration: `lorakeet.toml`, with generic defaults for anything it doesn't set.

Looked up at $LORAKEET_CONFIG, else `lorakeet.toml` next to this file. See `lorakeet.example.toml` for
every option. The config file is per-install and git-ignored; nothing personal belongs in the code.
"""
import copy
import json
import logging
import os
import re
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
        "host": "",               # a radio on the network instead (Wi-Fi / Ethernet): IP or name, e.g.
                                  # "192.168.1.50" or "meshtastic.local"; set = no USB auto-detect
        "tcp_port": 4403,         # the radio's API port (Meshtastic's default)
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
        "hostnames": [],          # names the dashboard may be opened by besides IP addresses and localhost
                                  # (e.g. "desk-pc", "desk-pc.tailnet.ts.net"); anything else is refused (DNS rebinding)
        "lan": "off",             # "off" = this computer only; "view" = others on the LAN get read-only access
                                  # (full after logging in, if a password is set); "login" = nothing until they log in
    },
    "map": {
        "center": [],             # [lat, lon] used before any node has shared a position; empty = world view
        "zoom": 10,
        "tiles": "osm",           # "osm": OpenStreetMap standard tiles + OpenTopoMap; "esri": Esri's (their terms
                                  # apply; the only choice with satellite imagery)
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
        "drive_pings": False,     # mobile only, TRANSMITS: a short ping on a private channel every
        "drive_ping_m": 1000,     #   drive_ping_m metres moved (never while parked), at most once per
        "drive_ping_min_s": 60,   #   drive_ping_min_s, so the Coverage view can map where the mesh heard us
        "drive_ping_channel": "Lorakeet",  # by name; a channel with a public key is refused
    },
    "remote": {
        "enabled": False,         # answer "lk ..." commands sent to this station's radio by direct message (remote.py)
        "allow": [],              # radios that may send them, e.g. ["!1234abcd"]: PKI direct messages only
    },
    "mcp": {                      # mcp_server.py: what an LLM connected to Lorakeet may do besides reading
        "allow_changes": False,   # watch radios, pause/resume logging, alert settings, mark alerts read, back up, restart
        "allow_transmit": False,  # send text messages and run traceroutes from this radio (they go out on the mesh)
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
    if not (isinstance(c["http"]["hostnames"], list) and all(isinstance(h, str) and h for h in c["http"]["hostnames"])):
        raise ValueError('lorakeet.toml: http.hostnames must be a list of names, e.g. ["desk-pc"]')
    if c["http"]["lan"] not in ("off", "view", "login"):
        raise ValueError('lorakeet.toml: http.lan must be "off", "view" or "login"')
    h, _, m = c["backup"]["time"].partition(":")
    if not (h.isdigit() and m.isdigit() and 0 <= int(h) < 24 and 0 <= int(m) < 60):
        raise ValueError('lorakeet.toml: backup.time must be "HH:MM"')
    center = c["map"]["center"]
    if center and (len(center) != 2 or not all(isinstance(x, (int, float)) for x in center)):
        raise ValueError("lorakeet.toml: map.center must be [lat, lon]")
    r = c["radio"]
    if r["host"] and r["port"]:
        raise ValueError("lorakeet.toml: set radio.port (USB) or radio.host (network), not both")
    if not (isinstance(r["tcp_port"], int) and 0 < r["tcp_port"] < 65536):
        raise ValueError("lorakeet.toml: radio.tcp_port must be a port number")
    if c["map"]["tiles"] not in ("osm", "esri"):
        raise ValueError('lorakeet.toml: map.tiles must be "osm" or "esri"')
    loc = c["station"]["location"]
    if loc and (len(loc) not in (2, 3) or not all(isinstance(x, (int, float)) for x in loc)
                or not (-90 <= loc[0] <= 90 and -180 <= loc[1] <= 180)):
        raise ValueError("lorakeet.toml: station.location must be [lat, lon] or [lat, lon, altitude_m]")
    st = c["station"]
    if st["drive_pings"] and not st["mobile"]:
        raise ValueError("lorakeet.toml: station.drive_pings needs station.mobile = true")
    if st["drive_ping_m"] < 200 or st["drive_ping_min_s"] < 30:
        raise ValueError("lorakeet.toml: drive pings at least 200 m and 30 s apart (the channel is shared airtime)")
    if c["station"]["mobile"] and loc:
        raise ValueError("lorakeet.toml: a mobile station takes its location from GPS; remove station.location")
    s = c["sync"]
    if s["mode"] not in ("off", "collector", "hub"):
        raise ValueError('lorakeet.toml: sync.mode must be "off", "collector" or "hub"')
    if s["mode"] == "collector" and len(s["token"]) < 16:
        raise ValueError("lorakeet.toml: sync.token must be at least 16 characters (the same on both ends)")
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
    for nid in c["remote"]["allow"]:
        if not (isinstance(nid, str) and len(nid) == 9 and nid.startswith("!")):
            raise ValueError('lorakeet.toml: remote.allow entries look like "!1234abcd"')
    if c["remote"]["enabled"] and not c["remote"]["allow"]:
        raise ValueError("lorakeet.toml: remote.enabled needs at least one radio in remote.allow")
    bid = c["base"]["id"]
    if bid and not (len(bid) == 9 and bid.startswith("!")):
        raise ValueError('lorakeet.toml: base.id must look like "!1234abcd"')


def load():
    path = Path(os.environ.get("LORAKEET_CONFIG") or HERE / "lorakeet.toml")
    user = {}
    if path.is_file():
        # utf-8-sig: Windows tools (PowerShell's Set-Content, older Notepad) start the file with a byte-order mark,
        # which tomllib rejects as "Invalid statement" at line 1
        user = tomllib.loads(path.read_text(encoding="utf-8-sig"))
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


# ---- first-run setup (server.py /api/setup, static/setup.html) ----

def config_path():
    """Where lorakeet.toml is, or will be written: $LORAKEET_CONFIG, else next to server.py."""
    return Path(os.environ.get("LORAKEET_CONFIG") or HERE / "lorakeet.toml")


def _toml(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(_toml(x) for x in v) + "]"
    return json.dumps(str(v), ensure_ascii=False)  # a JSON string is a valid TOML basic string


SECTION_NOTES = {
    "station": "# This listening station: a name, and the antenna's position [lat, lon] for the maps.",
    "sync": "# Listening stations: a hub collects what its stations log; a station sends to a hub (the Stations page).",
}


def update_section(section, values):
    """Set keys in one [section] of lorakeet.toml (the Radio and Stations pages): `values` {key: value}, None removes
    the key. Everything else in the file, comments included, is kept; the result is validated exactly like a
    hand-edited file before it replaces the old one, which is kept as lorakeet.toml.bak."""
    path = config_path()
    old = path.read_text(encoding="utf-8-sig") if path.exists() else ""
    lines = old.splitlines()
    head = next((i for i, l in enumerate(lines) if l.strip() == f"[{section}]"), None)
    if head is None:
        lines += ["", SECTION_NOTES.get(section, f"# {section}"), f"[{section}]"]
        head = len(lines) - 1
    end = next((i for i in range(head + 1, len(lines)) if lines[i].lstrip().startswith("[")), len(lines))
    keys = "|".join(re.escape(k) for k in values)
    body = [l for l in lines[head + 1:end] if not re.match(rf"\s*({keys})\s*=", l)]
    new = [f"{k} = {_toml(v)}" for k, v in values.items() if v is not None]
    lines[head + 1:end] = new + body
    text = "\n".join(lines).rstrip("\n") + "\n"
    try:
        _validate(_merge(DEFAULTS, tomllib.loads(text)))
    except tomllib.TOMLDecodeError as e:
        raise ValueError(f"couldn't update {path.name} automatically ({e}); edit it by hand") from e
    if path.exists():
        _write_private(path.with_suffix(".toml.bak"), old)
    tmp = path.with_suffix(".toml.tmp")
    _write_private(tmp, text)
    os.replace(tmp, path)
    return path


def _write_private(path, text):
    """Write a file only this user can read: it may hold the station's sync token ([sync] token)."""
    path.unlink(missing_ok=True)  # os.open's mode only applies to a new file
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
        f.write(text)


def update_station(name, location):
    """[station] name and location (the Radio page). location: [lat, lon] (or with altitude), or empty to clear it."""
    loc = [round(float(x), 6) for x in (location or [])]
    return update_section("station", {"name": str(name or "").strip()[:60], "location": loc or None})


def write_setup(answers):
    """Write a new lorakeet.toml from the setup page's answers (validated exactly like a hand-written file).
    Refuses to overwrite: setup is for first runs; an existing file is edited by hand."""
    path = config_path()
    if path.exists():
        raise FileExistsError(f"{path} already exists")
    a = answers or {}
    radio = {}
    if a.get("host"):
        radio = {"host": str(a["host"]).strip(), "tcp_port": int(a.get("tcpPort") or 4403)}
    elif a.get("port"):
        radio = {"port": str(a["port"]).strip()}
    user = {
        "radio": radio,
        "storage": {"data_dir": str(a.get("dataDir") or "").strip()},
        "http": {"lan": "view" if a.get("lan") == "view" else "off"},
        "logging": {"store_recipients": bool(a.get("storeRecipients", True))},
        "alerts": {"auto_traceroute": bool(a.get("autoTraceroute", False))},
        "map": {"tiles": "esri" if a.get("tiles") == "esri" else "osm"},
        "station": {"name": str(a.get("stationName") or "").strip()[:60]},
    }
    loc = a.get("location")
    if loc:
        user["station"]["location"] = [round(float(loc[0]), 6), round(float(loc[1]), 6)]
    if a.get("hubUrl"):  # this station sends what it logs to a hub (a pairing code, checked by the server first)
        user["sync"] = {"mode": "collector", "hub_url": str(a["hubUrl"]), "token": str(a.get("hubToken") or "")}
    _validate(_merge(DEFAULTS, user))  # raises ValueError with the same messages a hand-edited file gets

    notes = {
        "radio": "# The radio. Empty = auto-detect a USB radio. port = a specific serial port; host = a radio on your network.",
        "storage": "# Where the database, logs and backups live. Empty = the per-OS default folder.",
        "http": '# "off": the dashboard only answers on this computer. "view": also read-only from your local network.',
        "logging": "# Record the recipients of other people's addressed packets (what your radio hears on the air).",
        "alerts": "# Scheduled traceroutes TRANSMIT on the mesh, so they're off unless you turn them on.",
        "map": '# Map tiles: "osm" (OpenStreetMap + OpenTopoMap) or "esri" (Esri maps and satellite; their terms apply).',
        "station": "# This listening station: a name, and the antenna's position [lat, lon] for the maps.",
        "sync": "# This station sends what it logs to a hub (from a pairing code). Keep the token private.",
    }
    lines = ["# Lorakeet settings, written by the first-run setup page. Every option is explained in",
             "# lorakeet.example.toml; edit this file and restart Lorakeet to change them.", ""]
    for section, values in user.items():
        lines += [notes[section], f"[{section}]"]
        lines += [f"{k} = {_toml(v)}" for k, v in values.items() if not (k == "data_dir" and v == "")] or ["# (defaults)"]
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")
    return path
