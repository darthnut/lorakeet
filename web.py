"""The HTTP server: access rules, the request handler (pages, analytics, settings, login, the event
stream), and connection limits. Radio-page and hub routes are mixins in web_radio.py and web_hub.py.
"""
import errno
import ipaddress
import json
import logging
import os
import queue
import re
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import airtime
import analytics
import anatomy
import graphmetrics
import meshcore_anatomy
import api_index
import diagnostics
from channels import DEFAULT_PSK, _xor, channel_keys
import compare
from config import CFG
from config import public as config_public
import drive
import insights
import anomalies
import corruption
import linkhistory
import reach
import noise
import weather
import keyflags
import login as login_mod
from mesh import messages_for_this_radio, serial_ports
import node_analytics
import packetsearch
import pairing
import radioid
import runtime
from station import SOFTWARE, station_overrides, station_report, supervised_now
import sync as sync_mod
import topology
import web_hub
import web_radio


log = logging.getLogger("meshdash")


# lorakeet.toml settings the Settings page may change (sync is the Stations page's; the data folder isn't moved here)
PREFS_EDITABLE = {
    "station": ("name", "location", "note", "mobile", "drive_pings", "drive_ping_m", "drive_ping_min_s", "drive_ping_channel"),
    "radio": ("port", "host", "tcp_port"),
    "http": ("lan", "hostnames", "port"),
    "map": ("center", "zoom", "tiles"),
    "base": ("id", "name"),
    "backup": ("destinations", "time"),
    "storage": ("db_warn_gb", "debug_log_days"),
    "logging": ("store_recipients",),
    "remote": ("enabled", "allow"),
    "mcp": ("allow_changes", "allow_transmit"),
    "weather": ("enabled",),
}
SLOW_REQUEST_S = 5    # requests slower than this are logged (with the path), to find what holds the database


def packet_anatomy(store, mesh, rowid, hashtag=None):
    """One logged packet rebuilt layer by layer (anatomy.py), matched with the radio's own RX log line. A MeshCore
    packet is laid out from its own logged bytes instead (meshcore_anatomy.py); `hashtag` is a channel name to try."""
    rows = store.query("SELECT rowid, ts, from_id, pkt_id, hops, relay, pki, raw, station, portnum FROM packets "
                       "WHERE rowid=?", rowid)
    if not rows or not rows[0]["raw"]:
        raise ValueError("no such packet")
    if str(rows[0]["portnum"] or "").startswith("MC_"):
        return meshcore_packet_anatomy(store, mesh, dict(rows[0]), hashtag)
    row = dict(rows[0])
    row["raw"] = json.loads(row["raw"])
    # the reception of this same copy in the debug log: same sender + packet id, preferring the same
    # hop count and relay byte, nearest in time
    rx = store.query("SELECT * FROM rx_hops WHERE from_id=? AND pkt_id=? AND station IS ? "
                     "ORDER BY (hops IS ?) DESC, (relay IS ?) DESC, ABS(ts - ?) LIMIT 1",
                     row["from_id"], row["pkt_id"], row["station"], row["hops"], row["relay"], row["ts"]) if row["pkt_id"] is not None else []
    setting = lambda k: json.loads((store.query("SELECT value FROM settings WHERE key=?", k) or [{"value": "{}"}])[0]["value"])  # noqa: E731
    channels = setting("radio_channels").get("channels") or []
    lora = {k: v for k, v in setting("radio_lora").items() if k != "ts"}
    iface = mesh.iface
    try:
        keys = channel_keys(iface) if iface is not None else {}
    except Exception:  # noqa: BLE001
        keys = {}
    keys.setdefault(_xor(b"LongFast") ^ _xor(DEFAULT_PSK), DEFAULT_PSK)  # the public default key, always known
    # traceroutes addressed to the station that logged this packet were edited by that station's radio
    out = anatomy.build(row, dict(rx[0]) if rx else None, channels, lora, keys, row["station"] or mesh.local_id)
    # sender / recipient with a compromised or shared public key (keyflags.py)
    flagged = keyflags.flags(runtime.DATA_DIR / "mesh.db")
    to = row["raw"].get("toId")
    out["keyFlags"] = [{"role": role, "id": nid, "name": mesh.name(nid), **flagged[nid], "text": keyflags.text(flagged[nid], mesh.name)}
                       for role, nid in (("sender", row["from_id"]), ("recipient", to)) if nid in flagged]
    # probably damaged on the air (corruption.py): why, and the header next to the likely original's
    d = corruption.flags(runtime.DATA_DIR / "mesh.db")["packets"].get((row["from_id"], row["pkt_id"]))
    if d:
        out["corruption"] = dict(d, originalName=mesh.name(d["original"]) if d.get("original") else None)
    return out


def meshcore_packet_anatomy(store, mesh, row, hashtag=None):
    row["raw"] = json.loads(row["raw"])
    h = (row["raw"].get("meshcore") or {}).get("hash")
    receptions = store.query("SELECT * FROM mc_rx WHERE packet_hash = ? ORDER BY ts", h) if h else []
    # the logging radio's own settings, as it reported them when it connected (SELF_INFO)
    ev = store.query("SELECT detail FROM events WHERE kind = 'connected' AND station IS ? AND detail LIKE '%self_info%' "
                     "ORDER BY ts DESC LIMIT 1", row["station"])
    d = json.loads(ev[0]["detail"]) if ev and ev[0]["detail"] else {}
    radio = (d.get("self_info") or {}) if isinstance(d, dict) else {}
    nodes = [r["node"] for r in store.query("SELECT DISTINCT node FROM node_info WHERE node LIKE 'mc:%'")]
    return meshcore_anatomy.build(row, receptions, radio, nodes, lambda i: {"name": mesh.name(i)}, hashtag)


def setup_info(mesh):
    """What the setup page shows: whether this install is configured, the defaults, ports, the live link."""
    import config as cfgmod
    import importlib.util
    try:
        has_meshcore = importlib.util.find_spec("meshcore") is not None
    except (ImportError, ValueError):  # a stand-in module without a spec (tests)
        has_meshcore = "meshcore" in sys.modules
    py = Path(sys.executable)
    py = py.with_name("python.exe") if py.name.lower() == "pythonw.exe" else py  # pip's output needs a console
    req = runtime.HERE / "requirements-meshcore.txt"
    info = {"configured": CFG["_path"] is not None, "path": str(cfgmod.config_path()),
            # the optional library a MeshCore radio needs: looked for, never imported here
            "meshcoreAvailable": has_meshcore,
            "meshcoreInstall": f'"{py}" -m pip install -r "{req}"',
            "defaults": {"dataDir": str(cfgmod.default_data_dir()), "httpPort": CFG["http"]["port"]},
            "ports": serial_ports(), "status": mesh.status(),
            "radioName": mesh.name(mesh.local_id) if mesh.connected and mesh.local_id else None,
            "radioFix": mesh.own_fix_now() if mesh.connected else None,
            "supervised": bool(os.environ.get("LORAKEET_SUPERVISED") or os.environ.get("INVOCATION_ID"))}
    if info["configured"]:  # a read-only summary; the file itself is edited by hand
        r = CFG["radio"]
        where = r["host"] and f"{r['host']}:{r['tcp_port']}" or r["port"] or "auto-detect (USB)"
        info["summary"] = {"radio": f"MeshCore on {where}" if r["protocol"] == "meshcore" else where,
                           "dataDir": str(CFG["storage"]["data_dir"]), "lan": CFG["http"]["lan"],
                           "storeRecipients": CFG["logging"]["store_recipients"], "tiles": CFG["map"]["tiles"],
                           "stationName": CFG["station"]["name"], "location": bool(CFG["station"]["location"])}
    return info


MIME = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
        ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml", ".json": "application/json",
        ".png": "image/png", ".ico": "image/x-icon", ".webmanifest": "application/manifest+json"}


def client_access(ip):
    """'full' for this PC (loopback), 'view' for private home-network addresses, None for anything else.

    The dashboard can transmit (messages, traceroutes) and change settings, so only this PC gets that;
    other devices on the LAN get a read-only view (user's choice, 2026-10-06).
    """
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return None
    if a.is_loopback:
        return "full"
    if isinstance(a, ipaddress.IPv6Address) and a.ipv4_mapped:
        return client_access(str(a.ipv4_mapped))
    if a.is_private or a.is_link_local:
        return "view"
    return None


def make_handler(mesh, store, storage, alert_engine, syncer=None, login=None, peering=None, pairings=None):
    login = login or login_mod.Login(runtime.DATA_DIR / "login.json")
    pairings = pairings or pairing.Pairings(runtime.DATA_DIR / "stations.json")
    if isinstance(syncer, sync_mod.Hub):
        syncer.is_revoked = pairings.is_revoked
    hub_id = peering.hub_id if peering else pairing.install_id(runtime.DATA_DIR)

    def hub_name():
        return CFG["station"]["name"] or (mesh.name(mesh.local_id) if mesh.local_id else None) or socket.gethostname()

    def station_meta():
        r = store.query("SELECT value FROM settings WHERE key='station_meta'")
        return json.loads(r[0]["value"]) if r else {}

    def station_name(sid):
        """A station's name: the node DB, else the newest name in any station's identity log (a
        collector's own radio announces itself to that collector, not necessarily to us)."""
        if not sid:
            return None
        if sid == analytics.ALL_STATIONS:
            return "All stations"
        if sid == mesh.local_id and CFG["station"]["name"]:
            return CFG["station"]["name"]
        over = (station_overrides(store).get(sid) or {}).get("name") if store else None
        if over:
            return over
        meta = station_meta().get(sid) or {}
        if meta.get("name") and meta.get("name") != sid:
            return meta["name"]
        name = mesh.describe(sid)["name"]
        if name == sid:
            r = store.query("SELECT long_name FROM node_info WHERE node = ? AND long_name IS NOT NULL "
                            f"AND {radioid.REAL_IDENTITY} ORDER BY ts DESC LIMIT 1", sid)
            name = r[0]["long_name"] if r else sid
        return name

    ctx = dict(mesh=mesh, store=store, storage=storage, alert_engine=alert_engine, syncer=syncer, login=login,
               peering=peering, pairings=pairings, hub_id=hub_id, hub_name=hub_name, station_meta=station_meta,
               station_name=station_name)

    class Handler(web_radio.make(**ctx), web_hub.make(**ctx), BaseHTTPRequestHandler):
        timeout = 60  # a connection that sends nothing for this long is closed (the event stream pings every 15 s)

        def log_message(self, *a):
            pass

        def end_headers(self):
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("X-Content-Type-Options", "nosniff")
            super().end_headers()

        def _json(self, obj, code=200):
            body = json.dumps(obj, default=str).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _access(self):
            """(access, base): login.access() for this request, and client_access() of its address alone."""
            base = client_access(self.client_address[0])
            token = login_mod.cookie_value(self.headers.get("Cookie")) if base == "view" else None
            return login_mod.access(base, CFG["http"]["lan"], login.session(token)), base

        def _send(self, body, content_type, headers=None):
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _length(self):
            """The request body's length; a missing one is 0, a negative or garbled one is refused (rfile.read(-1)
            would read until the client closes)."""
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                raise ValueError("bad Content-Length") from None
            if n < 0:
                raise ValueError("bad Content-Length")
            return n

        def _host_ok(self):
            """DNS rebinding guard: a browser on this PC visiting a site whose name was pointed at 127.0.0.1 would
            otherwise get this PC's full access. Answer only to IP addresses, localhost, and names listed in
            [http] hostnames. (Ingest is exempt: it's token-checked, and stations may use any name for the hub.)"""
            host = (self.headers.get("Host") or "").strip()
            if not host:
                return True  # not a browser (browsers always send Host)
            name = host[1:host.find("]")] if host.startswith("[") else host.rsplit(":", 1)[0] if host.count(":") == 1 else host
            name = name.lower().rstrip(".")
            try:
                ipaddress.ip_address(name)
                return True
            except ValueError:
                pass
            return name == "localhost" or name in {h.lower() for h in CFG["http"]["hostnames"]}

        def _wrong_host(self):
            return self._json({"error": "Lorakeet only answers to its IP address, localhost, or a name listed in "
                                        "[http] hostnames in lorakeet.toml"}, 421)

        def do_GET(self):  # noqa: N802
            runtime.LAST_REQUEST[0] = time.time()
            if not self._host_ok():
                return self._wrong_host()
            t0 = time.time()
            try:
                return self._get()
            except ValueError as e:   # a malformed number in the query (?limit=abc): the asker's mistake, not ours
                log.info("bad request %s: %s", self.path[:200], e)
                return self._json({"error": f"bad request: {e}"}, 400)
            finally:
                dt = time.time() - t0
                if dt > SLOW_REQUEST_S and not self.path.startswith("/api/events"):
                    log.warning("slow request: %s took %.1f s", self.path[:200], dt)

        def _get(self):
            url = urlparse(self.path)
            qs = {k: v[0] for k, v in parse_qs(url.query).items()}
            # which listening station the analytics are about: ?station=!xxxxxxxx, default the radio this
            # server is logging from (analytics._connect validates it and scopes every query to it)
            station = qs.get("station") or mesh.local_id
            path = url.path
            access, base = self._access()
            if access is None:
                return self._json({"error": "only devices on the local network may connect"}, 403)
            if path == "/api/version":
                return self._json({"version": runtime.VERSION, "software": SOFTWARE, "python": sys.version.split()[0],
                                   "meshtastic": _lib_version()})
            if access == "login" and path != "/api/whoami" and not _login_page_asset(path):
                if path.startswith("/api/"):
                    return self._json({"error": "log in first"}, 401)
                self.send_response(302)
                self.send_header("Location", "/login.html")
                self.end_headers()
                return
            if path == "/api/config":
                return self._json(config_public())
            if path == "/api/index":  # every endpoint, for programs and LLMs (api_index.py, docs/API.md)
                return self._json(api_index.index(runtime.VERSION))
            if path == "/api/keyflags":  # radios with a compromised or shared public key (keyflags.py)
                f = keyflags.flags(runtime.DATA_DIR / "mesh.db")
                return self._json({nid: dict(v, name=mesh.name(nid), text=keyflags.text(v, mesh.name)) for nid, v in f.items()})
            if path == "/api/stations/timeline":  # power and network changes each station recorded (HealthWatch)
                hours = min(24 * 31, max(1, float(qs.get("hours", 24))))
                out = {}
                for r in store.query("SELECT ts, kind, detail, station FROM events WHERE kind IN ('power', 'network') "
                                     "AND ts >= ? ORDER BY ts", time.time() - hours * 3600):
                    d = json.loads(r["detail"] or "{}")
                    out.setdefault(r["station"] or "", []).append({"ts": r["ts"], "kind": r["kind"], **(d if isinstance(d, dict) else {})})
                return self._json({"hours": hours, "stations": out})
            if path == "/api/brief":  # one-line status for the tray icon
                return self._json({"version": runtime.VERSION, "connected": mesh.connected, "paused": mesh.paused,
                                   "port": mesh.port, "id": mesh.local_id,
                                   "name": mesh.name(mesh.local_id) if mesh.local_id else None})
            if path == "/api/whoami":
                return self._json({"access": access, "readOnly": access != "full", "version": runtime.VERSION, "paused": mesh.paused,
                                   "demo": bool(runtime.DEMO),
                                   "login": {"enabled": login.enabled, "loggedIn": access == "full" and base != "full",
                                             "lan": CFG["http"]["lan"]}})
            if runtime.DEMO and _demo_refuses("GET", path):
                return self._json(DEMO_REFUSAL, 403)
            if path == "/api/preferences":  # the Settings page: lorakeet.toml as saved and as running (this PC only)
                if base != "full":
                    return self._json({"error": "these settings are only available on the Lorakeet computer"}, 403)
                return self._json(self._preferences())
            if path in ("/api/diagnostics", "/api/diagnostics.zip"):  # Settings -> Help: a redacted bundle (this PC only)
                if base != "full":
                    return self._json({"error": "diagnostics are only available on the Lorakeet computer"}, 403)
                files = diagnostics.build(live=self._live_diagnostics())
                if path.endswith(".zip"):
                    return self._send(diagnostics.zip_bytes(files), "application/zip",
                                      {"Content-Disposition": f'attachment; filename="{diagnostics.filename()}"'})
                return self._json({"files": files, "filename": diagnostics.filename()})
            if path == "/api/hub":  # the Stations page: tokens and addresses, so this PC only
                if base != "full":
                    return self._json({"error": "the Stations page is only available on the dashboard PC"}, 403)
                return self._json(self._hub_info())
            if path.startswith("/api/radio"):  # the Radio page: backups and channel keys, so this PC only
                if base != "full":
                    return self._json({"error": "the Radio page is only available on the dashboard PC"}, 403)
                return self._radio_get(path, qs)
            if path == "/api/setup":  # paths and ports: this PC only (a login doesn't count: it shows paths)
                if base != "full":
                    return self._json({"error": "setup is only available on the dashboard PC"}, 403)
                return self._json(setup_info(mesh))
            # first run (no lorakeet.toml yet): the dashboard PC lands on the setup page
            if path in ("/", "/index.html") and CFG["_path"] is None and base == "full" and "skipsetup" not in qs and not runtime.DEMO:
                self.send_response(302)
                self.send_header("Location", "/setup.html")
                self.end_headers()
                return

            if path == "/api/state":
                return self._json(mesh.state())
            if path == "/api/packets":
                # the live feed: one entry per packet (its first copy), listing every station that heard it;
                # stations hearing each other's own radios are left out
                limit = min(int(qs.get("limit", 200)), 2000)
                # Pick the newest first copies FIRST, then work out arrival and "heard by" for just those: done for
                # every packet of the 3 days before the LIMIT, the feed took 5.7 s once five stations were logging.
                since = time.time() - 3 * 86400
                return self._json(store.query(
                    "WITH st AS (SELECT DISTINCT station AS id FROM packets WHERE station IS NOT NULL), "
                    # (a MeshCore packet usually names no sender: from_id NULL must neither drop it nor merge it)
                    "p AS (SELECT rowid AS rid, ts, ROW_NUMBER() OVER (PARTITION BY CASE WHEN pkt_id IS NULL THEN rowid "
                    "ELSE COALESCE(from_id, '') || ':' || pkt_id END ORDER BY ts) AS n FROM packets WHERE ts > ? "
                    "AND NOT (from_id IS NOT NULL AND from_id IN (SELECT id FROM st) AND from_id != station)), "
                    "top AS (SELECT rid FROM p WHERE n = 1 ORDER BY ts DESC LIMIT ?) "
                    "SELECT x.rowid AS rowid, x.ts, x.from_id, x.to_id, x.portnum, x.channel, x.snr, x.rssi, x.hops, x.via_mqtt, "
                    f"x.summary, x.relay, x.pkt_id, x.pki, x.station, {packetsearch.arrival_sql('x.raw')} AS arrival, "
                    "(SELECT GROUP_CONCAT(DISTINCT q.station) FROM packets q WHERE q.from_id IS x.from_id AND q.pkt_id = x.pkt_id "
                    "AND q.ts > ?) AS stations FROM top JOIN packets x ON x.rowid = top.rid ORDER BY x.ts DESC",
                    since, limit, since - 86400))
            if path.startswith("/api/packet/") and path.endswith("/anatomy"):
                try:
                    return self._json(packet_anatomy(store, mesh, int(path.split("/")[3]), (qs.get("channel") or "")[:32] or None))
                except (ValueError, IndexError):
                    return self._json({"error": "no such packet"}, 404)
                except Exception as e:  # noqa: BLE001
                    log.exception("anatomy failed")
                    return self._json({"error": f"anatomy failed: {e}"}, 500)
            if path.startswith("/api/packet/"):
                rows = store.query("SELECT raw FROM packets WHERE rowid=?", int(path.split("/")[3]))
                return self._json(json.loads(rows[0]["raw"]) if rows and rows[0]["raw"] else None)
            if path == "/api/channels":  # our radio's enabled channels: names and how private, never keys
                r = store.query("SELECT value FROM settings WHERE key='radio_channels'")
                chans = (json.loads(r[0]["value"]).get("channels") or []) if r else []
                return self._json({"channels": [{k: c.get(k) for k in ("index", "role", "name", "encrypted", "publicKey")}
                                                for c in chans]})
            if path == "/api/messages":
                # every station logs its own copy of a message: show each once, preferring this radio's copy
                # (our own sends keep their delivery status; another station's sends show as received here)
                return self._json(messages_for_this_radio(store, mesh.local_id))
            if path == "/api/links":
                since = time.time() - float(qs.get("hours", 24)) * 3600
                return self._json(store.query(
                    "SELECT a, b, source, MAX(ts) AS ts, AVG(snr) AS snr, COUNT(*) AS n FROM links "
                    "WHERE ts > ? GROUP BY a, b, source", since))
            if path.startswith("/api/node/"):
                nid = path.split("/")[3]
                since = time.time() - float(qs.get("hours", 72)) * 3600
                return self._json({
                    "node": mesh.node_json(nid),
                    "telemetry": store.query("SELECT * FROM telemetry WHERE node=? AND ts>? ORDER BY ts", nid, since),
                    "positions": store.query("SELECT * FROM positions WHERE node=? AND ts>? ORDER BY ts", nid, since),
                    "packets": store.query(
                        "SELECT rowid, ts, from_id, to_id, portnum, channel, snr, rssi, hops, via_mqtt, summary, "
                        "relay, pkt_id, pki FROM packets WHERE from_id=? ORDER BY ts DESC LIMIT 50", nid),
                })
            if path == "/api/traceroutes":
                rows = store.query("SELECT * FROM traceroutes WHERE target=? ORDER BY ts DESC LIMIT 5",
                                   qs.get("node", ""))
                return self._json([mesh._trace_json(r) for r in rows])
            if path == "/api/debuglog":
                return self._json(mesh.debug_log.query(
                    min(int(qs.get("limit", 500)), 5000),
                    since=float(qs["since"]) if "since" in qs else None,
                    contains=qs.get("q") or None,
                    before=float(qs["before"]) if "before" in qs else None,
                    levels=[x for x in qs.get("levels", "").split(",") if x] or None,
                    source=qs.get("source") or None))
            if path == "/api/analytics/airtime":
                try:
                    cache = {}
                    describe = lambda i: cache.setdefault(i, mesh.describe(i))  # noqa: E731
                    return self._json(airtime.compute(runtime.DATA_DIR / "mesh.db", qs.get("range", "7d"), station, describe))
                except ValueError as e:
                    return self._json({"error": str(e)}, 400)
                except Exception as e:  # noqa: BLE001
                    log.exception("airtime failed")
                    return self._json({"error": f"airtime failed: {e}"}, 500)
            if path == "/api/analytics/critical/history":   # critical relays day by day (graphmetrics.history)
                try:
                    cache = {}
                    describe = lambda i: cache.setdefault(i, mesh.describe(i))  # noqa: E731
                    return self._json(graphmetrics.history(runtime.DATA_DIR / "mesh.db", qs.get("range", "30d"), station, describe,
                                                           window_days=qs.get("window", graphmetrics.WINDOW_DAYS)))
                except ValueError as e:
                    return self._json({"error": str(e)}, 400)
                except Exception as e:  # noqa: BLE001
                    log.exception("critical relays history failed")
                    return self._json({"error": f"critical relays history failed: {e}"}, 500)
            if path == "/api/analytics/critical":
                try:
                    cache = {}
                    describe = lambda i: cache.setdefault(i, mesh.describe(i))  # noqa: E731
                    return self._json(graphmetrics.compute(runtime.DATA_DIR / "mesh.db", qs.get("range", "7d"), station, describe,
                                                           everyone=qs.get("all") == "1"))
                except ValueError as e:
                    return self._json({"error": str(e)}, 400)
                except Exception as e:  # noqa: BLE001
                    log.exception("critical relays failed")
                    return self._json({"error": f"critical relays failed: {e}"}, 500)
            if path == "/api/analytics/noise":   # noise floor, failed receptions and radio restarts per station (noise.py)
                try:
                    cache = {}
                    describe = lambda i: cache.setdefault(i, mesh.describe(i))  # noqa: E731
                    return self._json(noise.report(runtime.DATA_DIR / "mesh.db", qs.get("range", "7d"), station, describe))
                except ValueError as e:
                    return self._json({"error": str(e)}, 400)
                except Exception as e:  # noqa: BLE001
                    log.exception("noise failed")
                    return self._json({"error": f"noise failed: {e}"}, 500)
            if path == "/api/analytics/weather":   # weather at the stations (weather.py) and the links that follow it
                try:
                    cache = {}
                    describe = lambda i: cache.setdefault(i, mesh.describe(i))  # noqa: E731
                    dbp = runtime.DATA_DIR / "mesh.db"
                    h = linkhistory.history(dbp, qs.get("range", "7d"), station, describe)
                    db = analytics._connect(dbp, station)
                    try:
                        sq = weather.summary(db, dbp, h["stations"], describe, h["since"], h["until"])
                    finally:
                        db.close()
                    links = [{k: l[k] for k in ("a", "b", "aName", "bName", "station", "measured", "med", "n", "daily", "weather")}
                             for l in h["links"] if l.get("weather") and l["weather"].get("follows")]
                    links.sort(key=lambda l: -abs(l["weather"][l["weather"]["follows"]]["r"]))
                    return self._json({"enabled": bool(CFG["weather"]["enabled"]), "attribution": weather.ATTRIBUTION,
                                       "range": h["range"], "since": h["since"], "until": h["until"], "squares": sq,
                                       "links": links, "checked": sum(1 for l in h["links"] if l.get("weather")),
                                       "gridDeg": weather.GRID_DEG})
                except ValueError as e:
                    return self._json({"error": str(e)}, 400)
            if path in ("/api/analytics/links", "/api/analytics/links/compare"):   # link history (linkhistory.py)
                try:
                    cache = {}
                    describe = lambda i: cache.setdefault(i, mesh.describe(i))  # noqa: E731
                    dbp = runtime.DATA_DIR / "mesh.db"
                    if path == "/api/analytics/links":
                        return self._json(linkhistory.history(dbp, qs.get("range", "30d"), station, describe))
                    if not qs.get("at"):
                        return self._json({"error": "at (unix time) is required"}, 400)
                    ph = corruption.flags(dbp)["senders"]
                    return self._json(linkhistory.compare(dbp, qs["at"], qs.get("days", 7), station, describe,
                                                          phantom=lambda i: corruption.phantom(ph.get(i))))
                except ValueError as e:
                    return self._json({"error": str(e)}, 400)
                except Exception as e:  # noqa: BLE001
                    log.exception("link history failed")
                    return self._json({"error": f"link history failed: {e}"}, 500)
            if path == "/api/analytics/stations":
                try:
                    return self._json(compare.matrix(runtime.DATA_DIR / "mesh.db", qs.get("range", "7d"),
                                                     lambda i: {**mesh.describe(i), "name": station_name(i)}))
                except ValueError as e:
                    return self._json({"error": str(e)}, 400)
                except Exception as e:  # noqa: BLE001
                    log.exception("station matrix failed")
                    return self._json({"error": f"station matrix failed: {e}"}, 500)
            if path == "/api/analytics/compare":
                try:
                    others = [s["id"] for s in analytics.stations(runtime.DATA_DIR / "mesh.db") if s["id"] != mesh.local_id]
                    a = qs.get("a") or mesh.local_id
                    b = qs.get("b") or (others[0] if others else None)
                    out = compare.compute(runtime.DATA_DIR / "mesh.db", qs.get("range", "7d"), a, b,
                                          lambda i: {**mesh.describe(i), "name": station_name(i)})
                    for k, sid in (("aHw", a), ("bHw", b)):
                        out[k] = mesh.describe(sid).get("hw") or (store.query(
                            "SELECT hw_model FROM node_info WHERE node = ? ORDER BY ts DESC LIMIT 1", sid) or [{}])[0].get("hw_model")
                    return self._json(out)
                except ValueError as e:
                    return self._json({"error": str(e)}, 400)
                except Exception as e:  # noqa: BLE001
                    log.exception("compare failed")
                    return self._json({"error": f"compare failed: {e}"}, 500)
            if path == "/api/analytics/topology":
                try:
                    cache = {}
                    describe = lambda i: cache.setdefault(i, mesh.describe(i))  # noqa: E731
                    return self._json(topology.compute(runtime.DATA_DIR / "mesh.db", qs.get("range", "7d"), station, describe))
                except ValueError as e:
                    return self._json({"error": str(e)}, 400)
                except Exception as e:  # noqa: BLE001
                    log.exception("topology failed")
                    return self._json({"error": f"topology failed: {e}"}, 500)
            if path == "/api/analytics/drive":
                try:
                    cache = {}
                    describe = lambda i: cache.setdefault(i, mesh.describe(i))  # noqa: E731
                    # no station picked: every station that moved (the local radio rarely does)
                    return self._json(drive.compute(runtime.DATA_DIR / "mesh.db", qs.get("range", "24h"), qs.get("station") or "*", describe,
                                                    int(qs.get("bin", 250) or 250)))
                except ValueError as e:
                    return self._json({"error": str(e)}, 400)
                except Exception as e:  # noqa: BLE001
                    log.exception("drive coverage failed")
                    return self._json({"error": f"drive coverage failed: {e}"}, 500)
            if path == "/api/analytics/reach":   # each fixed station's direct neighbours, and the radios none hears directly
                try:
                    cache = {}
                    describe = lambda i: cache.setdefault(i, mesh.describe(i))  # noqa: E731
                    return self._json(reach.compute(runtime.DATA_DIR / "mesh.db", qs.get("range", "30d"), qs.get("station") or "*", describe))
                except ValueError as e:
                    return self._json({"error": str(e)}, 400)
                except Exception as e:  # noqa: BLE001
                    log.exception("station reach failed")
                    return self._json({"error": f"station reach failed: {e}"}, 500)
            if path == "/api/analytics/replay":
                try:
                    cache = {}
                    describe = lambda i: cache.setdefault(i, mesh.describe(i))  # noqa: E731
                    return self._json(topology.replay(runtime.DATA_DIR / "mesh.db", qs.get("range", "24h"), station, describe))
                except ValueError as e:
                    return self._json({"error": str(e)}, 400)
                except Exception as e:  # noqa: BLE001
                    log.exception("replay failed")
                    return self._json({"error": f"replay failed: {e}"}, 500)
            if path in ("/api/analytics/node", "/api/analytics/node.csv"):
                nid, rng = qs.get("id", ""), qs.get("range", "7d")
                try:
                    if path.endswith(".csv"):
                        safe = re.sub(r"[^0-9a-zA-Z]", "", nid) or "node"
                        return self._send(node_analytics.node_csv(runtime.DATA_DIR / "mesh.db", nid, rng, station).encode("utf-8"),
                                          "text/csv; charset=utf-8",
                                          {"Content-Disposition": f'attachment; filename="mesh-{safe}-{rng}.csv"'})
                    cache = {}
                    describe = lambda i: cache.setdefault(i, mesh.describe(i))  # noqa: E731
                    return self._json(node_analytics.compute_node(runtime.DATA_DIR / "mesh.db", nid, rng, station, describe))
                except ValueError as e:
                    return self._json({"error": str(e)}, 404 if "no data" in str(e) else 400)
                except Exception as e:  # noqa: BLE001
                    log.exception("node analytics failed")
                    return self._json({"error": f"node analytics failed: {e}"}, 500)
            if path == "/api/sync":
                if syncer is None:
                    return self._json({"mode": "off"})
                st = dict(syncer.status if isinstance(syncer, sync_mod.Collector) else syncer.status())
                if access != "full":  # the hub's address and peers' hub ids are for this computer only
                    st.pop("hub", None)
                    st["stations"] = {k: {x: y for x, y in v.items() if x != "via"} for k, v in (st.get("stations") or {}).items()}
                return self._json(st)
            if path == "/api/stations":
                # every listening station that has logged packets
                rows = analytics.stations(runtime.DATA_DIR / "mesh.db")
                metas = station_meta()
                hub = (syncer.status()["stations"] if isinstance(syncer, sync_mod.Hub) else {})
                for r in rows:
                    here = r["id"] == mesh.local_id
                    m = station_report(mesh) if here else (metas.get(r["id"]) or {})
                    loc = analytics.station_location(r["id"])
                    r.update(name=station_name(r["id"]), current=here, location=list(loc) if loc else None,
                             report=m, lastContact=time.time() if here and mesh.connected else
                             (hub.get(r["id"]) or {}).get("last") or m.get("received"))
                return self._json({"stations": rows, "current": mesh.local_id, "software": SOFTWARE, "version": runtime.VERSION})
            if path == "/api/analytics":
                cache = {}
                describe = lambda nid: cache.setdefault(nid, mesh.describe(nid))  # noqa: E731
                try:
                    out = analytics.compute(runtime.DATA_DIR / "mesh.db", qs.get("range", "7d"), station, describe)
                    out.update(station=station, stationName=station_name(station), stationIsLocal=station == mesh.local_id,
                               combined=station == analytics.ALL_STATIONS, homeName=station_name(mesh.local_id),
                               stationNames=[station_name(s["id"]) for s in analytics.stations(runtime.DATA_DIR / "mesh.db")])
                    return self._json(out)
                except ValueError as e:
                    return self._json({"error": str(e)}, 400)
                except Exception as e:  # noqa: BLE001 - a bad query must not drop the connection
                    log.exception("analytics failed")
                    return self._json({"error": f"analytics failed: {e}"}, 500)
            if path.startswith("/api/insights/") or path in ("/api/packets/search", "/api/packets/search.csv",
                                                             "/api/alerts", "/api/settings"):
                cache = {}
                describe = lambda i: cache.setdefault(i, mesh.describe(i))  # noqa: E731
                dbp, rng = runtime.DATA_DIR / "mesh.db", qs.get("range", "7d")
                try:
                    if path == "/api/insights/estimates":
                        return self._json(insights.estimate_positions(dbp, station, runtime.BASE_ID, describe))
                    if path == "/api/insights/anomalies":   # steady radios behaving unlike themselves now
                        return self._json({"anomalies": anomalies.detect(dbp, station or analytics.ALL_STATIONS, describe), "baselineDays": anomalies.BASE_DAYS})
                    if path == "/api/insights/health":
                        return self._json(insights.health(dbp, rng, station or analytics.ALL_STATIONS, describe))
                    if path == "/api/insights/coverage":
                        return self._json(insights.coverage(dbp, rng, qs.get("node") or None, qs.get("relayed") == "1", station))
                    if path == "/api/insights/traceroutes":
                        return self._json(insights.traceroutes(dbp, rng, describe, station))
                    if path == "/api/packets/search":
                        if qs.get("keyflag"):
                            qs = dict(qs, keyflagNodes=keyflags.flags(dbp))
                        damaged = corruption.flags(dbp)["packets"]
                        if qs.get("corrupt"):
                            qs = dict(qs, corruptPackets=damaged)
                        r = packetsearch.search(dbp, qs.get("source", "packets"), qs, qs.get("limit", 200), qs.get("offset", 0), station)
                        flagged = keyflags.flags(dbp)
                        for row in r["rows"]:
                            if row.get("from_id") in flagged:
                                row["from_keyflag"] = flagged[row["from_id"]]["kind"]
                            if (d := damaged.get((row.get("from_id"), row.get("pkt_id")))):
                                row["corrupt"] = {"confidence": d["confidence"], "why": d["why"]}
                            for k in ("from_id", "to_id"):
                                if radioid.protocol_of(row.get(k)):
                                    row[k.replace("_id", "_name")] = describe(row[k])["name"]
                        return self._json(r)
                    if path == "/api/packets/search.csv":
                        src = qs.get("source", "packets")
                        if qs.get("keyflag"):
                            qs = dict(qs, keyflagNodes=keyflags.flags(dbp))
                        if qs.get("corrupt"):
                            qs = dict(qs, corruptPackets=corruption.flags(dbp)["packets"])
                        return self._send(packetsearch.search_csv(dbp, src, qs, station).encode("utf-8"), "text/csv; charset=utf-8",
                                          {"Content-Disposition": f'attachment; filename="mesh-{src}.csv"'})
                    if path == "/api/alerts":
                        return self._json(alert_engine.recent(int(qs.get("limit", 100))))
                    if path == "/api/settings":
                        return self._json(alert_engine.status())
                    return self._json({"error": "not found"}, 404)
                except ValueError as e:
                    return self._json({"error": str(e)}, 400)
                except Exception as e:  # noqa: BLE001
                    log.exception("%s failed", path)
                    return self._json({"error": f"{path} failed: {e}"}, 500)
            if path == "/api/storage":
                st = {**storage.status(), "demo": bool(runtime.DEMO)}
                if access != "full":  # file paths (they include the Windows user name) are for this computer only
                    names = {d: f"backup folder {i + 1}" for i, d in enumerate(st.get("dests") or [])}
                    st["dbPath"] = "mesh.db in Lorakeet's data folder"
                    st["dests"] = list(names.values())
                    lb = st.get("lastBackup")
                    if lb:
                        st["lastBackup"] = {**{k: v for k, v in lb.items() if k not in ("dests", "error")},
                                            **({"error": "failed"} if lb.get("error") else {}),
                                            "dests": [{"path": names.get(d.get("path"), "a backup folder"), "ok": d.get("ok"),
                                                       **({} if d.get("ok") else {"error": "failed"})}
                                                      for d in lb.get("dests") or []]}
                return self._json(st)
            if path == "/api/debuglog/sources":
                return self._json(mesh.debug_log.sources())
            if path == "/api/events":
                return self._sse()

            rel = "index.html" if path == "/" else path.lstrip("/")
            f = (runtime.STATIC / rel).resolve()
            if runtime.STATIC in f.parents and f.is_file():
                body = f.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", MIME.get(f.suffix, "application/octet-stream"))
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_error(404)

        def _setup(self, body):
            """Write lorakeet.toml from the setup page, then restart to apply it when something will start us
            again (the Windows supervisor, or systemd); otherwise ask the user to restart."""
            import config as cfgmod
            if CFG["_path"] is not None:
                return self._json({"error": "already configured: edit lorakeet.toml to change settings"}, 409)
            if body.get("joinCode"):
                try:
                    c = pairing.parse_code(body["joinCode"])
                except ValueError as e:
                    return self._json({"error": str(e)}, 400)
                result, _ = pairing.test_hubs(c["hubs"], c["token"], mesh.local_id)
                if not result["ok"] and not body.get("joinAnyway"):
                    return self._json({"error": f"Couldn't reach the hub: {result['hint']}", "hubTest": result}, 409)
                body = {**body, "hubUrl": result["url"] if result["ok"] else c["hubs"][0], "hubToken": c["token"]}
            try:
                path = cfgmod.write_setup(body)
            except FileExistsError as e:
                return self._json({"error": str(e)}, 409)
            except (ValueError, TypeError) as e:
                return self._json({"error": str(e)}, 400)
            supervised = bool(os.environ.get("LORAKEET_SUPERVISED") or os.environ.get("INVOCATION_ID"))
            log.info("setup: wrote %s%s", path, "; restarting to apply it" if supervised else "")
            self._json({"saved": True, "path": str(path), "restarting": supervised})
            if supervised:  # let the response go out, then exit: the supervisor or systemd starts us again
                threading.Timer(1.0, runtime.exit_now).start()

        def do_POST(self):  # noqa: N802
            # A collector station sending what it logged. The one POST that may come from another machine:
            # only with sync.mode = "hub", only from the allowed networks (Tailscale), only with the token.
            if urlparse(self.path).path in ("/api/ingest", "/api/ingest/hello"):  # hello: a station's connection test
                self._body_read = False
                try:
                    return self._ingest(hello=urlparse(self.path).path.endswith("/hello"))
                finally:
                    self._drain()
            if not self._host_ok():
                return self._wrong_host()
            # Every other POST changes something (sends on the mesh, changes settings, starts a backup), so
            # only this PC, or a LAN device that logged in, may make one; other LAN devices are view-only.
            access, base = self._access()
            path = urlparse(self.path).path
            # Only this page may send. A JSON content type forces a CORS preflight that we never
            # answer, and a foreign Origin is refused outright. From another device the request must name
            # its origin (browsers always do for these), on top of the login cookie being SameSite=Strict.
            origin = self.headers.get("Origin")
            if origin and urlparse(origin).netloc != self.headers.get("Host", ""):
                return self._json({"error": "cross-origin request refused"}, 403)
            if base != "full" and not origin:
                return self._json({"error": "cross-origin request refused"}, 403)
            if not (self.headers.get("Content-Type") or "").startswith("application/json"):
                return self._json({"error": "expected application/json"}, 415)
            if access is None:
                return self._json({"error": "only devices on the local network may connect"}, 403)
            if path in ("/api/login", "/api/logout"):
                return self._login(path)
            if access != "full":
                return self._json({"error": "view only: log in to send or change settings" if login.enabled
                                   else "view only: sending and settings work only on the dashboard PC"}, 403)
            if runtime.DEMO and _demo_refuses("POST", path):
                return self._json(DEMO_REFUSAL, 403)
            try:
                body = json.loads(self.rfile.read(self._length()) or b"{}")
                if path == "/api/setup":
                    if base != "full":
                        return self._json({"error": "setup is only available on the dashboard PC"}, 403)
                    return self._setup(body)
                if path == "/api/demo":  # the setup page's "Explore a demo first": start the demo beside this one
                    if base != "full":
                        return self._json({"error": "the demo starts from the dashboard PC"}, 403)
                    if runtime.DEMO:
                        return self._json({"url": "/"})
                    out = start_demo(CFG["http"]["port"] + 1)
                    return self._json(out, 200 if out.get("url") else 503)
                if path.startswith("/api/hub/"):
                    if base != "full":
                        return self._json({"error": "the Stations page is only available on the dashboard PC"}, 403)
                    return self._hub_post(path, body)
                if path == "/api/preferences":  # the Settings page: lorakeet.toml (this computer only, even logged in)
                    if base != "full":
                        return self._json({"error": "these settings are only available on the Lorakeet computer"}, 403)
                    return self._preferences_post(body)
                if path.startswith("/api/radio/"):
                    if base != "full":
                        return self._json({"error": "the Radio page is only available on the dashboard PC"}, 403)
                    return self._radio_post(path, body)
                if path == "/api/restart":  # the Settings panel's "Restart Lorakeet"
                    if not supervised_now():
                        return self._json({"error": "Lorakeet isn't running under its background runner, so it can't "
                                                    "restart itself: stop it and start it again"}, 409)
                    log.info("restart requested from the dashboard")
                    self._json({"ok": True})
                    threading.Timer(1.0, runtime.exit_now).start()
                    return None
                if path == "/api/logging":  # pause / resume logging (the tray icon, the dashboard's paused chip)
                    for link in (mesh, *getattr(mesh, "extras", ())):   # every radio's port is let go, or none
                        link.set_paused(bool(body.get("paused")))
                    return self._json({"paused": mesh.paused})
                if path == "/api/send":
                    return self._json(mesh.send_text(body.get("text"), body.get("to"), body.get("channel", 0)))
                if path == "/api/traceroute":
                    return self._json(mesh.traceroute(body.get("to")))
                if path == "/api/settings":
                    return self._json({"settings": alert_engine.update_settings(body)})
                if path == "/api/watch":
                    return self._json({"settings": alert_engine.set_watched(body.get("id", ""), bool(body.get("watched")))})
                if path == "/api/alerts/read":
                    alert_engine.mark_read()
                    return self._json({"ok": True})
                if path == "/api/backup":
                    if storage.running:
                        return self._json({"started": False, "error": "a backup is already running"}, 409)
                    threading.Thread(target=storage.backup, daemon=True, name="backup-now").start()
                    return self._json({"started": True})
                return self._json({"error": "not found"}, 404)
            except (ValueError, RuntimeError) as e:
                return self._json({"error": str(e)}, 400)
            except Exception as e:  # noqa: BLE001 - e.g. the radio was unplugged mid-send
                log.exception("send failed")
                return self._json({"error": f"radio error: {e}"}, 503)

        def _live_diagnostics(self):
            """What only the running Lorakeet knows, for the diagnostics bundle (diagnostics.build redacts it again)."""
            now = time.time()
            ago = lambda ts: round((now - ts) / 60, 1) if ts else None  # noqa: E731
            port = str(mesh.port or "")
            live = {"supervised": supervised_now(), "passwordSet": login.enabled,
                    "radio": {"connected": mesh.connected, "paused": mesh.paused,
                              "link": "network" if getattr(mesh, "host", None) else "usb" if port else None,
                              "debugLogLines": mesh.log_counts.get("lines"), "receptionsParsed": mesh.log_counts.get("mined")}}
            if storage:
                st = storage.status()
                live["storage"] = {"dbMB": round(st.get("dbBytes", 0) / 1e6, 1), "overLimit": st.get("overLimit"),
                                   "backupStale": st.get("backupStale")}
            if isinstance(syncer, sync_mod.Collector):
                c = syncer.status
                live["sync"] = {"mode": "collector", "lastOkMinutesAgo": ago(c.get("lastOk")), "lastError": c.get("lastError"),
                                "backlog": c.get("backlog"), "sent": c.get("sent")}
            elif isinstance(syncer, sync_mod.Hub):
                live["sync"] = {"mode": "hub", "stations": len(syncer.status().get("stations") or {})}
            if peering:
                live["peers"] = [{"lastOkMinutesAgo": ago(p.status.get("lastOk")), "lastError": p.status.get("lastError"),
                                  "backlog": p.status.get("backlog"), "paused": p.status.get("paused")}
                                 for p in list(peering.senders.values())]
            return live

        def _preferences(self):
            """The Settings page's view of lorakeet.toml: the values saved in the file and those running now (they
            differ until a restart), for the keys it may change, plus context. Never the sync token or paths beyond the
            data folder and the file itself."""
            import config as cfgmod
            try:
                saved = cfgmod.file_settings()
            except (ValueError, OSError) as e:  # a hand edit broke the file: say so, change nothing
                return {"error": f"lorakeet.toml can't be read: {e}"}
            pick = lambda c: {s: {k: c[s][k] for k in keys} for s, keys in PREFS_EDITABLE.items()}  # noqa: E731
            return {"saved": pick(saved), "running": pick(CFG), "configured": CFG["_path"] is not None,
                    "path": str(cfgmod.config_path()), "dataDir": str(runtime.DATA_DIR), "passwordSet": login.enabled,
                    "supervised": supervised_now(), "version": runtime.VERSION, "syncMode": saved["sync"]["mode"],
                    "ports": [p["device"] for p in serial_ports() if p.get("radio")]}

        def _preferences_post(self, body):
            """Save the Settings page's lorakeet.toml changes, all at once ({section: {key: value}}, only PREFS_EDITABLE;
            validated like a hand edit; most apply after a restart, which the page offers)."""
            import config as cfgmod
            if CFG["_path"] is None:
                return self._json({"error": "finish setting Lorakeet up first (the setup page)"}, 409)
            changes = body.get("changes") or {}
            if not isinstance(changes, dict):
                return self._json({"error": "changes must be {section: {key: value}}"}, 400)
            for sec, values in changes.items():
                if sec not in PREFS_EDITABLE or not isinstance(values, dict) or any(k not in PREFS_EDITABLE[sec] for k in values):
                    return self._json({"error": f"{sec}: not a setting this page changes"}, 400)
            try:
                path_ = cfgmod.update_sections(changes)
            except ValueError as e:
                return self._json({"error": str(e).replace("lorakeet.toml: ", "")}, 400)
            log.info("settings saved to %s: %s", path_, ", ".join(f"{s}.{k}" for s, v in changes.items() for k in v))
            mesh.event("settings_saved", keys=[f"{s}.{k}" for s, v in changes.items() for k in v])
            return self._json({"ok": True, **self._preferences()})

        def _login(self, path):
            token = login_mod.cookie_value(self.headers.get("Cookie"))
            if path == "/api/logout":
                login.logout(token)
                return self._json_cookie({"ok": True}, login_mod.clear_cookie())
            if not login.enabled:
                return self._json({"error": "no password is set: run  python server.py --set-password  on the dashboard PC"}, 400)
            try:
                body = json.loads(self.rfile.read(min(self._length(), 4096)) or b"{}")
            except ValueError:
                return self._json({"error": "bad request"}, 400)
            ip = self.client_address[0]
            try:
                new = login.login(ip, body.get("password") if isinstance(body, dict) else None)
            except login_mod.LockedOut as e:
                log.warning("login from %s refused: locked out for %d s", ip, e.wait_s)
                return self._json({"error": str(e), "waitS": e.wait_s}, 429)
            if not new:
                log.warning("login from %s: wrong password", ip)
                return self._json({"error": "wrong password"}, 401)
            log.info("login from %s", ip)
            return self._json_cookie({"ok": True}, login_mod.set_cookie(new))

        def _json_cookie(self, obj, cookie):
            body = json.dumps(obj).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Set-Cookie", cookie)
            self.end_headers()
            self.wfile.write(body)

        def _sse(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            q = mesh.subscribe()
            try:
                self.wfile.write(f"event: status\ndata: {json.dumps(mesh.status())}\n\n".encode())
                self.wfile.flush()
                while True:
                    try:
                        msg = q.get(timeout=15)
                    except queue.Empty:
                        msg = ": ping\n\n"
                    self.wfile.write(msg.encode())
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass
            finally:
                mesh.unsubscribe(q)

    return Handler


def _login_page_asset(path):
    """What a device that hasn't logged in yet may load (lan = "login"): the login page and the files it uses."""
    return path in ("/login.html", "/app.css", "/favicon.svg", "/favicon.ico", "/apple-touch-icon.png",
                    "/icon-192.png", "/icon-512.png", "/site.webmanifest")


def _lib_version():
    try:
        from importlib.metadata import version
        return version("meshtastic")
    except Exception:  # noqa: BLE001
        return None


MAX_CONNECTIONS = 64  # concurrent requests from other machines; live pages hold one each (the event stream)
MAX_PER_ADDRESS = 8   # ... and from any one of them, so one device can't take every slot
MAX_LOCAL = 64        # this computer has its own allowance: other machines can never lock it out


class ExclusiveHTTPServer(ThreadingHTTPServer):
    # HTTPServer sets SO_REUSEADDR, which on Windows lets a second process bind the same port and silently share it:
    # there, exclusive binding makes a second instance fail fast instead. On Linux it means something else, and is
    # needed: without it, a restart within a minute of any client connection (a station's fleet agent polls every
    # minute) failed with "address in use" until the old connections' TIME_WAIT ran out. It never lets two
    # processes listen on one port there (that's SO_REUSEPORT), so a second instance still fails fast.
    allow_reuse_address = os.name != "nt"
    # How many connections may wait to be accepted. Python's default is 5, and a page opens more than that at once:
    # Windows refuses the overflow outright ("connection refused"; Linux makes them retry), which showed up as an
    # occasional failed request on a busy page and a browser test that failed now and then.
    request_queue_size = 128

    def __init__(self, *a, **k):
        self._conn_lock, self._conns, self._remote = threading.Lock(), {}, 0
        super().__init__(*a, **k)

    def _admit(self, ip):
        """A slot for this connection, or False: addresses that could never be served are closed at once, other
        machines share MAX_CONNECTIONS with at most MAX_PER_ADDRESS each, this computer has MAX_LOCAL of its own."""
        local = client_access(ip) == "full"
        if not local and client_access(ip) is None and not (
                CFG["sync"]["mode"] == "hub" and sync_mod.allowed_source(ip, CFG["sync"]["allow"])):
            return False
        with self._conn_lock:
            n = self._conns.get(ip, 0)
            if n >= (MAX_LOCAL if local else MAX_PER_ADDRESS) or (not local and self._remote >= MAX_CONNECTIONS):
                return False
            self._conns[ip] = n + 1
            self._remote += 0 if local else 1
        return True

    def _release(self, ip):
        with self._conn_lock:
            self._conns[ip] -= 1
            if not self._conns[ip]:
                del self._conns[ip]
            self._remote -= 0 if client_access(ip) == "full" else 1

    def process_request(self, request, client_address):
        """Refuse (close) a connection over its limit instead of starting yet another thread."""
        ip = client_address[0]
        if not self._admit(ip):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:  # noqa: BLE001
            self._release(ip)
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._release(client_address[0])

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def bind_server(addr, handler, wait_s=45):
    """ExclusiveHTTPServer on addr. On Linux, wait out "address in use" for up to wait_s: connections that were
    still open when the previous Lorakeet stopped linger half-closed for up to a minute, and SO_REUSEADDR doesn't
    cover those, so a restart right after one failed, over and over (systemd retries every 10 s, and gives up after
    ten failures in five minutes). On Windows a busy port means another Lorakeet is running, and the tray's
    supervisor relies on failing at once (EXIT_PORT_IN_USE)."""
    deadline = time.time() + (wait_s if os.name != "nt" else 0)
    while True:
        try:
            return ExclusiveHTTPServer(addr, handler)
        except OSError as e:
            if e.errno != errno.EADDRINUSE or time.time() >= deadline:
                raise
            if deadline - time.time() > wait_s - 2:
                log.warning("port %d busy (old connections closing?); waiting up to %d s", addr[1], wait_s)
            time.sleep(1)


DEMO_REFUSAL = {"error": "Not available in the demo: the Radio, Stations and setup pages change your real radio and "
                         "Lorakeet's settings. Open your own Lorakeet for those.", "demo": True}


def _demo_refuses(method, path):
    """The demo serves a made-up mesh, but its process can still reach this computer's radios and lorakeet.toml:
    every page-tier endpoint (radio settings, channels, pairing, peering, setup) is refused there."""
    e = api_index.find(method, path)
    return bool(e and e["tier"] == "page" and path != "/api/demo") or path.startswith(("/api/radio", "/api/hub"))


def start_demo(port, wait_s=60, tries=10):
    """Start `server.py --demo` on the first free port from `port` up (or find the one already running) and wait
    for it. {url} or {error}."""
    import subprocess
    import urllib.request

    def probe(p):
        """True = the demo answers there, False = something else holds the port, None = free."""
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{p}/api/whoami", timeout=2) as r:
                return bool(json.loads(r.read()).get("demo"))
        except urllib.error.HTTPError:
            return False
        except Exception:  # noqa: BLE001
            with socket.socket() as sk:
                return False if sk.connect_ex(("127.0.0.1", p)) == 0 else None
    for p in range(port, port + tries):
        state = probe(p)
        if state:
            return {"url": f"http://127.0.0.1:{p}/"}
        if state is None:
            break
    else:
        return {"error": f"ports {port}-{port + tries - 1} are all taken"}
    cmd = [sys.executable, str(runtime.HERE / "server.py"), "--demo", "--http", str(p)]
    # not supervised, whatever this process is: the demo's Restart must say it can't, not exit for good
    env = {k: v for k, v in os.environ.items() if k not in ("LORAKEET_SUPERVISED", "INVOCATION_ID")}
    if sys.platform == "win32":
        pyw = Path(sys.executable).with_name("pythonw.exe")
        cmd[0] = str(pyw if pyw.exists() else sys.executable)
        subprocess.Popen(cmd, cwd=runtime.HERE, env=env, close_fds=True, creationflags=subprocess.DETACHED_PROCESS
                         | subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW)
    else:
        subprocess.Popen(cmd, cwd=runtime.HERE, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    end = time.time() + wait_s
    while time.time() < end:
        if probe(p):
            return {"url": f"http://127.0.0.1:{p}/"}
        time.sleep(0.5)
    return {"error": f"the demo didn't start (see {runtime.DATA_DIR / 'demo' / 'server.log'})"}
