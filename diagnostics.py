"""A diagnostics bundle for bug reports: what's needed to understand a problem, with personal details removed
before anything is written. The person looks at it before attaching it anywhere; nothing is ever sent.

Works from the files alone (lorakeet.toml, the logs, mesh.db), so `python diagnostics.py` helps even when
Lorakeet won't start; the Settings page adds what only the running Lorakeet knows (`live`).

    summary.json   versions, platform, database, backups, sync, the radio (model, firmware, settings that matter)
    config.txt     lorakeet.toml: every key, but only on/off, numbers and a few fixed choices; the rest "<set>"
    log.txt        warnings and errors from server.log (and the start-up lines), redacted
    supervise.txt  the end of the background runner's log (Windows), redacted
"""
import getpass
import io
import json
import os
import platform
import re
import socket
import sqlite3
import sys
import time
import zipfile
from datetime import datetime
from pathlib import Path

PACKAGES = ("meshtastic", "pyserial", "protobuf", "pypubsub", "cryptography", "segno", "pystray", "Pillow", "bleak")
LOG_LINES = 400       # warnings/errors (and their tracebacks) kept from server.log
STARTUP_LINES = 60    # "connected", "disconnected"... lines kept for context
SUPERVISE_LINES = 120

# radio settings worth knowing when something goes wrong (from the last "connected" event): never names, keys,
# Wi-Fi details or positions
RADIO_CONFIG = {
    "device": ("role", "rebroadcastMode", "serialEnabled"),
    "lora": ("region", "modemPreset", "usePreset", "hopLimit", "txEnabled", "txPower", "overrideDutyCycle",
             "sx126xRxBoostedGain", "ignoreMqtt", "configOkToMqtt"),
    "security": ("debugLogApiEnabled", "isManaged", "serialEnabled", "adminChannelEnabled"),
    "position": ("gpsMode", "gpsUpdateInterval", "positionBroadcastSmartEnabled", "fixedPosition"),
    "network": ("wifiEnabled", "ethEnabled"),
    "bluetooth": ("enabled", "mode"),
}
RADIO_METADATA = ("firmwareVersion", "hwModel", "role", "hasWifi", "hasBluetooth", "hasPKC", "deviceStateVersion")
SAFE_STRINGS = {("http", "lan"), ("map", "tiles"), ("sync", "mode"), ("backup", "time")}


class Redactor:
    """Removes what could identify a person, a place or a secret from free text. Radio ids become !node01,
    !node02... consistently across the bundle, so "the same radio" stays visible."""

    COMMON = {"lorakeet", "meshtastic", "node", "radio", "station", "base", "home", "router", "client", "repeater",
              "tracker", "sensor", "default", "unknown", "test", "none", "true", "false", "error", "warning", "info"}

    def __init__(self, words=()):
        self.ids = {}
        words = sorted({w.strip() for w in words if isinstance(w, str) and self.is_name(w.strip())}, key=len, reverse=True)
        # case-sensitive: a radio called "SELF" mustn't blank out `self` in every traceback
        self.words = re.compile("|".join(r"(?<!\w)" + re.escape(w) + r"(?!\w)" for w in words)) if words else None

    @classmethod
    def is_name(cls, w):
        """Worth hiding: 3+ characters, not one of Lorakeet's own words, and not a short all-lowercase word (a radio
        named "node" would otherwise blank out that word everywhere)."""
        return len(w) >= 3 and w.lower() not in cls.COMMON and not (w.islower() and len(w) <= 6)

    def _id(self, m):
        v = m.group(0).lower()
        if v == "!ffffffff":
            return v
        if v not in self.ids:
            self.ids[v] = f"!node{len(self.ids) + 1:02d}"
        return self.ids[v]

    RULES = [
        (re.compile(r"""\bb(['"])(?:\\[\s\S]|(?!\1)[\s\S]){8,}?\1"""), "b'<bytes>'"),  # raw radio bytes: names, text
        # a peer's or pairing's label in a log line ("peer Alice's hub: unreachable"): the first one on each line
        (re.compile(r"\b(peer|pairing|station) ([^:!\n]{1,60}?)(: )"), r"\1 <name>\3"),
        (re.compile(r"\blk1-[A-Za-z0-9+/=_-]+"), "<pairing code>"),
        (re.compile(r"https?://(?!(?:127\.0\.0\.1|localhost)[:/])[^\s\"'<>)]+"), "<url>"),
        (re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"), "<email>"),
        (re.compile(r"\b(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}\b"), "<mac>"),
        (re.compile(r"\b(?!127\.0\.0\.1\b|0\.0\.0\.0\b|255\.255\.255\.255\b)(?:\d{1,3}\.){3}\d{1,3}\b"), "<ip>"),
        (re.compile(r"\b(?:[0-9a-fA-F]{1,4}:){3,7}[0-9a-fA-F]{0,4}\b|\b[0-9a-fA-F]{1,4}::[0-9a-fA-F:]*\b"), "<ip6>"),
        # a space is part of the path when more of the path follows ("C:\Program Files\Lorakeet")
        (re.compile(r"(?<![A-Za-z])[A-Za-z]:[\\/](?![\\/])(?:[^\s\"',;)\]]| (?=[^\s\"']*[\\/]))*"), "<path>"),
        (re.compile(r"(?<![\w.])/(?:home|Users|media|mnt|root|srv|var/lib|run/media)/[^\s\"',;)\]]*"), "<path>"),
        (re.compile(r"-?\b\d{1,3}\.\d{3,}\s*,\s*-?\d{1,3}\.\d{3,}\b"), "<lat>, <lon>"),
        (re.compile(r"(?i)\b(lat|lon|lng|latitude|longitude|latitude_?i|longitude_?i)([\"']?\s*[:=]\s*)-?\d+(?:\.\d+)?"),
         r"\1\2<n>"),
        (re.compile(r"\b[A-Za-z0-9+/_-]{32,}={0,2}"), "<secret>"),
    ]

    def __call__(self, text, names=True):
        text = str(text)
        for i, (rx, rep) in enumerate(self.RULES[:9]):  # bytes, labels, URLs, emails, MACs, addresses, paths before ids
            text = "\n".join(rx.sub(rep, line, count=1) for line in text.split("\n")) if i == 1 else rx.sub(rep, text)
        text = re.sub(r"![0-9A-Fa-f]{8}\b", self._id, text)
        for rx, rep in self.RULES[9:]:
            text = rx.sub(rep, text)
        if names and self.words:
            text = self.words.sub("<name>", text)
        return text


def _items(s):
    """How many list items a piece of a TOML array holds (up to its closing bracket)."""
    return len([x for x in s.split("]")[0].split(",") if x.strip()])


def redact_config(text, redact):
    """lorakeet.toml, line by line (so a file Lorakeet can't parse still comes through): every section and key,
    with only on/off, numbers and a few fixed choices shown; strings become <set>, lists their length. Comments go."""
    out, section, key, in_list = [], "", "", None
    for line in text.splitlines():
        s = line.split(" #")[0].strip() if not line.strip().startswith("#") else ""
        if in_list is not None:  # the rest of a multi-line list
            in_list += _items(s)
            if "]" in s:
                out.append(f"{key} = [{in_list} items]")
                in_list = None
            continue
        if not s:
            if not line.strip():
                out.append("")
            continue
        if s.startswith("["):
            section = s.strip("[] ")
            out.append(redact(f"[{section}]"))
            continue
        key, eq, value = s.partition("=")
        key, value = redact(key.strip()), value.strip()
        if not eq:
            out.append("<unreadable line>")
        elif value in ("true", "false") or re.fullmatch(r"-?\d+(\.\d+)?", value):
            out.append(f"{key} = {value}")
        elif value.startswith("["):
            if "]" in value:
                out.append(f"{key} = [{_items(value[1:])} items]")
            else:
                in_list = _items(value[1:])
        elif value in ('""', "''"):
            out.append(f'{key} = ""')
        elif (section, key) in SAFE_STRINGS or (section, key) == ("radio", "port") and re.fullmatch(r'"(COM\d+|/dev/tty\w+)"', value):
            out.append(f"{key} = {value}")
        else:
            out.append(f"{key} = <set>")
    return "\n".join(out).strip("\n") + "\n"


def _log_lines(paths):
    lines = []
    for p in paths:
        try:
            lines += p.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            pass
    return lines


STAMP = re.compile(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d")
STARTUP = re.compile(r"INFO\s+\S+: (Lorakeet \d|connecting to|connected:|disconnected|watchdog|logging (paused|resumed)|"
                     r"radio channels|backup|sync|restart|demo)", re.I)


def pick_log(lines):
    """Warnings and errors with their tracebacks, plus the start-up/connection lines; oldest first."""
    problems, context, keep = [], [], False
    for line in lines:
        if STAMP.match(line):
            keep = bool(re.search(r"\b(WARNING|ERROR|CRITICAL)\b", line[:60]))
            if keep:
                problems.append(line)
            elif STARTUP.search(line):
                context.append(line)
        elif keep:  # a traceback's continuation
            problems.append(line)
    return problems[-LOG_LINES:], context[-STARTUP_LINES:]


def _versions():
    from importlib import metadata
    out = {}
    for p in PACKAGES:
        try:
            out[p] = metadata.version(p)
        except metadata.PackageNotFoundError:
            pass
    return out


def _ago(ts, now):
    return None if not ts else round((now - ts) / 3600, 1)


def _database(path, now, redact):
    if not path.is_file():
        return {"exists": False}, None
    info = {"exists": True, "sizeMB": round(path.stat().st_size / 1e6, 1)}
    wal = path.with_name(path.name + "-wal")
    if wal.is_file():
        info["walMB"] = round(wal.stat().st_size / 1e6, 1)
    radio = None
    try:
        db = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=5)
        db.row_factory = sqlite3.Row
        info["layoutVersion"] = db.execute("PRAGMA user_version").fetchone()[0]
        info["journal"] = db.execute("PRAGMA journal_mode").fetchone()[0]
        tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        info["rows"] = {t: db.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in
                        ("packets", "rx_hops", "tx_log", "telemetry_full", "positions", "messages", "node_info",
                         "events") if t in tables}
        if "packets" in tables:
            first, last = db.execute("SELECT MIN(ts), MAX(ts) FROM packets").fetchone()
            info["firstPacketDaysAgo"] = None if not first else round((now - first) / 86400, 1)
            info["lastPacketHoursAgo"] = _ago(last, now)
            info["stations"] = db.execute("SELECT COUNT(DISTINCT station) FROM packets").fetchone()[0]
        if "events" in tables:
            week = now - 7 * 86400
            info["eventsLast7Days"] = {r[0]: r[1] for r in db.execute(
                "SELECT kind, COUNT(*) FROM events WHERE ts > ? GROUP BY kind ORDER BY 2 DESC", (week,))}
            info["recentProblems"] = [
                {"hoursAgo": _ago(r["ts"], now), "kind": r["kind"], "detail": redact(r["detail"] or "")[:300]}
                for r in db.execute("SELECT ts, kind, detail FROM events WHERE kind IN ('connect_failed', 'disconnected',"
                                    " 'watchdog', 'power', 'network') ORDER BY ts DESC LIMIT 15")]
            r = db.execute("SELECT ts, detail FROM events WHERE kind='connected' ORDER BY ts DESC LIMIT 1").fetchone()
            if r:
                radio = _radio_from_event(json.loads(r["detail"] or "{}"), r["ts"], now)
        if "settings" in tables:
            r = db.execute("SELECT value FROM settings WHERE key='radio_channels'").fetchone()
            if r and radio is not None:
                chans = json.loads(r[0]).get("channels") or []
                radio["channels"] = {"count": len(chans), "publicKey": sum(1 for c in chans if c.get("publicKey")),
                                     "private": sum(1 for c in chans if c.get("encrypted") and not c.get("publicKey"))}
        if "alerts" in tables:
            info["openAlerts"] = {r[0]: r[1] for r in db.execute(
                "SELECT kind, COUNT(*) FROM alerts WHERE resolved_ts IS NULL AND read = 0 GROUP BY kind")}
        db.close()
    except sqlite3.Error as e:
        info["error"] = redact(str(e))
    return info, radio


def _radio_from_event(ev, ts, now):
    port = str(ev.get("port") or "")
    r = {"lastConnectedHoursAgo": _ago(ts, now), "nodes": ev.get("nodes"),
         "link": "network" if ":" in port and not port.upper().startswith("COM") else "usb" if port else None,
         **{k: (ev.get("metadata") or {}).get(k) for k in RADIO_METADATA},
         "pioEnv": (ev.get("my_info") or {}).get("pioEnv"), "rebootCount": (ev.get("my_info") or {}).get("rebootCount")}
    cfg = ev.get("config") or {}
    r["config"] = {sec: {k: cfg.get(sec, {}).get(k) for k in keys if k in cfg.get(sec, {})} for sec, keys in RADIO_CONFIG.items()}
    mods = ev.get("module_config") or {}
    r["modulesEnabled"] = sorted(m for m, v in mods.items() if isinstance(v, dict) and v.get("enabled"))
    return {k: v for k, v in r.items() if v not in (None, {}, [])}


def _names(db_path, data_dir, cfg):
    """Words to hide wherever they appear: radio and station names, peer and pairing labels, this computer's name
    and user name."""
    words = {getpass.getuser(), socket.gethostname(), cfg.get("station", {}).get("name"), cfg.get("station", {}).get("note"),
             cfg.get("base", {}).get("name"), cfg.get("station", {}).get("drive_ping_channel")}
    try:
        db = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True, timeout=5)
        for long_name, short_name in db.execute("SELECT DISTINCT long_name, short_name FROM node_info"):
            words |= {long_name, short_name}
        for (detail,) in db.execute("SELECT detail FROM events WHERE kind = 'network'"):  # Wi-Fi network names
            d = json.loads(detail or "{}")
            words |= {d.get("network"), d.get("prev")} - {"no network"}
        r = db.execute("SELECT value FROM settings WHERE key='station_meta'").fetchone()
        for m in (json.loads(r[0]) if r else {}).values():
            words |= {m.get("name"), m.get("note")}
        r = db.execute("SELECT value FROM settings WHERE key='radio_channels'").fetchone()
        presets = {"LongFast", "LongSlow", "LongModerate", "LongTurbo", "MediumFast", "MediumSlow", "ShortFast",
                   "ShortSlow", "ShortTurbo", "VLongSlow"}  # the public preset channels name nothing personal
        words |= {c.get("name") for c in (json.loads(r[0]).get("channels") or [] if r else [])} - presets
        db.close()
    except (sqlite3.Error, ValueError):
        pass
    for f, key in (("peers.json", "name"), ("stations.json", "label")):
        try:
            words |= {e.get(key) for e in json.loads((data_dir / f).read_text(encoding="utf-8"))}
        except (OSError, ValueError, AttributeError, TypeError):
            pass
    return {w for w in words if isinstance(w, str)}


def build(config_path=None, data_dir=None, live=None, now=None):
    """{file name: text}. config_path / data_dir default to this install's; `live` is what the running server adds
    (already free of personal details: see web.py)."""
    import config as cfgmod
    now = now or time.time()
    config_path = Path(config_path or cfgmod.config_path())
    raw = config_path.read_text(encoding="utf-8-sig", errors="replace") if config_path.is_file() else ""
    cfg_error, cfg = None, {}
    try:
        import tomllib
        cfg = cfgmod._merge(cfgmod.DEFAULTS, tomllib.loads(raw))  # as config.file_settings, for any path
        cfgmod._validate(cfg)
    except Exception as e:  # noqa: BLE001  - a broken file is exactly what a report may be about
        cfg_error = str(e)
    if data_dir is None:  # CFG's: the file's data_dir, or (diagnostics.py on a broken file) its best guess
        data_dir = (cfg.get("storage", {}).get("data_dir") if cfg else None) or cfgmod.CFG["storage"]["data_dir"]
    data_dir = Path(data_dir)
    redact = Redactor(_names(data_dir / "mesh.db", data_dir, cfg))

    import runtime
    try:  # the software fingerprint; station.py imports the radio libraries, which may be what's broken
        from station import SOFTWARE
    except Exception:  # noqa: BLE001
        SOFTWARE = None
    summary = {
        "created": datetime.fromtimestamp(now).isoformat(timespec="seconds"),
        "lorakeet": {"version": runtime.VERSION, "software": SOFTWARE, "running": live is not None},
        "system": {"os": platform.platform(), "machine": platform.machine(), "python": sys.version.split()[0],
                   "venv": sys.prefix != sys.base_prefix, "packages": _versions()},
        "config": {"found": config_path.is_file(), "error": redact(cfg_error) if cfg_error else None},
    }
    if cfg:
        summary["config"].update(lan=cfg["http"]["lan"], hostnames=len(cfg["http"]["hostnames"]),
                                 radio="network" if cfg["radio"]["host"] else "usb" if cfg["radio"]["port"] else "auto",
                                 sync=cfg["sync"]["mode"], mobile=cfg["station"]["mobile"],
                                 drivePings=cfg["station"]["drive_pings"], remoteCommands=cfg["remote"]["enabled"],
                                 backupFolders=len(cfg["backup"]["destinations"]), tiles=cfg["map"]["tiles"],
                                 mcp={k: cfg["mcp"][k] for k in ("allow_changes", "allow_transmit")})
    db, radio = _database(data_dir / "mesh.db", now, redact)
    summary["database"] = db
    if (data_dir / "debug.db").is_file():
        summary["database"]["debugLogMB"] = round((data_dir / "debug.db").stat().st_size / 1e6, 1)
    try:
        st = json.loads((data_dir / "storage_state.json").read_text(encoding="utf-8"))
        last = st.get("last_backup") or {}
        summary["backups"] = {"lastOkHoursAgo": _ago(st.get("last_ok_ts"), now), "lastOk": bool(last.get("ok")),
                              "lastError": redact(last.get("error") or "") or None,
                              "destinations": [bool(d.get("ok")) for d in last.get("dests") or []]}
    except (OSError, ValueError):
        summary["backups"] = None
    if radio:
        summary["radio"] = radio
    try:
        usage = __import__("shutil").disk_usage(data_dir)
        summary["system"]["diskFreeGB"] = round(usage.free / 1e9, 1)
    except OSError:
        pass
    if live:  # errors in it can name a hub, a peer or a path
        summary["live"] = json.loads(redact(json.dumps(live, default=str)))

    problems, context = pick_log(_log_lines([data_dir / "server.log.2", data_dir / "server.log.1", data_dir / "server.log"]))
    log_txt = ("Warnings and errors (newest last):\n\n" + "\n".join(redact(x) for x in problems) if problems
               else "No warnings or errors in server.log.\n") + \
        "\n\nStart-up and connection lines:\n\n" + "\n".join(redact(x) for x in context) + "\n"
    files = {
        "README.txt": README,
        # its free text (event details, errors) was redacted where it was read; this pass catches ids and addresses
        # anywhere without touching key names
        "summary.json": redact(json.dumps(summary, indent=2, default=str), names=False) + "\n",
        "config.txt": redact_config(raw, redact) if raw else "(no lorakeet.toml: Lorakeet hasn't been set up yet)\n",
        "log.txt": log_txt,
    }
    sup = _log_lines([data_dir / "supervise.log"])
    if sup:
        files["supervise.txt"] = "\n".join(redact(x) for x in sup[-SUPERVISE_LINES:]) + "\n"
    return files


README = """Lorakeet diagnostics

Made by Lorakeet for a bug report. Personal details were removed before anything was written: radio ids are
replaced (!node01, !node02...), and so are names, addresses, paths, locations, keys and tokens.

Please read the files before attaching them anywhere: an automatic check can miss something (a name in an
unusual place, for example). Remove anything you'd rather not share.

    summary.json   versions, platform, database, backups, sync, the radio's model, firmware and settings
    config.txt     lorakeet.toml: on/off settings and numbers only; everything else shows as <set>
    log.txt        warnings and errors from Lorakeet's log, and its start-up lines
    supervise.txt  the background runner's log (Windows), if there is one

Nothing was sent anywhere.
"""


def zip_bytes(files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, text in files.items():
            z.writestr(f"lorakeet-diagnostics/{name}", text)
    return buf.getvalue()


def filename(now=None):
    import runtime
    return f"lorakeet-diagnostics-{runtime.VERSION}-{datetime.fromtimestamp(now or time.time()):%Y%m%d-%H%M}.zip"


def write(path=None, live=None):
    """Write the zip (default: the current folder) and return its path."""
    files = build(live=live)
    path = Path(path) if path else Path(os.getcwd()) / filename()
    if path.is_dir():
        path = path / filename()
    path.write_bytes(zip_bytes(files))
    return path


if __name__ == "__main__":
    # python diagnostics.py [folder or file.zip]: works even when Lorakeet won't start (a broken lorakeet.toml, a
    # missing library), so it imports as little as it can and tells config.py not to give up on a broken file
    os.environ["LORAKEET_DIAGNOSTICS"] = "1"
    out = write(sys.argv[1] if len(sys.argv) > 1 else None)
    print(f"Saved {out}")
    print("Personal details were removed, but please read it before attaching it to a bug report.")
