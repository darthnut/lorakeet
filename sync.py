"""Station sync: a remote listening station (collector) sends what it logged to the home server (hub).

The collector runs this same server against its own radio and logs to its own mesh.db, so an internet or
hub outage loses nothing. Every `interval_s` it sends its rows home, table by table in rowid order, in
gzip'd JSON batches, and only advances its bookmark (settings 'sync_progress') once the hub has stored
them. The hub inserts with INSERT OR IGNORE on a unique (station, src_rowid) key, so a resent batch never
duplicates anything. Transport: plain HTTP over a private Tailscale network (encrypted end to end by
WireGuard), with a shared token. Nothing is exposed to the internet at either end.

Only station-tagged tables sync, and a collector only sends rows its own station logged. The firmware
debug log (debug.db) stays on the collector; everything mined from it (rx_hops, tx_log) syncs.
"""
import gzip
import hmac
import ipaddress
import json
import logging
import threading
import time
import urllib.error
import urllib.request

from analytics import STATION_RE, STATION_TABLES

log = logging.getLogger("lorakeet.sync")
BATCH_ROWS = 500
MAX_BODY = 20 * 1024 * 1024        # compressed request body the hub will read
MAX_JSON = 200 * 1024 * 1024       # and what it may decompress to
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


# ---------------------------------------------------------------- station reports

REPORT_FIELDS = {  # field -> type; anything else is dropped
    "name": str, "note": str, "location": list, "software": str, "radioFirmware": str, "radioHw": str,
    "uptimeS": (int, float), "throttled": str, "tempC": (int, float), "diskFreeMB": (int, float),
    "backlog": int, "platform": str, "mobile": bool,
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
        if k == "location":
            if not (len(v) in (2, 3) and all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in v)
                    and -90 <= v[0] <= 90 and -180 <= v[1] <= 180):
                continue
            v = [float(x) for x in v]
        out[k] = v
    return out


# ---------------------------------------------------------------- hub

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


class Hub:
    """Stores collectors' batches. Status per station lives in settings 'sync_hub'."""

    def __init__(self, store, on_rows=None, on_meta=None):
        self.store = store
        self.cols = {}
        self.lock = threading.Lock()
        self.on_rows = on_rows  # called (station, table, src_rowids) after a batch is stored: the live map
        self.on_meta = on_meta  # called (station, report) after a station report is stored

    def _columns(self, table):
        if table not in self.cols:
            self.cols[table] = {r["name"] for r in self.store.query(f"PRAGMA table_info({table})")}
        return self.cols[table]

    def status(self):
        r = self.store.query("SELECT value FROM settings WHERE key='sync_hub'")
        return {"mode": "hub", "stations": json.loads(r[0]["value"]) if r else {}}

    def _report(self, station, meta):
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
            st.setdefault(station, {"first": time.time(), "rows": 0, "tables": {}})["last"] = time.time()
            self.store.db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('sync_hub', ?)", (json.dumps(st),))
            self.store.db.commit()
        if self.on_meta:
            try:
                self.on_meta(station, clean)
            except Exception:  # noqa: BLE001
                log.exception("station report hook failed")
        return {"ok": True}

    def ingest(self, batch, bound_station=None):
        """Validate and store one batch. Returns {"acked": highest src_rowid stored or already present}.
        bound_station: the station the request's token belongs to; the batch must be that station's."""
        if not isinstance(batch, dict) or batch.get("protocol") != PROTOCOL:
            raise IngestError(400, f"expected protocol {PROTOCOL}")
        station, table, rows = batch.get("station"), batch.get("table"), batch.get("rows")
        if bound_station is not None and station != bound_station:
            raise IngestError(403, "this token belongs to a different station")
        if not isinstance(station, str) or not STATION_RE.match(station):
            raise IngestError(400, "bad station id")
        if "meta" in batch and table is None:
            return self._report(station, batch["meta"])
        if table not in STATION_TABLES:
            raise IngestError(400, f"table {table!r} does not sync")
        if not isinstance(rows, list) or not rows or len(rows) > 5 * BATCH_ROWS:
            raise IngestError(400, "rows must be a non-empty list")
        if self.store.station and station == self.store.station:
            raise IngestError(409, "that station is this hub's own radio")
        cols = self._columns(table) - {"src_rowid", "station"}
        unknown = set()
        values = []
        for r in rows:
            if not isinstance(r, dict) or r.get("station") != station or not isinstance(r.get("src_rowid"), int):
                raise IngestError(400, "every row needs this batch's station and an integer src_rowid")
            unknown |= set(r) - cols - {"src_rowid", "station"}
            if table == "events" and r.get("kind") == "connected" and r.get("detail"):
                r = {**r, "detail": _strip_secrets(r["detail"])}  # a collector on older code may still log them
            values.append(r)
        use = sorted(cols & set().union(*(set(r) for r in values)))  # columns both sides know
        names = ",".join(use + ["station", "src_rowid"])
        marks = ",".join("?" * (len(use) + 2))
        with self.lock, self.store.lock:
            cur = self.store.db.executemany(f"INSERT OR IGNORE INTO {table} ({names}) VALUES ({marks})",
                                            [[r.get(c) for c in use] + [station, r["src_rowid"]] for r in values])
            stored = cur.rowcount
            r = self.store.db.execute("SELECT value FROM settings WHERE key='sync_hub'").fetchone()
            st = json.loads(r[0]) if r else {}  # read directly: store.query would re-take the store lock
            s = st.setdefault(station, {"first": time.time(), "rows": 0, "tables": {}})
            s.update(last=time.time(), rows=s["rows"] + max(stored, 0))
            s["tables"][table] = max(s["tables"].get(table, 0), rows[-1]["src_rowid"])
            if unknown:
                s["unknownColumns"] = sorted(set(s.get("unknownColumns", [])) | unknown)
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
