"""The SQLite log (mesh.db): schema, migrations, field notes, the Store and the debug-log database, and
the secret stripping applied to everything stored.
"""
import atexit
import json
import logging
import queue
import sqlite3
import threading
import time

import analytics
from config import CFG


log = logging.getLogger("meshdash")


# field_notes: caveats stored *in the database* next to the data, for anyone analysing mesh.db later.
# Values are logged as received; these explain the ones that could mislead. Rewritten on every start.
FIELD_NOTES = [
    ("*", "ts", "When the server received it, from this PC's clock (local machine time as Unix seconds), not "
                "when it was on the air. Usually within a second of reception."),
    ("packets", "*", "One row per packet the radio firmware handed to the dashboard: the FIRST copy only "
                     "(duplicate rebroadcasts are dropped by the firmware), and essentially only broadcasts and "
                     "packets addressed to our radio. Our own radio's per-minute telemetry is not stored here "
                     "(it is in telemetry_full). For every copy heard, see rx_hops."),
    ("packets", "raw", "The complete packet exactly as the meshtastic Python library decoded it, as JSON (bytes "
                       "base64). The most authoritative record; rows logged before 2026-10-05 ~21:50 have no raw."),
    ("packets", "channel", "For DECODED packets: the channel INDEX on our radio (0 = primary). Protobuf omits the "
                           "default 0 on the wire, so raw has no 'channel' key and 0 is the protocol default. For "
                           "ENCRYPTED (undecodable) packets: the 1-byte on-air channel HASH instead. Not comparable "
                           "with rx_hops.channel, which is always the hash (LongFast = 0x08)."),
    ("packets", "hops", "Derived: hopStart - hopLimit from raw. NULL when the sender's firmware doesn't send hopStart."),
    ("packets", "relay", "Low byte only of the node number of the LAST radio that transmitted this copy (firmware "
                         "2.6+). For 0-hop packets it is the sender's own low byte. Not unique: several nodes can "
                         "share a byte."),
    ("packets", "snr", "Measured by our radio on the copy it received, i.e. the signal from the last transmitter "
                       "(the relay), not from the original sender unless hops = 0. Same for rssi."),
    ("packets", "(arrival)", "Not a stored column. The packet browser derives how each row arrived from raw: "
                              "transportMechanism TRANSPORT_LORA = over the air; no transportMechanism and no "
                              "rxSnr/rxRssi = 'local', a report the radio sent the dashboard itself (e.g. a "
                              "MAX_RETRANSMIT NAK from our radio to our radio); no raw = unknown."),
    ("packets", "to_id", "'^all' means broadcast (on air 0xffffffff), the meshtastic library's convention."),
    ("packets", "pkt_id", "Meshtastic: the packet id the sender chose (32 bits). MeshCore (portnum MC_*): the first 13 hex "
     "digits of the packet hash (52 bits); rows logged before 2026-10-15 used 15 (60 bits, more than a browser shows "
     "exactly)."),
    ("rx_hops", "*", "One row per over-the-air reception, duplicates included, parsed live from the firmware's "
                     "'Lora RX (' debug lines (all header fields as logged). Depends on the firmware's log "
                     "format; lines can be missed while the dashboard reconnects. No decoded contents."),
    ("rx_hops", "transport", "As printed in the RX line. Firmware 2.7.26 prints it BEFORE setting it, so it reads 0 "
                             "(TRANSPORT_INTERNAL) even though every 'Lora RX' line is by definition a LoRa reception. "
                             "The real value is in packets.raw -> transportMechanism (TRANSPORT_LORA)."),
    ("rx_hops", "channel", "The 1-byte on-air channel hash (derived from channel name + key), not an index. LongFast "
                           "with the default key = 0x08. Different channels can share a hash."),
    ("rx_hops", "hops", "Derived convenience column: hop_start - hop_limit (both stored raw alongside)."),
    ("mc_rx", "*", "MeshCore stations: one row per over-the-air reception (duplicates included), from the companion "
                   "radio's RX_LOG_DATA reports. frame is the whole packet as hex; the other columns are parsed from "
                   "it (meshcore_packet.parse) and from the radio's signal readings. docs/MESHCORE.md."),
    ("mc_rx", "packet_hash", "The firmware's packet hash (first 8 bytes of SHA-256 over the payload type and payload), "
                             "the same however the packet travelled: one transmission heard several times shares it. "
                             "MeshCore has no packet id."),
    ("mc_rx", "path", "The repeaters it came through, first first, as comma-separated public-key prefixes "
                      "(hop_bytes bytes each). A 1-byte prefix can match several radios: a prefix, never a radio id."),
    ("mc_rx", "from_id", "The sender, only where the packet itself says (adverts carry the full public key). Empty "
                         "otherwise: most MeshCore packets don't name their sender in the clear."),
    ("mc_rx", "transport1", "Region transport codes (transport routes only): a flood limited to a region carries them."),
    ("rx_hops", "relay", "Low byte of the last transmitter's node number, as logged (e.g. 0x22). Not unique: several nodes can share it."),
    ("rx_hops", "next_hop", "Low byte of the next hop the sender chose for a directed packet (next-hop routing); "
                            "0/NULL when not set."),
    ("rx_hops", "directed", "Derived: 1 if to_id is a single node, 0 if broadcast."),
    ("*", "station", "Node id of the listening station's radio that logged the row (e.g. a USB "
                     "Heltec at home). Added 2026-10-06 for a second station; every earlier row was backfilled with the "
                     "radio connected at the time. Each station logs what IT heard: the same packet heard at two "
                     "stations is two rows, so counting across stations needs DISTINCT (from_id, pkt_id). For "
                     "tx_log it's the station that transmitted."),
    ("rx_hops", "length", "The 'len=' of the RX line: the WHOLE on-air frame, 16-byte header included (not just the "
                          "encrypted body). Verified 2026-10-06 by packet anatomy: 436 of 441 rebuilt packets came to "
                          "exactly this length; 4 traceroute replies differ because our radio appends its own "
                          "SNR to snr_back before handing them over. Same meaning in tx_log.length."),
    ("rx_hops", "encrypted", "Derived: 1 if the RX line contains the word 'encrypted' (payload not yet decrypted "
                             "at that point in the firmware; that is normal for all packets at receive time)."),
    ("rx_hops", "to_id", "'^all' means broadcast (on air 0xffffffff), matching packets.to_id."),
    ("rx_hops", "snr", "Signal of this copy as received by our radio, i.e. from the relay that sent it. Same for rssi."),
    ("tx_log", "*", "One row per transmission by OUR radio, parsed from the firmware's 'Started Tx (' debug lines: "
                    "its own broadcasts, replies the firmware sends automatically (e.g. NodeInfo responses), "
                    "and messages/traceroutes sent from the dashboard. Same header fields and caveats as rx_hops "
                    "(transport is printed before it is set). priority is the firmware's queue priority."),
    ("tx_log", "relay", "Low byte of our own node number (0x48), as the firmware stamps it on transmit."),
    ("traceroutes", "origin", "'manual' = started from the dashboard UI, 'scheduled' = the hourly auto-traceroute. "
                              "NULL for traces run before 2026-10-05 ~23:50 (all manual)."),
    ("messages", "hop_limit", "Outgoing only: the hop limit the dashboard sent with. Directed messages use "
                              "max(radio default, last known hops to the recipient + 1), capped at 7; broadcasts use "
                              "the radio default. NULL for messages sent before 2026-10-06."),
    ("alerts", "*", "Dashboard-generated alerts (not received data): watched node silent/back, low battery, new "
                    "nodes, new health warnings."),
    ("settings", "*", "Dashboard settings as JSON (not received data)."),
    ("telemetry", "battery", "Percent as reported. Values above 100 (typically 101) mean external power, not a charge level."),
    ("telemetry", "*", "Chart-friendly subset of telemetry. telemetry_full has every field of every telemetry kind."),
    ("node_info", "role", "When a node broadcasts the default role (CLIENT), protobuf omits the field; CLIENT is "
                          "recorded in that case. data holds the user record exactly as received."),
    ("node_info", "*", "A row only when a node's name / hardware / role / public key first appears or changes."),
    ("links", "*", "DERIVED, not raw: 'direct' rows are written when a packet arrives with 0 hops; 'traceroute' "
                   "and 'neighborinfo' rows are hops parsed from those packets. The analytics topology also infers "
                   "links from rx_hops/packets relay bytes at query time (not stored)."),
    ("messages", "*", "Derived from TEXT_MESSAGE packets plus messages this dashboard sent (outgoing = 1, status "
                      "updated as ACKs arrive). encrypted = 1 marks DMs to us that couldn't be decrypted."),
    ("events", "detail", "Radio config snapshots have security.private_key, security.admin_key and channel PSKs "
                         "removed before storage. Everything else is as reported."),
    ("positions", "precision_bits", "Position precision the node shares at. Below 32 the coordinates are "
                                    "deliberately truncated: ~23 km box at 10 bits, halving per extra bit."),
    ("positions", "source", "'own' = the listening station's radio's own GPS fix (node = station), logged so a "
                            "moving station's location is known over time; NULL = a position heard from the mesh."),
    ("positions", "fix_time", "The GPS fix's own timestamp as the radio reported it (own fixes only)."),
    ("*", "src_rowid", "Set on rows that came from elsewhere (a station sending to this hub, or a peer hub): the row's "
                       "number in the database of the station that logged it, so a resend, or the same row arriving by "
                       "two routes, is stored once (unique with station). Empty on rows this computer's radio logged."),
    ("*", "received_via", "How the row reached this database: empty = logged here by this computer's radio; "
                          "'pair:<id>' = from a station paired on the Stations page; 'shared' / 'token' = from a station "
                          "using a hand-set [sync] token; 'peer:<hub id>' = from another hub (peering). Rows that came "
                          "from elsewhere before 0.3.0 have it empty but src_rowid set."),
    ("packets", "raw (from peers)", "A peer only sees the contents of packets the share settings allow: a private-channel "
                                    "or direct text arrives with decoded.textWithheld (summary '(text not shared)'), other "
                                    "payloads with decoded.payloadWithheld (summary '(not shared)'). The reception itself "
                                    "(time, sender, hops, signal, relay) is complete."),
    ("positions", "lat / lon (from peers)", "Unless a peer chose to share exact locations, every location it sends is "
                                            "rounded to a ~3 km grid (0.03 degrees) and marked precision_bits 14: "
                                            "positions, a moving station's own GPS track, coordinates inside packets "
                                            "and node-database snapshots, and its stations' reported locations."),
]


DEBUG_RETENTION_DAYS = CFG["storage"]["debug_log_days"]  # high-volume; lives in its own short-lived DB

SCHEMA = """
CREATE TABLE IF NOT EXISTS packets (
  ts REAL, from_id TEXT, to_id TEXT, portnum TEXT, channel INTEGER,
  snr REAL, rssi INTEGER, hops INTEGER, via_mqtt INTEGER, summary TEXT);
CREATE INDEX IF NOT EXISTS packets_ts ON packets(ts);
CREATE INDEX IF NOT EXISTS packets_from_ts ON packets(from_id, ts);
CREATE TABLE IF NOT EXISTS telemetry (
  ts REAL, node TEXT, battery REAL, voltage REAL, ch_util REAL, air_util REAL,
  uptime INTEGER, temperature REAL, humidity REAL, pressure REAL);
CREATE INDEX IF NOT EXISTS telemetry_node_ts ON telemetry(node, ts);
CREATE TABLE IF NOT EXISTS positions (
  ts REAL, node TEXT, lat REAL, lon REAL, alt REAL, precision_bits INTEGER);
CREATE INDEX IF NOT EXISTS positions_node_ts ON positions(node, ts);
CREATE TABLE IF NOT EXISTS messages (
  ts REAL, from_id TEXT, to_id TEXT, channel INTEGER, text TEXT);
CREATE INDEX IF NOT EXISTS messages_ts ON messages(ts);
CREATE TABLE IF NOT EXISTS links (
  ts REAL, a TEXT, b TEXT, snr REAL, source TEXT);
CREATE INDEX IF NOT EXISTS links_ts ON links(ts);
CREATE TABLE IF NOT EXISTS traceroutes (
  ts REAL, target TEXT, status TEXT, forward TEXT, back TEXT, done_ts REAL);
-- every telemetry variant in full (device, environment, power, air quality, local stats, health, host)
CREATE TABLE IF NOT EXISTS telemetry_full (ts REAL, node TEXT, kind TEXT, data TEXT);
CREATE INDEX IF NOT EXISTS telemetry_full_node_ts ON telemetry_full(node, kind, ts);
-- a row whenever a node's identity changes: name, hardware, role, firmware-visible key
CREATE TABLE IF NOT EXISTS node_info (
  ts REAL, node TEXT, long_name TEXT, short_name TEXT, hw_model TEXT, role TEXT,
  public_key TEXT, is_licensed INTEGER, data TEXT);
CREATE INDEX IF NOT EXISTS node_info_node_ts ON node_info(node, ts);
-- radio link + server lifecycle: connects (with radio config), disconnects, watchdog, prunes
CREATE TABLE IF NOT EXISTS events (ts REAL, kind TEXT, detail TEXT);
CREATE INDEX IF NOT EXISTS events_ts ON events(ts);
-- daily copy of the radio's whole node DB
CREATE TABLE IF NOT EXISTS nodedb_snapshots (ts REAL, data TEXT);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
-- one row per over-the-air reception (duplicates included), mined from the firmware's "Lora RX" debug
-- lines: the full header our radio received (sender, recipient, packet id, channel byte, hops, relay
-- byte, signal) plus a broadcast/directed flag. The user wants complete packet info logged (2026-10-05).
CREATE TABLE IF NOT EXISTS rx_hops (ts REAL, from_id TEXT, relay INTEGER, hops INTEGER, snr REAL, rssi INTEGER);
CREATE INDEX IF NOT EXISTS rx_hops_ts ON rx_hops(ts);
-- one row per transmission by OUR radio, from the firmware's "Started Tx (" debug lines (full header).
CREATE TABLE IF NOT EXISTS tx_log (ts REAL, from_id TEXT, to_id TEXT, pkt_id INTEGER, channel INTEGER,
  directed INTEGER, want_ack INTEGER, next_hop INTEGER, relay INTEGER, length INTEGER, encrypted INTEGER,
  transport INTEGER, hop_start INTEGER, hop_limit INTEGER, hops INTEGER, priority INTEGER);
CREATE INDEX IF NOT EXISTS tx_log_pkt ON tx_log(pkt_id);
-- MeshCore stations: one row per over-the-air reception, from the companion radio's RX_LOG_DATA (docs/MESHCORE.md).
-- MeshCore packets have no packet id and usually no sender in the clear, so they don't fit rx_hops. frame (not raw:
-- raw is JSON elsewhere) is the whole packet as hex; nothing parsed from it is stored without it.
CREATE TABLE IF NOT EXISTS mc_rx (ts REAL, snr REAL, rssi INTEGER, length INTEGER, route TEXT, payload_type INTEGER,
  payload_name TEXT, version INTEGER, transport1 INTEGER, transport2 INTEGER, hop_bytes INTEGER, hops INTEGER,
  path TEXT, packet_hash TEXT, from_id TEXT, frame TEXT);
CREATE INDEX IF NOT EXISTS mc_rx_hash ON mc_rx(packet_hash);
"""
# Every table whose rows were logged by a listening station's radio. `station` = that radio's node id
# (the desk Heltec at home; later a second station across town). Store.insert fills it in; the analytics
# modules scope their reads to one station (analytics._connect).
STATION_TABLES = analytics.STATION_TABLES
MIGRATIONS = [
    "ALTER TABLE packets ADD COLUMN relay INTEGER",
    "ALTER TABLE messages ADD COLUMN pkt_id INTEGER",
    "ALTER TABLE messages ADD COLUMN outgoing INTEGER DEFAULT 0",
    "ALTER TABLE messages ADD COLUMN status TEXT",
    "ALTER TABLE packets ADD COLUMN pkt_id INTEGER",
    "ALTER TABLE packets ADD COLUMN pki INTEGER",
    "ALTER TABLE packets ADD COLUMN raw TEXT",          # the full packet as JSON, nothing dropped
    "ALTER TABLE messages ADD COLUMN encrypted INTEGER DEFAULT 0",
    "ALTER TABLE rx_hops ADD COLUMN pkt_id INTEGER",
    "ALTER TABLE rx_hops ADD COLUMN channel INTEGER",   # the on-air 1-byte channel hash, not a name
    "ALTER TABLE rx_hops ADD COLUMN directed INTEGER",  # 1 = addressed to a single node, 0 = broadcast
    "ALTER TABLE rx_hops ADD COLUMN to_id TEXT",
    "ALTER TABLE rx_hops ADD COLUMN want_ack INTEGER",
    "ALTER TABLE rx_hops ADD COLUMN next_hop INTEGER",
    "ALTER TABLE rx_hops ADD COLUMN length INTEGER",
    "ALTER TABLE rx_hops ADD COLUMN encrypted INTEGER",
    "ALTER TABLE traceroutes ADD COLUMN origin TEXT",
    "ALTER TABLE messages ADD COLUMN hop_limit INTEGER",  # hop limit we sent with (outgoing only)  # 'manual' (from the UI) or 'scheduled'
    "ALTER TABLE rx_hops ADD COLUMN hop_start INTEGER",
    "ALTER TABLE rx_hops ADD COLUMN hop_limit INTEGER",
    "ALTER TABLE rx_hops ADD COLUMN transport INTEGER",  # TransportMechanism as the RX line prints it; firmware 2.7.26 logs it before setting it, so it reads 0 (INTERNAL) even for LoRa
    # multiple listening stations (2026-10-06): which station's radio logged each row
    *[f"ALTER TABLE {t} ADD COLUMN station TEXT" for t in STATION_TABLES],
    "CREATE INDEX IF NOT EXISTS packets_station_ts ON packets(station, ts)",
    "CREATE INDEX IF NOT EXISTS rx_hops_station_ts ON rx_hops(station, ts)",
    "CREATE INDEX IF NOT EXISTS tx_log_station_ts ON tx_log(station, ts)",
    "CREATE INDEX IF NOT EXISTS telemetry_full_station ON telemetry_full(station, kind, ts)",
    # rows a collector sent us: their rowid at the collector, so a resent batch is ignored (sync.py)
    *[f"ALTER TABLE {t} ADD COLUMN src_rowid INTEGER" for t in STATION_TABLES],
    *[f"CREATE UNIQUE INDEX IF NOT EXISTS {t}_station_src ON {t}(station, src_rowid) WHERE src_rowid IS NOT NULL"
      for t in STATION_TABLES],
    # moving stations (2026-10-07): a station's own GPS fixes, so where it was is known over time
    "ALTER TABLE positions ADD COLUMN source TEXT",
    "ALTER TABLE positions ADD COLUMN fix_time INTEGER",
    "CREATE INDEX IF NOT EXISTS positions_own ON positions(station, node, ts) WHERE source = 'own'",
    # 0.3.0 (database layout 2): how each row arrived, so what one route brought can be told apart and deleted
    *[f"ALTER TABLE {t} ADD COLUMN received_via TEXT" for t in STATION_TABLES],
    # 0.5.0 (database layout 3): MeshCore receptions (mc_rx, in SCHEMA; station, src_rowid and received_via come from
    # the loops above, which run over every station table)
    "CREATE INDEX IF NOT EXISTS mc_rx_station_ts ON mc_rx(station, ts)",
]
# The database's layout version, stored in the file (PRAGMA user_version). Updates only ever ADD tables, columns
# and indexes (MIGRATIONS above), never remove or rewrite logged data. Bump it with each change to SCHEMA or
# MIGRATIONS, so an older Lorakeet opening a newer database can say so.
SCHEMA_VERSION = 3  # 2: received_via on every station table; 3: mc_rx (MeshCore receptions)
DB_WAIT_S = 30        # how long a write or read waits for the database before giving up
# Rows are committed together this often (or once COMMIT_MAX_ROWS are waiting), not one transaction each: on a Pi
# every commit rewrites several 4 KB pages (the table and each index) and flushes them, which is what wore SD cards.
# The writes are no less safe: until the commit, readers on this connection already see the rows, other connections
# (analytics, backups) see them up to COMMIT_EVERY_S later, and Store.flush() / runtime.exit_now() commit at exit.
COMMIT_EVERY_S = 2.0
COMMIT_MAX_ROWS = 500
# A hub tells rows apart by (station, the row's number at the station) and ignores ones it has (sync.py). A new
# database counting from 1 again (a replaced SD card or drive, a reinstall, the drive-failure fallback) would have
# every new row ignored as already received, silently. So a database created now starts each station table's row
# numbers at the time in milliseconds: a later database starts above anything an earlier one could have reached
# (short of writing a thousand rows a second), and existing databases keep counting where they are.
ROWID_FLOOR_KEY = "rowid_floor"


class Store:
    def __init__(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False, timeout=DB_WAIT_S)
        self.db.row_factory = sqlite3.Row
        # WAL: readers (analytics, every page) and the writer (the radio, sync) never block each other. In the
        # default rollback mode a slow analytics read made writes wait, and a write left waiting blocked new
        # reads: "database is locked". The setting is stored in the file, so this converts an existing database.
        try:
            mode = self.db.execute("PRAGMA journal_mode=WAL").fetchone()[0]
            if mode != "wal":
                log.warning("mesh.db journal mode is %s, not WAL (another program has it open?)", mode)
        except sqlite3.OperationalError as e:
            log.warning("couldn't switch mesh.db to WAL: %s", e)
        # NORMAL, not FULL: in WAL mode a power cut can lose the last commits but never corrupts the file, and it
        # skips a flush per commit
        self.db.execute("PRAGMA synchronous=NORMAL")
        fresh = not self.db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'packets'").fetchone()
        self.db.executescript(SCHEMA)
        for sql in MIGRATIONS:
            try:
                self.db.execute(sql)
            except sqlite3.OperationalError:
                pass  # column already exists
        found = self.db.execute("PRAGMA user_version").fetchone()[0]
        if found > SCHEMA_VERSION:
            # a newer Lorakeet made this database: logging still works (its additions are left alone), but
            # this version can't use them. Never lower the number.
            log.warning("mesh.db is from a newer Lorakeet (database version %d, this one knows %d): update Lorakeet",
                        found, SCHEMA_VERSION)
        elif found < SCHEMA_VERSION:
            self.db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        self.schema_version = max(found, SCHEMA_VERSION)
        self.db.execute("CREATE TABLE IF NOT EXISTS field_notes (tbl TEXT, col TEXT, note TEXT, PRIMARY KEY (tbl, col))")
        self.db.execute("DELETE FROM field_notes")
        self.db.executemany("INSERT INTO field_notes VALUES (?, ?, ?)", FIELD_NOTES)
        if fresh:
            self.db.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
                            (ROWID_FLOOR_KEY, str(int(time.time() * 1000))))
        r = self.db.execute("SELECT value FROM settings WHERE key = ?", (ROWID_FLOOR_KEY,)).fetchone()
        self.rowid_floor = int(r[0]) if r else None
        # station tables still waiting for their first row in a database that has a floor
        self._unfloored = {t for t in STATION_TABLES if self.rowid_floor is not None
                           and self.db.execute(f"SELECT MAX(rowid) FROM {t}").fetchone()[0] is None}
        self.db.commit()
        self.lock = threading.Lock()
        self._pending = 0  # writes since the last commit
        atexit.register(self.flush)
        threading.Thread(target=self._committer, daemon=True, name="db-commit").start()

    def _wrote(self):
        """After a write (lock held): commit now if enough are waiting, otherwise the committer thread will."""
        self._pending += 1
        if self._pending >= COMMIT_MAX_ROWS:
            self.db.commit()
            self._pending = 0

    def flush(self):
        """Commit whatever is waiting."""
        with self.lock:
            try:
                if self.db.in_transaction:
                    self.db.commit()
            except sqlite3.ProgrammingError:
                pass  # already closed (a test, or shutdown): nothing left to write
            self._pending = 0

    def close(self):
        """Commit what's waiting, then close. (Closing the connection directly would throw it away.)"""
        self.flush()
        with self.lock:
            self.db.close()

    def _committer(self):
        while True:
            time.sleep(COMMIT_EVERY_S)
            try:
                self.flush()
            except Exception:  # noqa: BLE001 - keep committing; the next pass retries
                log.exception("database commit failed")

    station = None  # node id of the radio this server is logging from; set on connect

    def claim_station(self, sid):
        """This server now logs from radio `sid`: tag new rows with it, and give any untagged rows (logged
        before stations existed, or before the radio connected) to it."""
        self.station = sid
        analytics.HOME["id"] = sid  # what "our radio" means in the combined view
        with self.lock:  # remembered, so nobody can write as this radio while it's unplugged or before it connects
            self.db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('own_station', ?)", (sid,))
            self.db.commit()
        with self.lock:
            for t in STATION_TABLES:
                self.db.execute(f"UPDATE {t} SET station = ? WHERE station IS NULL", (sid,))
            self.db.commit()

    def add_own_station(self, sid):
        """Another radio this Lorakeet logs from ([[radios]]): remembered with the main one, so a hub never takes
        rows for it from anyone else. Doesn't change which radio is "ours" on the pages (claim_station does)."""
        with self.lock:
            r = self.db.execute("SELECT value FROM settings WHERE key = 'own_stations'").fetchone()
            ids = set(json.loads(r[0])) if r else set()
            if sid not in ids:
                ids.add(sid)
                self.db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('own_stations', ?)",
                                (json.dumps(sorted(ids)),))
                self.db.commit()

    def own_stations(self):
        """Every radio this Lorakeet logs from: the main one and the [[radios]] ones it has seen."""
        r = self.query("SELECT value FROM settings WHERE key IN ('own_station', 'own_stations')")
        out = {self.station} if self.station else set()
        for x in r:
            v = x["value"]
            out |= set(json.loads(v)) if v.startswith("[") else {v}
        return out

    def insert(self, table, **row):
        if table in STATION_TABLES and "station" not in row and self.station:
            row["station"] = self.station
        with self.lock:
            if table in self._unfloored:  # its first row: start counting at the floor (ROWID_FLOOR_KEY)
                self._unfloored.discard(table)
                row = {"rowid": self.rowid_floor, **row}
            cols = ",".join(row)
            marks = ",".join("?" * len(row))
            cur = self.db.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", list(row.values()))
            self._wrote()
            return cur.lastrowid

    def execute(self, sql, *args):
        with self.lock:
            self.db.execute(sql, args)
            self._wrote()

    def query(self, sql, *args):
        with self.lock:
            return [dict(r) for r in self.db.execute(sql, args).fetchall()]


class StationStore:
    """One extra radio's view of the shared Store ([[radios]]: several radios, one Lorakeet, MESHCORE.md step 6).
    Everything goes to the same database on the same connection; rows it inserts are tagged with its own station, and
    claiming a station doesn't make it the main radio. A link takes it wherever it takes a Store."""

    def __init__(self, store):
        self._store = store
        self.station = None

    def __getattr__(self, name):
        return getattr(self._store, name)

    def insert(self, table, **row):
        if table in STATION_TABLES and "station" not in row and self.station:
            row["station"] = self.station
        return self._store.insert(table, **row)

    def claim_station(self, sid):
        self.station = sid
        self._store.add_own_station(sid)


class DebugLog:
    """The radio firmware's own debug log, in a separate database with a one-week window.

    Lines arrive in bursts (several per packet), so they're queued and written in batches by one
    thread; the radio's reader thread never waits on disk. The live viewer gets each batch every BATCH_S; the disk
    gets them every COMMIT_EVERY_S (each commit rewrites pages and flushes them, the SD-card wear store.py's
    COMMIT_EVERY_S explains), or sooner when a page asks for lines (query) or Lorakeet exits (flush).
    """

    LEVELS = {0: "UNSET", 5: "TRACE", 10: "DEBUG", 20: "INFO", 30: "WARN", 40: "ERROR", 50: "CRIT"}
    COLS = ("ts", "device_time", "level", "source", "message")
    BATCH_S = 2
    COMMIT_EVERY_S = 15
    MAX_BATCH = 5000

    def __init__(self, path):
        self.path = path
        self.on_batch = None  # called with each batch (list of dicts) as it's written — feeds the live viewer
        self.q = queue.Queue(maxsize=50_000)
        self.dropped = 0
        self._flush_req = threading.Event()   # set by flush(): commit now
        self._flushed = threading.Event()     # set by the writer when it has
        threading.Thread(target=self._writer, daemon=True, name="debuglog").start()

    def flush(self, timeout=3.0):
        """Ask the writer to commit what it holds, and wait (briefly) until it has."""
        self._flushed.clear()
        self._flush_req.set()
        self._flushed.wait(timeout)

    def add(self, level, source, message, device_time=None):
        try:
            self.q.put_nowait((time.time(), device_time, level, source, message))
        except queue.Full:
            self.dropped += 1  # disk stalled for minutes; shedding debug lines beats blocking the radio

    def _writer(self):
        db = sqlite3.connect(self.path)
        db.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=NORMAL;
            CREATE TABLE IF NOT EXISTS debug_log (
              ts REAL, device_time INTEGER, level TEXT, source TEXT, message TEXT);
            CREATE INDEX IF NOT EXISTS debug_log_ts ON debug_log(ts);
        """)
        db.execute("DELETE FROM debug_log WHERE ts < ?", (time.time() - DEBUG_RETENTION_DAYS * 86400,))
        db.commit()  # the week-old lines go at start-up, then hourly
        last_prune = time.time()
        pending, last_commit = 0, time.time()
        while True:
            batch = []
            deadline = time.time() + self.BATCH_S
            while len(batch) < self.MAX_BATCH and not self._flush_req.is_set():
                left = deadline - time.time()
                if left <= 0:
                    break
                try:
                    batch.append(self.q.get(timeout=min(left, 0.5)))
                except queue.Empty:
                    continue
            flush = self._flush_req.is_set()
            try:
                if batch:
                    db.executemany("INSERT INTO debug_log VALUES (?,?,?,?,?)", batch)
                    pending += len(batch)
                    if self.on_batch:
                        self.on_batch([dict(zip(self.COLS, row)) for row in batch])
                if pending and (flush or pending >= self.MAX_BATCH or time.time() - last_commit >= self.COMMIT_EVERY_S):
                    db.commit()
                    pending, last_commit = 0, time.time()
                if time.time() - last_prune > 3600:
                    db.execute("DELETE FROM debug_log WHERE ts < ?", (time.time() - DEBUG_RETENTION_DAYS * 86400,))
                    db.commit()
                    pending, last_prune = 0, time.time()
            except Exception:  # noqa: BLE001 - never let the debug log take anything else down
                log.exception("debug log write failed (%d lines lost)", pending or len(batch))
                try:
                    db.rollback()
                except sqlite3.Error:
                    pass
                pending = 0
            if flush:
                self._flush_req.clear()
                self._flushed.set()

    def query(self, limit, since=None, contains=None, before=None, levels=None, source=None):
        self.flush(timeout=1.0)  # a page loading lines gets the last few seconds too, not just what's committed
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        sql, args = "SELECT rowid, * FROM debug_log WHERE 1=1", []
        if since:
            sql += " AND ts > ?"
            args.append(since)
        if before:
            sql += " AND ts < ?"
            args.append(before)
        if contains:
            sql += " AND message LIKE ?"
            args.append(f"%{contains}%")
        if levels:
            sql += f" AND level IN ({','.join('?' * len(levels))})"
            args.extend(levels)
        if source:
            sql += " AND IFNULL(source, '') = ?"
            args.append("" if source == "(none)" else source)
        sql += " ORDER BY ts DESC LIMIT ?"
        args.append(limit)
        try:
            return [dict(r) for r in db.execute(sql, args).fetchall()]
        finally:
            db.close()

    def sources(self):
        db = sqlite3.connect(self.path)
        try:
            return [r[0] for r in db.execute(
                "SELECT IFNULL(source, '(none)') AS s, COUNT(*) FROM debug_log GROUP BY s ORDER BY 2 DESC").fetchall()]
        finally:
            db.close()


def node_id(num):
    return f"!{num:08x}"


def clean(obj):
    """Packet dicts from the library mix JSON, raw protobufs and bytes. Make them storable.

    Drops the library's duplicate `raw` protobuf copies (their content is already parsed alongside),
    turns bytes into base64 and any remaining protobuf message into a dict.
    """
    import base64

    from google.protobuf.json_format import MessageToDict
    from google.protobuf.message import Message

    if isinstance(obj, dict):
        return {k: clean(v) for k, v in obj.items() if k != "raw"}
    if isinstance(obj, (list, tuple)):
        return [clean(v) for v in obj]
    if isinstance(obj, (bytes, bytearray)):
        return base64.b64encode(obj).decode()
    if isinstance(obj, Message):
        return MessageToDict(obj)
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    return str(obj)


def redacted_config(cfg):
    """Radio config minus secrets: the node's private and admin keys, the Bluetooth pairing PIN and the
    Wi-Fi password never go in the log."""
    c = type(cfg)()
    c.CopyFrom(cfg)
    c.security.ClearField("private_key")
    c.security.ClearField("admin_key")
    c.bluetooth.ClearField("fixed_pin")
    c.network.ClearField("wifi_psk")
    return c


def redacted_module_config(cfg):
    """Module config minus secrets: the MQTT password never goes in the log."""
    c = type(cfg)()
    c.CopyFrom(cfg)
    c.mqtt.ClearField("password")
    return c


# JSON paths of the same secrets in already-logged "connected" events (scrub_logged_secrets)
LOGGED_SECRET_PATHS = ("$.config.bluetooth.fixedPin", "$.config.network.wifiPsk", "$.module_config.mqtt.password")


def scrub_logged_secrets(store):
    """Remove secrets from config snapshots logged before they were redacted. Idempotent; runs at start."""
    paths = ", ".join(f"'{p}'" for p in LOGGED_SECRET_PATHS)
    with store.lock:
        cur = store.db.execute(f"UPDATE events SET detail = json_remove(detail, {paths}) WHERE kind = 'connected' AND ("
                               + " OR ".join(f"json_extract(detail, '{p}') IS NOT NULL" for p in LOGGED_SECRET_PATHS) + ")")
        store.db.commit()
    if cur.rowcount:
        log.info("removed secrets from %d logged config snapshots", cur.rowcount)


def redacted_channels(channels):
    out = []
    for ch in channels or []:
        if ch.role:  # 0 = disabled slot
            c = type(ch)()
            c.CopyFrom(ch)
            c.settings.ClearField("psk")
            out.append(c)
    return out


def dumps(obj):
    return json.dumps(clean(obj), separators=(",", ":"))
