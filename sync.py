"""Station sync: a remote listening station (collector) sends what it logged to the home server (hub).

The collector runs this same server against its own radio and logs to its own mesh.db, so an internet or
hub outage loses nothing. Every `interval_s` it sends its rows home, table by table in rowid order, in
gzip'd JSON batches, and only advances its bookmark (settings 'sync_progress') once the hub has stored
them. The hub inserts with INSERT OR IGNORE on a unique (station, src_rowid) key, so a resent batch never
duplicates anything. Transport: plain HTTP over a private Tailscale network (encrypted end to end by
WireGuard), with a shared token. Nothing is exposed to the internet at either end.

Only station-tagged tables sync, and a collector only sends rows its own station logged. The firmware
debug log (debug.db) stays on the collector; everything mined from it (rx_hops, tx_log) syncs.

Peering (PeerSender): a hub also sends to another hub what its stations logged, filtered by what that peer may see
(SHARE_LEVELS). Rows keep their origin station and their origin row number (src_rowid, or the rowid where they were
logged), so the same row arriving by two routes is stored once.
"""
import gzip
import hmac
import ipaddress
import json
import re
import logging
import threading
import time
import urllib.error
import urllib.request

from analytics import STATION_RE, STATION_TABLES

log = logging.getLogger("lorakeet.sync")
BATCH_ROWS = 500
MAX_BODY = 20 * 1024 * 1024        # compressed request body the hub will read
MAX_JSON = 50 * 1024 * 1024        # and what it may decompress to (a batch is <= 2,500 rows)
PROTOCOL = 1


# ---------------------------------------------------------------- collector

class Collector:
    def __init__(self, store, hub_url, token, interval_s, report=None):
        self.store, self.url, self.token, self.interval = store, hub_url.rstrip("/") + "/api/ingest", token, interval_s
        self.report = report  # () -> station report dict (name, location, health); sent every pass
        self.status = {"mode": "collector", "hub": hub_url, "lastOk": None, "lastError": None, "lastErrorAt": None,
                       "sent": 0, "backlog": None}
        self._wake = threading.Event()

    def start(self):
        threading.Thread(target=self._run, daemon=True, name="sync").start()

    def sync_now(self):
        self._wake.set()

    def _run(self):
        while True:
            try:
                self.sync_once()
            except Exception as e:  # noqa: BLE001 - the loop must survive anything; the next pass retries
                log.exception("sync pass failed")
                self._error(str(e))
            self._wake.wait(self.interval)
            self._wake.clear()

    def _progress(self):
        r = self.store.query("SELECT value FROM settings WHERE key='sync_progress'")
        return json.loads(r[0]["value"]) if r else {}

    def _save_progress(self, p):
        self.store.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('sync_progress', ?)", json.dumps(p))

    def _error(self, msg):
        self.status.update(lastError=msg, lastErrorAt=time.time())

    def sync_once(self):
        """Send every table's unsent rows. Returns rows sent this pass."""
        station = self.store.station
        if not station:
            return 0  # radio not connected yet: we don't know who we are
        progress, sent = self._progress(), 0
        for table in STATION_TABLES:
            while True:
                last = progress.get(table, 0)
                rows = self.store.query(f"SELECT rowid AS src_rowid, * FROM {table} WHERE rowid > ? AND station = ? "
                                        f"ORDER BY rowid LIMIT {BATCH_ROWS}", last, station)
                if not rows:
                    break
                for r in rows:
                    r.pop("received_via", None)
                acked = self._send(station, table, rows)
                progress[table] = acked
                self._save_progress(progress)
                sent += len(rows)
                if len(rows) < BATCH_ROWS:
                    break
        backlog = self.backlog(progress, station)
        if self.report:  # every pass, rows or not: the station report doubles as a heartbeat
            self._post({"protocol": PROTOCOL, "station": station, "meta": {**self.report(), "backlog": sum(backlog.values())}})
        self.status.update(lastOk=time.time(), lastError=None, sent=self.status["sent"] + sent, backlog=backlog)
        return sent

    def backlog(self, progress=None, station=None):
        progress, station = progress or self._progress(), station or self.store.station
        return {t: n for t in STATION_TABLES
                if (n := self.store.query(f"SELECT COUNT(*) AS n FROM {t} WHERE rowid > ? AND station = ?",
                                          progress.get(t, 0), station)[0]["n"])}

    def _send(self, station, table, rows):
        reply = self._post({"protocol": PROTOCOL, "station": station, "table": table, "rows": rows}, f"{table} batch")
        acked = reply.get("acked")
        if acked != rows[-1]["src_rowid"]:
            raise RuntimeError(f"hub acknowledged {acked!r}, expected {rows[-1]['src_rowid']}")
        return acked

    def _post(self, payload, what="station report"):
        body = gzip.compress(json.dumps(payload).encode())
        req = urllib.request.Request(self.url, data=body, method="POST", headers={
            "Content-Type": "application/json", "Content-Encoding": "gzip", "Authorization": f"Bearer {self.token}",
            "X-Lorakeet-Station": payload.get("station") or ""})
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"hub refused {what}: HTTP {e.code} {e.read()[:200].decode(errors='replace')}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise RuntimeError(f"hub unreachable: {getattr(e, 'reason', e)}") from None


# ---------------------------------------------------------------- peering: what a peer may see

PEER_TABLES = tuple(t for t in STATION_TABLES if t != "events")  # a station's own operational log stays home
SHARE_LEVELS = {
    # text on public channels, not on private channels or in direct messages (those packets go, without their text)
    "default": {"tables": PEER_TABLES, "text": "public"},
    "everything": {"tables": PEER_TABLES, "text": "all"},
    # what was heard where and how well: no message text at all
    "receptions": {"tables": tuple(t for t in PEER_TABLES if t != "messages"), "text": "none"},
}
TEXT_PORTS = ("TEXT_MESSAGE_APP", "TEXT_MESSAGE_COMPRESSED_APP", "DETECTION_SENSOR_APP", "ALERT_APP")

# Every location a peer gets is rounded to this grid (~3 km; Meshtastic's own "precision 14"), whatever its source:
# position rows (a moving station's own GPS track included), position / map-report / waypoint packets and their
# summaries, node-database snapshots and station reports. Peers see roughly where things are, never a home or a route.
PEER_GRID_DEG = 0.03
PEER_PRECISION_BITS = 14
_DEG_KEYS = {"latitude", "longitude", "lat", "lon"}
_INT_KEYS = {"latitudeI", "longitudeI", "latitude_i", "longitude_i"}


def coarse(v):
    """A coordinate in degrees, rounded to the peer grid."""
    return round(round(float(v) / PEER_GRID_DEG) * PEER_GRID_DEG, 4)


def coarsen(obj):
    """Round every coordinate inside a packet / snapshot JSON structure to the peer grid (in place; returns obj)."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in _DEG_KEYS and isinstance(v, (int, float)) and not isinstance(v, bool):
                obj[k] = coarse(v)
            elif k in _INT_KEYS and isinstance(v, int) and not isinstance(v, bool):
                obj[k] = int(round(coarse(v / 1e7) * 1e7))
            elif k == "precisionBits" and isinstance(v, int):
                obj[k] = min(v or 32, PEER_PRECISION_BITS)
            else:
                coarsen(v)
    elif isinstance(obj, list):
        for v in obj:
            coarsen(v)
    return obj


_LATLON = re.compile(r"(-?\d{1,3}\.\d+),\s*(-?\d{1,3}\.\d+)")


def _coarse_row(table, row):
    r = dict(row)
    if table == "positions":
        for k in ("lat", "lon"):
            if r.get(k) is not None:
                r[k] = coarse(r[k])
        r["precision_bits"] = min(r.get("precision_bits") or 32, PEER_PRECISION_BITS)
    elif table == "packets":
        if r.get("raw"):
            try:
                d = coarsen(json.loads(r["raw"]))
                # the payload is the packet's own bytes, base64: a position's exact coordinates are still in there
                # (and a waypoint's, or a range test's text). The decoded fields above carry everything else.
                dec = d.get("decoded")
                if isinstance(dec, dict) and "payload" in dec:
                    del dec["payload"]
                    dec["payloadDropped"] = "locations rounded"
                r["raw"] = json.dumps(d)
            except ValueError:
                r["raw"] = None
        if r.get("summary"):
            r["summary"] = _LATLON.sub(lambda m: f"{coarse(m.group(1)):.2f}, {coarse(m.group(2)):.2f}", r["summary"])
    elif table == "nodedb_snapshots" and r.get("data"):
        try:
            r["data"] = json.dumps(coarsen(json.loads(r["data"])))
        except ValueError:
            r["data"] = None
    return r


PEER_REPORT_FIELDS = ("name", "version", "mobile", "radioFirmware", "radioHw")


def coarse_report(meta, exact=False):
    """A station report as a peer gets it: its name, version and radio, and the location rounded to the peer grid.
    Never the machine's details (platform, uptime, disk, temperature, power flags, code fingerprint, backlog, note)."""
    m = {k: v for k, v in (meta or {}).items() if k in PEER_REPORT_FIELDS}
    loc = (meta or {}).get("location")
    if isinstance(loc, (list, tuple)) and len(loc) >= 2:
        m["location"] = list(loc) if exact else [coarse(loc[0]), coarse(loc[1])]
    return m


# Ports whose decoded contents a peer may see (default / receptions) when the packet is proven to have been broadcast
# on a public channel. Everything else (other apps' payloads, range-test text, store-and-forward history, waypoint
# names, anything on a private channel or unproven) goes as a bare reception: portnum and radio fields, no payload.
OPEN_PORTS = ("POSITION_APP", "NODEINFO_APP", "TELEMETRY_APP", "NEIGHBORINFO_APP", "MAP_REPORT_APP", "ROUTING_APP")


def public_keys(store, public_hashes, rows):
    """{(from_id, pkt_id)} among `rows` proven broadcast on a public channel: an over-the-air copy logged by the SAME
    station carries a public channel's fingerprint, and the row itself is a broadcast, not a direct (PKI) message.
    Copies from other stations don't count (a peer could plant them). No copy, no proof: private.
    public_hashes: a set for every station, or station -> set (each station judged by its own radio's channels)."""
    out = set()
    by_station = {}
    for r in rows:
        if r.get("pkt_id") is None or r.get("to_id") not in (None, "^all") or r.get("pki"):
            continue
        by_station.setdefault(r.get("station"), set()).add((r.get("from_id"), r.get("pkt_id")))
    if not by_station or not public_hashes:
        return out
    for st, keys in by_station.items():
        hashes = sorted(public_hashes(st) if callable(public_hashes) else public_hashes)
        if not hashes:
            continue
        hm = ",".join("?" * len(hashes))
        pids = sorted({k[1] for k in keys})
        for i in range(0, len(pids), 400):  # one query per 400 packets, not one per packet
            chunk = pids[i:i + 400]
            for r in store.query(f"SELECT DISTINCT from_id, pkt_id FROM rx_hops WHERE station = ? AND directed = 0 "
                                 f"AND channel IN ({hm}) AND pkt_id IN ({','.join('?' * len(chunk))})",
                                 st, *hashes, *chunk):
                if (r["from_id"], r["pkt_id"]) in keys:
                    out.add((r["from_id"], r["pkt_id"]))
    return out


public_text_keys = public_keys  # the earlier name


def _withhold(row, text):
    """A packet row as a bare reception: no payload (and, for text ports, a note that the words were withheld)."""
    r = dict(row)
    try:
        d = json.loads(r.get("raw") or "{}")
        dec = d.get("decoded") or {}
        d["decoded"] = {"portnum": dec.get("portnum"), ("textWithheld" if text else "payloadWithheld"): True} if dec else {}
        r["raw"] = json.dumps(d)
    except ValueError:
        r["raw"] = None
    r["summary"] = "(text not shared)" if text else "(not shared)"
    return r


def _strip_text(row):
    return _withhold(row, True)


def share_rows(store, table, rows, level, public_hashes, exact=False):
    """The rows of one batch a peer may see, as they may see them: contents by the share level, every location rounded
    to the peer grid unless the peer may see exact locations. Returns the filtered (and edited) rows."""
    rows = _share_text(store, table, rows, level, public_hashes)
    return rows if exact else [_coarse_row(table, r) for r in rows]


def _share_text(store, table, rows, level, public_hashes):
    rule = SHARE_LEVELS[level]
    if table not in rule["tables"]:
        return []
    if rule["text"] == "all":
        return rows
    if table == "packets":
        public = public_keys(store, public_hashes, rows)
        out = []
        for r in rows:
            port, proven = r.get("portnum"), (r.get("from_id"), r.get("pkt_id")) in public
            if port in TEXT_PORTS:
                out.append(r if proven and rule["text"] == "public" else _withhold(r, True))
            elif port in OPEN_PORTS and proven:
                out.append(r)
            else:
                out.append(_withhold(r, False))
        return out
    if table == "messages":
        public = public_keys(store, public_hashes, rows) if rule["text"] == "public" else set()
        return [r for r in rows if r.get("to_id") == "^all" and (r.get("from_id"), r.get("pkt_id")) in public]
    return rows


class PeerSender:
    """Sends to one peer hub what this hub's stations logged (and, if `forward`, what other peers sent here), in
    batches per station and table, bookmarked in settings 'peer_progress'. Never sends back to a hub what came from
    that hub. peer: {id, url, token, share, forward, hubId}."""

    def __init__(self, store, peer, my_hub_id, public_hashes, reports, interval_s=60):
        self.store, self.peer, self.my_hub_id = store, dict(peer), my_hub_id
        self.public_hashes = public_hashes   # () -> station -> set of public channel fingerprints (or one set)
        self.reports = reports               # station -> report dict for that station (or None)
        self.interval = interval_s
        self.status = {"lastOk": None, "lastError": None, "lastErrorAt": None, "sent": 0, "backlog": None,
                       "paused": bool(peer.get("paused"))}
        self._wake, self._stop = threading.Event(), threading.Event()

    def start(self):
        threading.Thread(target=self._run, daemon=True, name=f"peer-{self.peer['id']}").start()

    def stop(self):
        self._stop.set()
        self._wake.set()

    def sync_now(self):
        self._wake.set()

    def _run(self):
        while not self._stop.is_set():
            self.tick()
            self._wake.wait(self.interval)
            self._wake.clear()

    def tick(self):
        """One pass, unless paused (Stations -> Peers -> Pause): then nothing is sent and the bookmarks stay, so it
        catches up on resume. Errors are recorded for the page; the next pass retries."""
        self.status["paused"] = bool(self.peer.get("paused"))
        if self.status["paused"]:
            return
        try:
            self.sync_once()
        except Exception as e:  # noqa: BLE001 - next pass retries
            log.warning("peer %s: %s", self.peer.get("name") or self.peer["id"], e)
            self.status.update(lastError=str(e), lastErrorAt=time.time())

    def stations(self):
        """Stations whose data this peer may get: everything here that didn't arrive from a peer, plus (forward)
        what other peers sent, never what came from this peer itself."""
        r = self.store.query("SELECT value FROM settings WHERE key='sync_hub'")
        hub = json.loads(r[0]["value"]) if r else {}
        mine = {x["station"] for x in self.store.query("SELECT DISTINCT station FROM packets WHERE station IS NOT NULL")}
        mine |= set(hub)
        out = []
        for st in sorted(mine):
            via = (hub.get(st) or {}).get("via")
            if via and (not self.peer.get("forward") or via == self.peer.get("hubId")):
                continue
            out.append(st)
        return out

    def _progress(self):
        r = self.store.query("SELECT value FROM settings WHERE key='peer_progress'")
        return (json.loads(r[0]["value"]) if r else {}).get(self.peer["id"], {})

    def _save_progress(self, mine):
        with self.store.lock:
            r = self.store.db.execute("SELECT value FROM settings WHERE key='peer_progress'").fetchone()
            allp = json.loads(r[0]) if r else {}
            allp[self.peer["id"]] = mine
            self.store.db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('peer_progress', ?)", (json.dumps(allp),))
            self.store.db.commit()

    def sync_once(self):
        level = self.peer.get("share") or "default"
        tables = SHARE_LEVELS[level]["tables"]
        progress, sent = self._progress(), 0
        hashes = self.public_hashes()
        refused = {}
        for st in self.stations():
            try:
                sent += self._sync_station(st, progress, tables, level, hashes)
            except PeerRefused as e:  # that station only: the others still go, and this one is retried next pass
                refused[st] = str(e)
        self.status.update(lastOk=time.time(), lastError=None, sent=self.status["sent"] + sent, backlog=self.backlog(progress),
                           refused=refused)
        return sent

    def _sync_station(self, st, progress, tables, level, hashes):
        sent = 0
        p = progress.setdefault(st, {})
        for table in tables:
            while True:
                last = p.get(table, 0)
                rows = self.store.query(f"SELECT rowid AS _rowid, * FROM {table} WHERE station = ? AND rowid > ? "
                                        f"ORDER BY rowid LIMIT {BATCH_ROWS}", st, last)
                if not rows:
                    break
                top = rows[-1]["_rowid"]
                for r in rows:  # the origin row number travels with the row, so every route dedupes alike
                    r["src_rowid"] = r["src_rowid"] if r.get("src_rowid") is not None else r["_rowid"]
                    del r["_rowid"]
                rows.sort(key=lambda r: r["src_rowid"])
                for r in rows:
                    r.pop("received_via", None)  # how it reached THIS hub is no business of the peer's
                send = share_rows(self.store, table, rows, level, hashes, exact=bool(self.peer.get("exact")))
                if send:
                    self._post({"protocol": PROTOCOL, "station": st, "table": table, "rows": send}, st)
                    sent += len(send)
                p[table] = top
                self._save_progress(progress)
                if len(rows) < BATCH_ROWS:
                    break
        rep = self.reports(st)
        if rep:
            self._post({"protocol": PROTOCOL, "station": st, "meta": coarse_report(rep, bool(self.peer.get("exact")))}, st)
        return sent

    def backlog(self, progress=None):
        progress = progress or self._progress()
        n = 0
        for st in self.stations():
            for table in SHARE_LEVELS[self.peer.get("share") or "default"]["tables"]:
                n += self.store.query(f"SELECT COUNT(*) AS n FROM {table} WHERE station = ? AND rowid > ?",
                                      st, (progress.get(st) or {}).get(table, 0))[0]["n"]
        return n

    def _post(self, payload, station):
        body = gzip.compress(json.dumps(payload).encode())
        req = urllib.request.Request(self.peer["url"].rstrip("/") + "/api/ingest", data=body, method="POST", headers={
            "Content-Type": "application/json", "Content-Encoding": "gzip", "Authorization": f"Bearer {self.peer['token']}",
            "X-Lorakeet-Station": station, "X-Lorakeet-Hub": self.my_hub_id})
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            msg = e.read()[:200].decode(errors="replace")
            if e.code == 409 and "own radio" in msg:  # the peer's own radio: it has those rows already
                return {}
            if e.code == 409:  # the peer won't take this station (another source has it): not sent, kept pending
                raise PeerRefused(f"{station}: {msg}") from None
            raise RuntimeError(f"peer refused: HTTP {e.code} {msg}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise RuntimeError(f"peer unreachable: {getattr(e, 'reason', e)}") from None


# ---------------------------------------------------------------- station reports

REPORT_FIELDS = {  # field -> type; anything else is dropped
    "name": str, "note": str, "location": list, "software": str, "version": str, "radioFirmware": str, "radioHw": str,
    "uptimeS": (int, float), "throttled": str, "tempC": (int, float), "diskFreeMB": (int, float),
    "backlog": int, "platform": str, "mobile": bool, "publicHashes": list, "privateHashes": list,
}


def clean_report(meta):
    if not isinstance(meta, dict):
        raise IngestError(400, "meta must be an object")
    out = {}
    for k, t in REPORT_FIELDS.items():
        v = meta.get(k)
        if v is None or isinstance(v, bool) != (t is bool) or not isinstance(v, t):
            continue
        if isinstance(v, str):
            v = v[:120]
        if k in ("publicHashes", "privateHashes"):  # channel fingerprints: one byte each, a radio has at most 8
            if len(v) > 8 or not all(isinstance(x, int) and not isinstance(x, bool) and 0 <= x <= 255 for x in v):
                continue
        if k == "location":
            if not (len(v) in (2, 3) and all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in v)
                    and -90 <= v[0] <= 90 and -180 <= v[1] <= 180):
                continue
            v = [float(x) for x in v]
        out[k] = v
    return out


# ---------------------------------------------------------------- hub

class PeerRefused(Exception):
    """A peer hub won't take one station's rows (409: it has that station from another source). Kept pending."""


class IngestError(Exception):
    def __init__(self, status, msg):
        super().__init__(msg)
        self.status = status


def allowed_source(ip, networks):
    """Loopback, or an address inside one of the configured networks (default: Tailscale's ranges)."""
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if isinstance(a, ipaddress.IPv6Address) and a.ipv4_mapped:
        a = a.ipv4_mapped
    return a.is_loopback or any(a in ipaddress.ip_network(n) for n in networks)


def check_token(header, token):
    given = (header or "").removeprefix("Bearer ").strip()
    return bool(token) and hmac.compare_digest(given.encode(), token.encode())


def _strip_secrets(detail):
    """A logged config snapshot (JSON) minus the Bluetooth PIN, Wi-Fi password and MQTT password."""
    try:
        d = json.loads(detail)
    except (TypeError, ValueError):
        return detail
    for path in (("config", "bluetooth", "fixedPin"), ("config", "network", "wifiPsk"), ("module_config", "mqtt", "password")):
        node = d
        for k in path[:-1]:
            node = node.get(k) if isinstance(node, dict) else None
        if isinstance(node, dict):
            node.pop(path[-1], None)
    return json.dumps(d)


def station_tokens(entries):
    """sync.station_tokens ["!id:token", ...] -> {station id: token}."""
    out = {}
    for e in entries or []:
        sid, _, tok = str(e).partition(":")
        out[sid] = tok
    return out


def follow_tokens(per_station, aliases):
    """Per-station tokens keyed by radio number: after a 2.8 renumbering the station's token also answers to its
    new number (aliases: old -> new)."""
    return {**per_station, **{aliases[sid]: tok for sid, tok in per_station.items() if sid in aliases}}


def authorize(header, station, shared_token, per_station, require_own):
    """Which token a request must carry. A station with its own token must use it (the shared one is no
    longer enough for it, so one stolen station can be cut off alone); others may use the shared token
    unless require_own. Returns the station the request is bound to, or None if refused."""
    if station in per_station:
        return station if check_token(header, per_station[station]) else None
    if require_own:
        return None
    return station if check_token(header, shared_token) else None


def decode_body(raw, encoding):
    if len(raw) > MAX_BODY:
        raise IngestError(413, "batch too large")
    if (encoding or "").lower() == "gzip":
        d = gzip.GzipFile(fileobj=__import__("io").BytesIO(raw))
        raw = d.read(MAX_JSON + 1)
        if len(raw) > MAX_JSON:
            raise IngestError(413, "batch too large once decompressed")
    try:
        return json.loads(raw)
    except ValueError:
        raise IngestError(400, "not JSON") from None


NODE_COLUMNS = {"from_id", "to_id", "node", "target", "a", "b"}
JSON_COLUMNS = {"raw", "forward", "back", "data", "detail"}
NODE_RE = re.compile(r"![0-9a-f]{8}|\^all")
TEXT_MAX = {"raw": 256 * 1024, "data": 4 * 1024 * 1024, "detail": 256 * 1024, "forward": 64 * 1024, "back": 64 * 1024}
TEXT_MAX_DEFAULT = 4096
MIN_TS = 1.5e9  # 2017: a timestamp before this is nonsense (and ts=0 could pose as "first seen")


def _check_value(table, col, kind, v, now):
    """Raise IngestError unless `v` fits the column: a number in a number column, a bounded string in a text column,
    a node id in a node column, JSON in a JSON column. SQLite would store anything anywhere; the pages trust types."""
    if v is None:
        return
    where = f"{table}.{col}"
    if kind in ("INTEGER", "REAL"):
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v or v in (float("inf"), float("-inf")):
            raise IngestError(400, f"{where} must be a number")
        if col == "ts" and not (MIN_TS <= v <= now + 86400):
            raise IngestError(400, f"{where} is out of range")
        return
    if not isinstance(v, str):
        raise IngestError(400, f"{where} must be text")
    if len(v) > TEXT_MAX.get(col, TEXT_MAX_DEFAULT):
        raise IngestError(400, f"{where} is too long")
    if col in NODE_COLUMNS and not NODE_RE.fullmatch(v):
        raise IngestError(400, f"{where} must be a node id")
    if col in JSON_COLUMNS:
        try:
            json.loads(v)
        except ValueError:
            raise IngestError(400, f"{where} must be JSON") from None


class Hub:
    """Stores collectors' batches. Status per station lives in settings 'sync_hub'; who may write each station in
    settings 'station_owners' (see claim)."""

    def __init__(self, store, on_rows=None, on_meta=None, is_revoked=None):
        self.store = store
        self.is_revoked = is_revoked or (lambda owner: False)  # owner tag -> its pairing is revoked
        self.cols = {}
        self.lock = threading.Lock()
        self.on_rows = on_rows  # called (station, table, src_rowids) after a batch is stored: the live map
        self.on_meta = on_meta  # called (station, report) after a station report is stored

    def _columns(self, table):
        if table not in self.cols:
            self.cols[table] = {r["name"]: (r["type"] or "").upper() for r in self.store.query(f"PRAGMA table_info({table})")}
        return set(self.cols[table])

    # -- who may write a station
    def own_stations(self):
        """This hub's own radio: the connected one, and the last one it had (persisted, so it's protected right after
        a restart or while the radio is unplugged)."""
        r = self.store.query("SELECT value FROM settings WHERE key='own_station'")
        return {x for x in (self.store.station, r[0]["value"] if r else None) if x}

    def _logged_here(self, station):
        """A radio this hub logged from itself at some point (rows without src_rowid: not synced, not imported). An
        earlier home radio is no peer's to write, though own_station only remembers the latest; it may still become
        one of this hub's own stations later (a pairing or token), as a radio moved to a Pi or a car does."""
        return bool(self.store.query("SELECT 1 FROM packets WHERE station = ? AND src_rowid IS NULL LIMIT 1", station))

    def _has_data(self, station):
        """Called with the store lock held: reads the connection directly (store.query takes that lock)."""
        return any(self.store.db.execute(f"SELECT 1 FROM {t} WHERE station = ? LIMIT 1", (station,)).fetchone()
                   for t in ("packets", "positions", "rx_hops"))

    def claim(self, station, writer):
        """May `writer` write `station`? Records the first writer as its owner. writer tags: "pair:<id>" (a paired
        station), "peer:<hub id>" (a peer hub), "shared" (the shared [sync] token), "token" (a per-station [sync]
        token, bound by config). Rules: nobody writes as this hub's own radio; a peer only writes stations it brought
        (never ones this hub already has data for from elsewhere); a pairing or the shared token may take over a
        station from the shared token or a revoked pairing, never from a live pairing or a peer."""
        if station in self.own_stations():
            raise IngestError(409, "that station is this hub's own radio")
        if writer.startswith("peer:") and self._logged_here(station):
            # not "own radio": the peer must keep these rows pending (it may have ones this hub never logged)
            raise IngestError(409, "this hub logged that radio itself once, so it takes its data from no peer")
        if writer == "token":
            return
        with self.lock, self.store.lock:
            r = self.store.db.execute("SELECT value FROM settings WHERE key='station_owners'").fetchone()
            owners = json.loads(r[0]) if r else {}
            cur = owners.get(station)
            if cur == writer:
                return
            kind, cur_kind = writer.split(":")[0], (cur or "").split(":")[0]
            if cur is None:
                if kind == "peer" and self._has_data(station):
                    raise IngestError(409, "this hub already has that station's data from another source")
            elif kind in ("pair", "shared") and (cur_kind == "shared" or self.is_revoked(cur)):
                pass  # e.g. a hand-set station being paired properly, or re-paired after a revoke
            else:
                raise IngestError(409, "that station belongs to another source on this hub")
            owners[station] = writer
            self.store.db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('station_owners', ?)", (json.dumps(owners),))
            self.store.db.commit()

    def status(self):
        r = self.store.query("SELECT value FROM settings WHERE key='sync_hub'")
        return {"mode": "hub", "stations": json.loads(r[0]["value"]) if r else {}}

    def _report(self, station, meta, via=None):
        """A station report: name, location, note and health. Whitelisted, typed and bounded; doubles as a
        heartbeat for the station-silence alert."""
        if self.store.station and station == self.store.station:
            raise IngestError(409, "that station is this hub's own radio")
        clean = clean_report(meta)
        with self.lock, self.store.lock:
            r = self.store.db.execute("SELECT value FROM settings WHERE key='station_meta'").fetchone()
            allm = json.loads(r[0]) if r else {}
            allm[station] = {**clean, "received": time.time()}
            self.store.db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('station_meta', ?)", (json.dumps(allm),))
            r = self.store.db.execute("SELECT value FROM settings WHERE key='sync_hub'").fetchone()
            st = json.loads(r[0]) if r else {}
            s = st.setdefault(station, {"first": time.time(), "rows": 0, "tables": {}})
            s["last"] = time.time()
            if via:
                s["via"] = via
            self.store.db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('sync_hub', ?)", (json.dumps(st),))
            self.store.db.commit()
        if self.on_meta:
            try:
                self.on_meta(station, clean)
            except Exception:  # noqa: BLE001
                log.exception("station report hook failed")
        return {"ok": True}

    def _owned(self, writer):
        r = self.store.query("SELECT value FROM settings WHERE key='station_owners'")
        return sorted(st for st, o in (json.loads(r[0]["value"]) if r else {}).items() if o == writer)

    def owned_by(self, writer):
        """What `writer` sent, as {station: rows}: every row stamped received_via = writer (exact, whatever station),
        plus, for stations it owns, rows from before received_via existed (src_rowid set, received_via empty)."""
        out = {}
        for t in STATION_TABLES:
            for r in self.store.query(f"SELECT station, COUNT(*) AS n FROM {t} WHERE received_via = ? GROUP BY station", writer):
                out[r["station"]] = out.get(r["station"], 0) + r["n"]
        for st in self._owned(writer):
            n = sum(self.store.query(f"SELECT COUNT(*) AS n FROM {t} WHERE station = ? AND src_rowid IS NOT NULL "
                                     f"AND received_via IS NULL", st)[0]["n"] for t in STATION_TABLES)
            if n or st not in out:
                out[st] = out.get(st, 0) + n
        return out

    def delete_from(self, writer):
        """Delete everything `writer` sent: every row stamped received_via = writer, plus older synced rows of the
        stations it owns; and, for stations that were all its, what this hub keeps about them (reports, sync status,
        ownership, overrides, peering bookmarks). Rows this hub logged itself, and rows that came by any other route,
        are never touched. Returns {station: rows deleted}."""
        counts = self.owned_by(writer)
        if not counts:
            return {}
        owned = set(self._owned(writer))
        with self.lock, self.store.lock:
            for t in STATION_TABLES:
                self.store.db.execute(f"DELETE FROM {t} WHERE received_via = ?", (writer,))
                for st in owned:  # rows from before received_via existed
                    self.store.db.execute(f"DELETE FROM {t} WHERE station = ? AND src_rowid IS NOT NULL AND received_via IS NULL",
                                          (st,))
            for key in ("station_meta", "sync_hub", "station_owners", "station_overrides"):  # stations that were all its
                r = self.store.db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
                if r:
                    d = json.loads(r[0])
                    for st in owned:
                        d.pop(st, None)
                    self.store.db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, json.dumps(d)))
            r = self.store.db.execute("SELECT value FROM settings WHERE key='peer_progress'").fetchone()
            if r:
                d = {pid: {st: v for st, v in prog.items() if st not in owned} for pid, prog in json.loads(r[0]).items()}
                self.store.db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('peer_progress', ?)", (json.dumps(d),))
            self.store.db.commit()
        return counts

    def ingest(self, batch, bound_station=None, via=None, writer=None):
        """Validate and store one batch. Returns {"acked": highest src_rowid stored or already present}.
        bound_station: the station the request's token belongs to; the batch must be that station's.
        via: the peer hub this came through (peering), recorded per station.
        writer: who is sending (see claim); None = trusted (tests, local tools)."""
        if not isinstance(batch, dict) or batch.get("protocol") != PROTOCOL:
            raise IngestError(400, f"expected protocol {PROTOCOL}")
        station, table, rows = batch.get("station"), batch.get("table"), batch.get("rows")
        if bound_station is not None and station != bound_station:
            raise IngestError(403, "this token belongs to a different station")
        if not isinstance(station, str) or not STATION_RE.fullmatch(station):
            raise IngestError(400, "bad station id")
        if writer is not None:
            if writer.startswith("peer:") and table is not None and table not in PEER_TABLES:
                raise IngestError(400, f"table {table!r} isn't shared between hubs")
            self.claim(station, writer)
        if "meta" in batch and table is None:
            return self._report(station, batch["meta"], via)
        if table not in STATION_TABLES:
            raise IngestError(400, f"table {table!r} does not sync")
        if not isinstance(rows, list) or not rows or len(rows) > 5 * BATCH_ROWS:
            raise IngestError(400, "rows must be a non-empty list")
        if self.store.station and station == self.store.station:
            raise IngestError(409, "that station is this hub's own radio")
        cols = self._columns(table) - {"src_rowid", "station", "received_via"}
        types = self.cols[table]
        now = time.time()
        unknown = set()
        values = []
        for r in rows:
            if not isinstance(r, dict) or r.get("station") != station or not isinstance(r.get("src_rowid"), int) \
                    or isinstance(r.get("src_rowid"), bool):
                raise IngestError(400, "every row needs this batch's station and an integer src_rowid")
            for c in cols & set(r):
                _check_value(table, c, types[c], r[c], now)
            unknown |= {str(k)[:40] for k in set(r) - cols - {"src_rowid", "station", "received_via"}}
            if table == "events" and r.get("kind") == "connected" and r.get("detail"):
                r = {**r, "detail": _strip_secrets(r["detail"])}  # a collector on older code may still log them
            values.append(r)
        use = sorted(cols & set().union(*(set(r) for r in values)))  # columns both sides know
        via_col = "received_via" in self.cols[table]  # how it arrived: the hub's own record, never the sender's
        names = ",".join(use + ["station", "src_rowid"] + (["received_via"] if via_col else []))
        marks = ",".join("?" * (len(use) + 2 + via_col))
        with self.lock, self.store.lock:
            cur = self.store.db.executemany(f"INSERT OR IGNORE INTO {table} ({names}) VALUES ({marks})",
                                            [[r.get(c) for c in use] + [station, r["src_rowid"]] + ([writer] if via_col else [])
                                             for r in values])
            stored = cur.rowcount
            r = self.store.db.execute("SELECT value FROM settings WHERE key='sync_hub'").fetchone()
            st = json.loads(r[0]) if r else {}  # read directly: store.query would re-take the store lock
            s = st.setdefault(station, {"first": time.time(), "rows": 0, "tables": {}})
            s.update(last=time.time(), rows=s["rows"] + max(stored, 0))
            if via:
                s["via"] = via
            s["tables"][table] = max(s["tables"].get(table, 0), rows[-1]["src_rowid"])
            if unknown:
                s["unknownColumns"] = sorted(set(s.get("unknownColumns", [])) | unknown)[:20]
            self.store.db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('sync_hub', ?)", (json.dumps(st),))
            self.store.db.commit()
        if unknown:
            log.warning("ingest from %s: ignored columns this hub doesn't have in %s: %s", station, table, sorted(unknown))
        if self.on_rows and stored:
            try:
                self.on_rows(station, table, [r["src_rowid"] for r in values])
            except Exception:  # noqa: BLE001 - the live view must never fail an ingest
                log.exception("live update after ingest failed")
        return {"acked": rows[-1]["src_rowid"], "stored": stored, "duplicates": len(rows) - max(stored, 0)}
