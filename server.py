"""Meshtastic map dashboard backend.

Holds a serial link to a USB-attached Meshtastic node, logs everything it hears
to SQLite, and serves a map-first dashboard with live updates over SSE.

    python server.py [--port COM3] [--http 5190]      (defaults come from lorakeet.toml; see config.py)
"""
import argparse
import json
import logging
import logging.handlers
import os
import ipaddress
import queue
import re
import socket
import sqlite3
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from pubsub import pub
from serial.tools import list_ports

import airtime
import api_index
import alerts as alerts_mod
import analytics
import anatomy
import compare
import drive
import insights
import keyflags
import login as login_mod
import nodeids
import pairing
import radio_setup
import remote as remote_mod
import node_analytics
import packetsearch
import topology
from config import CFG
from config import public as config_public
from storage import Storage
import sync as sync_mod

HERE = Path(__file__).resolve().parent
STATIC = HERE / "static"
DATA_DIR = CFG["storage"]["data_dir"]
VERSION = (HERE / "VERSION").read_text(encoding="utf-8").strip()
RADIO_BACKUPS = DATA_DIR / "radio-backups"  # the Radio page's settings backups: private keys, this user only
RADIO_LOCK = threading.Lock()  # one change to a radio at a time
DEMO = None          # --demo: the made-up mesh's own radio id (demo.py); no radio, its own data folder and port
DEMO_IDLE_S = 3 * 3600  # a demo nobody has looked at for this long exits
LAST_REQUEST = [time.time()]


def key_flag(nid):
    """A radio's compromised / shared public key flag (keyflags.py), or None."""
    return keyflags.flags(DATA_DIR / "mesh.db").get(nid)


def ping_due(last_pos, last_ts, pos, now, every_m, min_s):
    """A drive ping is due once we've moved every_m metres from the last one and min_s has passed."""
    return now - last_ts >= min_s and analytics.dist_m(*last_pos, *pos) >= every_m


def messages_for_this_radio(store, local, limit=200):
    """The Messages tab: every message once (this radio's copy first), with channel numbers as THIS radio numbers
    them. Channel indexes differ between radios (the Pi's 2 is the desk's 1, both "Lorakeet"), so another
    station's copy is renumbered by channel name; one on a channel this radio doesn't have gets channel None
    (shown in no channel view). Phone-log imports (no packet id, no channel recorded) are left out: they can't be
    matched with the stations' copies and only ever showed up as unlabelled duplicates."""
    rows = store.query(
        "SELECT * FROM (SELECT *, ROW_NUMBER() OVER (PARTITION BY CASE WHEN pkt_id IS NULL THEN rowid "
        "ELSE from_id || ':' || pkt_id END ORDER BY (station IS ?) DESC, outgoing DESC, ts) AS _copy "
        "FROM messages WHERE NOT (pkt_id IS NULL AND station IS NOT ?)) WHERE _copy = 1 "
        "ORDER BY ts DESC LIMIT ?", local, local, limit)
    with store.lock:
        names = topology._channel_names(store.db)
    if not local or local not in names:
        return rows  # radio not connected yet (we don't know whose numbering to use): leave channels as logged
    mine = {name: idx for idx, name in names.get(local, {}).items()}
    for m in rows:
        st = m.get("station")
        if st and st != local and m.get("to_id") in ("^all", "!ffffffff"):
            name = names.get(st, {}).get(m.get("channel") or 0)
            m["channelName"] = name
            m["channel"] = mine.get(name) if name else None
    return rows


def first_public_key(store, nid):
    """The first public key this database recorded for a radio: what remote commands are checked against."""
    r = store.query("SELECT public_key FROM node_info WHERE node = ? AND public_key IS NOT NULL AND public_key != '' "
                    "ORDER BY ts LIMIT 1", nid)
    return r[0]["public_key"] if r else None


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


def likely_28(nid):
    """Numbered the firmware 2.8 way (crc32 of its key; nodeids.py)."""
    return nid in nodeids.info(DATA_DIR / "mesh.db")["v28"]
# Optional featured base station (lorakeet.toml [base]); empty = none.
BASE_ID = CFG["base"]["id"]
BASE_NAME = CFG["base"]["name"] or BASE_ID
BASE_BYTE = int(BASE_ID[-2:], 16) if BASE_ID else None   # the 1-byte relay ID it stamps on relayed packets
ESPRESSIF_VID = 0x303A
CONNECT_TIMEOUT_S = 45     # library default is 300 s; a radio still booting after replug shouldn't stall us that long
TRACK_MIN_MOVE_M = 25        # own GPS fixes: log one when the station has moved this far...
TRACK_HEARTBEAT_S = 10 * 60  # ...or this long after the last one (a parked car is still somewhere)
# A mobile station asks its radio for the node list this often: the only way the client gets the radio's
# own position at full precision (its broadcast copy is rounded to the channel's precision). Firmware 2.7+
# answers want_config_id ONLY_NODES with just node infos; the library updates them in place. 10 s: at 60 mph
# that's ~270 m between looks (USB traffic only, nothing on air). Pair it with the radio's own
# position.gps_update_interval, which defaults to 120 s and caps how fresh the fix can be.
OWN_FIX_REFRESH_S = 10
ONLY_NODES_CONFIG_ID = 69421
CLOCK_MAX_SKEW_S = 60  # a mobile Pi with no network sets its clock from the radio's GPS time past this
WATCHDOG_S = 10 * 60       # the radio reports its own telemetry every ~60 s; silence this long means it's wedged
# mesh.db keeps everything forever; storage.py warns (dashboard + Windows notification) at 1 GB instead.
EXIT_PORT_IN_USE = 3       # tells supervise.pyw another instance already owns the HTTP port
ANSI = re.compile(r"\x1b\[[0-9;]*m")  # firmware colours its plain-text boot output
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
    ("rx_hops", "*", "One row per over-the-air reception, duplicates included, parsed live from the firmware's "
                     "'Lora RX (' debug lines (all header fields as logged). Depends on the firmware's log "
                     "format; lines can be missed while the dashboard reconnects. No decoded contents."),
    ("rx_hops", "transport", "As printed in the RX line. Firmware 2.7.26 prints it BEFORE setting it, so it reads 0 "
                             "(TRANSPORT_INTERNAL) even though every 'Lora RX' line is by definition a LoRa reception. "
                             "The real value is in packets.raw -> transportMechanism (TRANSPORT_LORA)."),
    ("rx_hops", "channel", "The 1-byte on-air channel hash (derived from channel name + key), not an index. LongFast "
                           "with the default key = 0x08. Different channels can share a hash."),
    ("rx_hops", "hops", "Derived convenience column: hop_start - hop_limit (both stored raw alongside)."),
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

RX_FIELDS = re.compile(r"\b(id|fr|to|transport|Ch|HopLim|hopStart|relay|nextHop|WantAck|len|rxSNR|rxRSSI|priority)\s*=\s*(-?[0-9a-fA-Fx.]+)")
BROADCAST_NUM = 0xFFFFFFFF


# Plain-text log lines (boot, before the API switches to structured records) carry a prefix like
# "DEBUG | ??:??:?? 32 [RadioIf] " in front of the message itself.
TEXT_PREFIX = re.compile(r"^(?:[A-Z]+\s*\|\s*\S+\s+\d+\s+)?(?:\[[^\]]+\]\s+)?")


def keep_recipient(frm, to, local):
    """lorakeet.toml logging.store_recipients = false drops recipients of packets overheard between others."""
    return CFG["logging"]["store_recipients"] or to in (None, "^all", "!ffffffff") or local in (frm, to)


def arrival_of(packet):
    """How a packet reached the dashboard, from fields the radio reported (mirrors packetsearch.ARRIVAL_SQL)."""
    tm = packet.get("transportMechanism")
    if isinstance(tm, str) and tm.startswith("TRANSPORT_LORA"):
        return "lora"
    if tm in ("TRANSPORT_MQTT", "TRANSPORT_MULTICAST_UDP", "TRANSPORT_API"):
        return {"TRANSPORT_MQTT": "mqtt", "TRANSPORT_MULTICAST_UDP": "udp", "TRANSPORT_API": "api"}[tm]
    if packet.get("rxSnr") is None and packet.get("rxRssi") is None:
        return "local"
    return "unknown"


def strip_text_prefix(line):
    return TEXT_PREFIX.sub("", line, count=1)


def parse_rx(message):
    """Every header field from a firmware 'Lora RX (...)' line, or None."""
    if not message.startswith("Lora RX ("):
        return None
    return _parse_header(message)


def parse_tx(message):
    """Every header field from a firmware 'Started Tx (...)' line (our radio transmitting), or None."""
    if not message.startswith("Started Tx ("):
        return None
    h = _parse_header(message, tx=True)
    if h:
        for k in ("snr", "rssi"):
            h.pop(k, None)
    return h


def _parse_header(message, tx=False):
    f = dict(RX_FIELDS.findall(message))
    try:
        frm = int(f["fr"], 16)
        hop_start, hop_lim = int(f["hopStart"]), int(f["HopLim"])
    except (KeyError, ValueError):
        return None
    relay = int(f["relay"], 16) if f.get("relay") else None
    to = int(f["to"], 16) if f.get("to") else None
    return {"from_id": node_id(frm), "relay": relay or None,
            "pkt_id": int(f["id"], 16) if f.get("id") else None,
            # 2.7 prints the channel hash in hex ("Ch=0x1f"), 2.8.1 in decimal ("Ch=31"): read what's there
            "channel": (int(f["Ch"], 16) if f["Ch"].lower().startswith("0x") else int(f["Ch"])) if f.get("Ch") else None,
            "directed": None if to is None else int(to != BROADCAST_NUM),
            "to_id": None if to is None else ("^all" if to == BROADCAST_NUM else node_id(to)),
            "want_ack": int(f["WantAck"]) if f.get("WantAck") else None,
            "next_hop": int(f["nextHop"], 16) if f.get("nextHop") else None,
            "length": int(f["len"]) if f.get("len") else None,
            "encrypted": int(" encrypted " in message),
            "transport": int(f["transport"]) if f.get("transport") else None,
            "hop_start": hop_start, "hop_limit": hop_lim,
            "hops": hop_start - hop_lim if hop_start >= hop_lim else None,
            "snr": float(f["rxSNR"]) if "rxSNR" in f else None,
            "rssi": int(float(f["rxRSSI"])) if "rxRSSI" in f else None,
            **({"priority": int(f["priority"]) if f.get("priority") else None} if tx else {})}


DEBUG_RETENTION_DAYS = CFG["storage"]["debug_log_days"]  # high-volume; lives in its own short-lived DB

log = logging.getLogger("meshdash")

# ---------------------------------------------------------------- storage

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
]
# The database's layout version, stored in the file (PRAGMA user_version). Updates only ever ADD tables, columns
# and indexes (MIGRATIONS above), never remove or rewrite logged data. Bump it with each change to SCHEMA or
# MIGRATIONS, so an older Lorakeet opening a newer database can say so.
SCHEMA_VERSION = 2  # 2: received_via on every station table
BROADCAST = "^all"
MAX_TEXT_BYTES = 200
DB_WAIT_S = 30        # how long a write or read waits for the database before giving up
SLOW_REQUEST_S = 5    # requests slower than this are logged (with the path), to find what holds the database
TRACEROUTE_COOLDOWN_S = 30  # firmware rate-limits traceroutes too; be a good neighbour on a shared channel
UNKNOWN_HOP = 0xFFFFFFFF
UNK_SNR = -128


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
        self.db.commit()
        self.lock = threading.Lock()

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

    def insert(self, table, **row):
        if table in STATION_TABLES and "station" not in row and self.station:
            row["station"] = self.station
        cols = ",".join(row)
        marks = ",".join("?" * len(row))
        with self.lock:
            cur = self.db.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", list(row.values()))
            self.db.commit()
            return cur.lastrowid

    def execute(self, sql, *args):
        with self.lock:
            self.db.execute(sql, args)
            self.db.commit()

    def query(self, sql, *args):
        with self.lock:
            return [dict(r) for r in self.db.execute(sql, args).fetchall()]


class DebugLog:
    """The radio firmware's own debug log, in a separate database with a one-week window.

    Lines arrive in bursts (several per packet), so they're queued and written in batches by one
    thread; the radio's reader thread never waits on disk.
    """

    LEVELS = {0: "UNSET", 5: "TRACE", 10: "DEBUG", 20: "INFO", 30: "WARN", 40: "ERROR", 50: "CRIT"}
    COLS = ("ts", "device_time", "level", "source", "message")

    def __init__(self, path):
        self.path = path
        self.on_batch = None  # called with each written batch (list of dicts) — feeds the live viewer
        self.q = queue.Queue(maxsize=50_000)
        self.dropped = 0
        threading.Thread(target=self._writer, daemon=True, name="debuglog").start()

    def add(self, level, source, message, device_time=None):
        try:
            self.q.put_nowait((time.time(), device_time, level, source, message))
        except queue.Full:
            self.dropped += 1  # disk stalled for minutes; shedding debug lines beats blocking the radio

    def _writer(self):
        db = sqlite3.connect(self.path)
        db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS debug_log (
              ts REAL, device_time INTEGER, level TEXT, source TEXT, message TEXT);
            CREATE INDEX IF NOT EXISTS debug_log_ts ON debug_log(ts);
        """)
        last_prune = 0
        while True:
            batch = [self.q.get()]
            deadline = time.time() + 2
            while time.time() < deadline and len(batch) < 5000:
                try:
                    batch.append(self.q.get(timeout=max(0.0, deadline - time.time())))
                except queue.Empty:
                    break
            try:
                db.executemany("INSERT INTO debug_log VALUES (?,?,?,?,?)", batch)
                db.commit()
                if self.on_batch:
                    self.on_batch([dict(zip(self.COLS, row)) for row in batch])
                if time.time() - last_prune > 3600:
                    db.execute("DELETE FROM debug_log WHERE ts < ?", (time.time() - DEBUG_RETENTION_DAYS * 86400,))
                    db.commit()
                    last_prune = time.time()
            except Exception:  # noqa: BLE001 - never let the debug log take anything else down
                log.exception("debug log write failed (%d lines lost)", len(batch))

    def query(self, limit, since=None, contains=None, before=None, levels=None, source=None):
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


# ---------------------------------------------------------------- mesh link


def node_id(num):
    return f"!{num:08x}"


def node_status_text(payload):
    """The status string from a NODE_STATUS_APP payload (base64 protobuf, field 1 = string).

    Library 2.7.11 has no message type for this port, so it hands us the raw bytes. Only the summary
    is derived here; the stored raw JSON keeps the payload exactly as received."""
    import base64
    try:
        b = base64.b64decode(payload or "")
        i, out = 0, []
        while i < len(b):
            key, shift = 0, 0
            while True:  # varint tag
                key |= (b[i] & 0x7F) << shift
                shift += 7
                i += 1
                if not b[i - 1] & 0x80:
                    break
            field, wire = key >> 3, key & 7
            if wire == 0:  # varint
                while b[i] & 0x80:
                    i += 1
                i += 1
            elif wire == 2:  # length-delimited
                n, shift = 0, 0
                while True:
                    n |= (b[i] & 0x7F) << shift
                    shift += 7
                    i += 1
                    if not b[i - 1] & 0x80:
                        break
                if field == 1:
                    out.append(b[i:i + n].decode("utf-8", "replace"))
                i += n
            else:
                break  # fixed32/64 never appear here; stop rather than misparse
        return " ".join(out)[:120]
    except (IndexError, ValueError):
        return ""


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


# ---- on-air channel hashes, reimplemented from firmware src/mesh/Channels.cpp (v2.7.26):
# hash = xorHash(name) ^ xorHash(expanded key). Verified: LongFast + default key -> 0x08, as observed on air.
DEFAULT_PSK = bytes([0xd4, 0xf1, 0xbb, 0x3a, 0x20, 0x29, 0x07, 0x59, 0xf0, 0xbc, 0xff, 0xab, 0xcf, 0x4e, 0x69, 0x01])
PRESET_NAMES = {"SHORT_TURBO": "ShortTurbo", "SHORT_SLOW": "ShortSlow", "SHORT_FAST": "ShortFast",
                "MEDIUM_SLOW": "MediumSlow", "MEDIUM_FAST": "MediumFast", "LONG_SLOW": "LongSlow",
                "LONG_FAST": "LongFast", "LONG_TURBO": "LongTurbo", "LONG_MODERATE": "LongMod"}


def _xor(b):
    h = 0
    for x in b:
        h ^= x
    return h


def _expand_psk(psk):
    """Channels::getKey: 1-byte keys select a well-known default (0 = no encryption); short keys are
    zero-padded, which doesn't change an XOR hash."""
    if len(psk) == 1:
        return b"" if psk[0] == 0 else DEFAULT_PSK[:-1] + bytes([(DEFAULT_PSK[-1] + psk[0] - 1) & 0xFF])
    return bytes(psk)


def radio_channels(iface):
    """Every enabled channel on our radio with its on-air hash. Keys are used here, in memory only."""
    from meshtastic.protobuf import channel_pb2, config_pb2

    lora = iface.localNode.localConfig.lora
    preset = config_pb2.Config.LoRaConfig.ModemPreset.Name(lora.modem_preset)
    default_name = PRESET_NAMES.get(preset, "Invalid") if lora.use_preset else "Custom"
    chans = [c for c in (iface.localNode.channels or []) if c.role != channel_pb2.Channel.Role.DISABLED]
    primary_psk = next((bytes(c.settings.psk) for c in chans if c.role == channel_pb2.Channel.Role.PRIMARY), b"")
    out = []
    for c in chans:
        psk = bytes(c.settings.psk)
        if not psk and c.role == channel_pb2.Channel.Role.SECONDARY:
            psk = primary_psk  # unset secondary key falls back to the primary's
        key = _expand_psk(psk)
        name = c.settings.name or default_name
        out.append({"index": c.index, "role": channel_pb2.Channel.Role.Name(c.role), "name": name,
                    "hash": _xor(name.encode()) ^ _xor(key),
                    "encrypted": bool(key),
                    # 1-byte keys are the publicly known defaults: anyone can read those channels
                    "publicKey": len(psk) == 1 and psk[0] != 0})
    return out


def radio_lora(iface, chans):
    """Modulation + frequency settings (packet anatomy's radio layer). Nothing secret. The frequency slot
    comes from the primary channel's name."""
    from meshtastic.protobuf import config_pb2
    lora = iface.localNode.localConfig.lora
    L = config_pb2.Config.LoRaConfig
    return {"region": L.RegionCode.Name(lora.region), "preset": L.ModemPreset.Name(lora.modem_preset),
            "use_preset": lora.use_preset, "bandwidth": lora.bandwidth, "spread_factor": lora.spread_factor,
            "coding_rate": lora.coding_rate, "channel_num": lora.channel_num,
            "override_frequency": lora.override_frequency, "frequency_offset": lora.frequency_offset,
            "channel_name": next((c["name"] for c in chans if c["role"] == "PRIMARY"), None)}


def channel_keys(iface):
    """On-air channel hash -> AES key, from the live radio. In memory only: never stored or returned."""
    from meshtastic.protobuf import channel_pb2
    keys = {}
    chans = [c for c in (iface.localNode.channels or []) if c.role != channel_pb2.Channel.Role.DISABLED]
    primary_psk = next((bytes(c.settings.psk) for c in chans if c.role == channel_pb2.Channel.Role.PRIMARY), b"")
    for ch, c in zip(radio_channels(iface), chans):
        psk = bytes(c.settings.psk) or (primary_psk if c.role == channel_pb2.Channel.Role.SECONDARY else b"")
        key = anatomy._aes_key(_expand_psk(psk))
        if key:
            keys[ch["hash"]] = key
    return keys


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
    if DEMO:  # made up, like the rest of the demo: never this computer's platform, uptime or disk
        return {"name": "Demo Home", "version": VERSION, "platform": "demo", "radioHw": "HELTEC_V3",
                "radioFirmware": "2.7.26", "uptimeS": 3 * 86400, "backlog": 0}
    md = getattr(mesh.iface, "metadata", None) if mesh.iface is not None else None
    loc = CFG["station"]["location"]
    rep = {"name": CFG["station"]["name"] or (mesh.name(mesh.local_id) if mesh.local_id else ""),
           "note": CFG["station"]["note"], "software": SOFTWARE, "version": VERSION, "platform": f"{sys.platform} {platform.machine()}",
           "uptimeS": _uptime_s(), "diskFreeMB": round(shutil.disk_usage(DATA_DIR).free / 1048576),
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


def packet_anatomy(store, mesh, rowid):
    """One logged packet rebuilt layer by layer (anatomy.py), matched with the radio's own RX log line."""
    rows = store.query("SELECT rowid, ts, from_id, pkt_id, hops, relay, pki, raw, station FROM packets WHERE rowid=?", rowid)
    if not rows or not rows[0]["raw"]:
        raise ValueError("no such packet")
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
    flagged = keyflags.flags(DATA_DIR / "mesh.db")
    to = row["raw"].get("toId")
    out["keyFlags"] = [{"role": role, "id": nid, "name": mesh.name(nid), **flagged[nid], "text": keyflags.text(flagged[nid], mesh.name)}
                       for role, nid in (("sender", row["from_id"]), ("recipient", to)) if nid in flagged]
    return out


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


# USB vendors of Meshtastic radios, in order of preference. Espressif first, so a desk Heltec is always
# picked as before; then Adafruit's ID, used by most nRF52 boards' firmware (RAK4631, T-Echo, SenseCAP
# T1000-E: 239a:8029); then Seeed Studio's own. The meshtastic library whitelists the first two.
RADIO_VIDS = (ESPRESSIF_VID, 0x239A, 0x2886)


def serial_ports():
    """USB serial ports for the setup page, likely radios first."""
    out = [{"device": p.device, "description": p.description or "", "radio": p.vid in RADIO_VIDS,
            "vid": f"{p.vid:04x}" if p.vid else None} for p in list_ports.comports()]
    return sorted(out, key=lambda x: (not x["radio"], x["device"]))


PRESET_HASHES = {_xor(n.encode()) ^ _xor(DEFAULT_PSK) for n in PRESET_NAMES.values()}


def channel_fingerprints(store):
    """This radio's channels as on-air fingerprints only (never names or keys): {"publicHashes", "privateHashes"}.
    Stations send it in their report, so the hub can judge each station's traffic by that station's own channels."""
    r = store.query("SELECT value FROM settings WHERE key='radio_channels'")
    chans = json.loads(r[0]["value"]).get("channels", []) if r else []
    if not chans:
        return {}
    return {"publicHashes": sorted({c["hash"] for c in chans if c.get("publicKey")}),
            "privateHashes": sorted({c["hash"] for c in chans if c.get("encrypted") and not c.get("publicKey")})}


def public_channel_hashes(store, own=()):
    """station -> the on-air fingerprints of channels anyone can read, AS THAT STATION'S RADIO SEES THEM: every preset
    name with the well-known key, plus its channels with a public key, minus any fingerprint one of ITS private
    channels shares (a fingerprint is one byte, so a collision proves nothing). This hub's own radios use its channel
    list; other stations the fingerprints in their reports; a station that never reported them (an older Lorakeet, a
    peer's station) has none: nothing it logged is proven public (sync.public_keys)."""
    mine = channel_fingerprints(store)
    r = store.query("SELECT value FROM settings WHERE key='station_meta'")
    metas = json.loads(r[0]["value"]) if r else {}
    r = store.query("SELECT value FROM settings WHERE key='own_station'")
    own = set(own) | ({r[0]["value"]} if r else set())  # stored as the bare id

    def hashes(station):
        fp = mine if station in own else {k: (metas.get(station) or {}).get(k) for k in ("publicHashes", "privateHashes")}
        if fp.get("publicHashes") is None and fp.get("privateHashes") is None:
            return set() if station not in own else set(PRESET_HASHES)
        return (PRESET_HASHES | set(fp.get("publicHashes") or ())) - set(fp.get("privateHashes") or ())
    return hashes


class PeerManager:
    """The hubs this one sends to (Stations page -> Peers): one sync.PeerSender each, started at launch and as peers
    are added, stopped as they're removed."""

    def __init__(self, store, mesh, hub_id):
        self.store, self.mesh, self.hub_id = store, mesh, hub_id
        self.peers = pairing.Peers(DATA_DIR / "peers.json")
        self.senders = {}

    def _report(self, station):
        if station == self.mesh.local_id:
            return station_report(self.mesh)
        r = self.store.query("SELECT value FROM settings WHERE key='station_meta'")
        m = (json.loads(r[0]["value"]) if r else {}).get(station)
        return {k: v for k, v in m.items() if k != "received"} if m else None

    def _start(self, peer):
        snd = sync_mod.PeerSender(self.store, peer, self.hub_id,
                                  lambda: public_channel_hashes(self.store, {self.mesh.local_id}), self._report)
        self.senders[peer["id"]] = snd
        snd.start()

    def start_all(self):
        for peer in self.peers.all():
            self._start(peer)

    def add(self, name, url, token, share, forward, hub_id, exact=False):
        peer = self.peers.add(name, url, token, share, forward, hub_id, exact)
        self._start(peer)
        return peer

    def update(self, pid, **kw):
        peer = self.peers.update(pid, **kw)
        if peer and pid in self.senders:
            self.senders[pid].peer.update({k: peer.get(k) for k in ("share", "forward", "name", "url", "exact", "paused")})
            self.senders[pid].sync_now()
        return peer

    def remove(self, pid):
        snd = self.senders.pop(pid, None)
        if snd:
            snd.stop()
        self.peers.remove(pid)

    def list(self):
        out = []
        for p in self.peers.list():
            snd = self.senders.get(p["id"])
            out.append({**p, "status": dict(snd.status) if snd else None})
        return out


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


def setup_info(mesh):
    """What the setup page shows: whether this install is configured, the defaults, ports, the live link."""
    import config as cfgmod
    info = {"configured": CFG["_path"] is not None, "path": str(cfgmod.config_path()),
            "defaults": {"dataDir": str(cfgmod.default_data_dir()), "httpPort": CFG["http"]["port"]},
            "ports": serial_ports(), "status": mesh.status(),
            "radioName": mesh.name(mesh.local_id) if mesh.connected and mesh.local_id else None,
            "radioFix": mesh.own_fix_now() if mesh.connected else None,
            "supervised": bool(os.environ.get("LORAKEET_SUPERVISED") or os.environ.get("INVOCATION_ID"))}
    if info["configured"]:  # a read-only summary; the file itself is edited by hand
        r = CFG["radio"]
        info["summary"] = {"radio": r["host"] and f"{r['host']}:{r['tcp_port']}" or r["port"] or "auto-detect (USB)",
                           "dataDir": str(CFG["storage"]["data_dir"]), "lan": CFG["http"]["lan"],
                           "storeRecipients": CFG["logging"]["store_recipients"], "tiles": CFG["map"]["tiles"],
                           "stationName": CFG["station"]["name"], "location": bool(CFG["station"]["location"])}
    return info


def find_port():
    ports = list_ports.comports()
    for vid in RADIO_VIDS:
        for p in sorted(ports, key=lambda p: p.device):
            if p.vid == vid:
                return p.device
    return None


class Mesh:
    def __init__(self, store, port_hint, debug_log=None, host=None, tcp_port=4403):
        """port_hint: a serial port (else auto-detect). host: a radio on the network instead (TCP API)."""
        self.store = store
        self.debug_log = debug_log
        self.port_hint = port_hint
        self.host, self.tcp_port = host or None, tcp_port
        self.iface = None
        self.connected = False
        self.connected_at = None
        self.paused = False  # logging paused (tray / dashboard): the radio's port is let go until resumed
        self.port = None
        self.local_id = None
        self.rx = {}  # node id -> {"rssi","snr","ts","count"} as heard by our radio
        # firmware debug-log records received / RX-TX header lines mined from them (alerts.py watches the
        # pair: lines still arriving but nothing mined means the firmware's log format changed)
        self.log_counts = {"lines": 0, "mined": 0}
        self.remote = None  # remote.Remote when [remote] is enabled: "lk ..." commands by direct message
        self.rx_remote = {}  # node id -> newest reception at ANOTHER listening station (sync hub): {station, ts, snr, rssi, hops}
        self.station_ids = set()  # every listening station seen (ours + collectors)
        r = store.query("SELECT MAX(ts) AS ts FROM packets WHERE relay=? AND hops>0", BASE_BYTE) if BASE_ID else None
        self.base_relay_ts = r[0]["ts"] if r else None
        for r in store.query("SELECT from_id, MAX(ts) AS ts, snr, rssi, hops FROM packets GROUP BY from_id"):
            self.rx[r["from_id"]] = {"ts": r["ts"], "snr": r["snr"], "rssi": r["rssi"], "hops": r["hops"], "count": 0}
        # latest position per node from our own log; the library's node DB doesn't always keep them
        self.pos = {r["node"]: r for r in store.query(
            "SELECT node, MAX(ts) AS ts, lat, lon, alt, precision_bits FROM positions GROUP BY node")}
        self.last_traceroute = 0
        self._own_fix = None  # our radio's last logged GPS fix (_track_self); None = not looked up yet
        self._own_refresh = 0  # last node-list request for our exact position (_refresh_own_fix)
        self._ping = {"n": 0, "pos": None, "ts": 0}  # drive pings: count, where and when the last one went
        self._clock_checked = False
        self.last_connect_error = None
        self.known = {r["node"]: r for r in store.query(
            "SELECT node, MAX(ts), long_name, short_name, hw_model, role FROM node_info GROUP BY node")}
        self.last_rx = 0          # any packet from the radio, including its own telemetry (watchdog)
        self.identity = {r["node"]: r["sig"] for r in store.query(
            "SELECT node, MAX(ts), IFNULL(long_name,'')||'|'||IFNULL(short_name,'')||'|'||IFNULL(hw_model,'')||'|'"
            "||IFNULL(role,'')||'|'||IFNULL(public_key,'') AS sig FROM node_info GROUP BY node")}
        self.node_cache = {}      # last known node DB, served while the radio is unplugged
        self.lock = threading.RLock()
        self.subscribers = set()
        self.sub_lock = threading.Lock()
        pub.subscribe(self._on_receive, "meshtastic.receive")
        pub.subscribe(self._on_lost, "meshtastic.connection.lost")
        pub.subscribe(self._on_node, "meshtastic.node.updated")
        pub.subscribe(self._on_log_line, "meshtastic.log.line")

    # -- connection management (reconnects forever)
    def run(self):
        if self.host:
            from meshtastic.tcp_interface import TCPInterface

            def SerialInterface(_label, timeout, connectNow):  # noqa: N802 - same shape for _connect
                # connectNow=False leaves the socket closed; _connect's connect() opens it (TCPInterface.connect
                # calls myConnect itself: opening it here as well made a second connection, and a radio serves
                # one client at a time, so it kept dropping us)
                return TCPInterface(self.host, portNumber=self.tcp_port, timeout=timeout, connectNow=False)
        else:
            from meshtastic.serial_interface import SerialInterface

        last_snapshot = last_follow = 0
        while True:
            try:
                if self.connected and not self.paused and time.time() - self.last_rx > WATCHDOG_S:
                    log.warning("no data from radio for %d s; reconnecting", WATCHDOG_S)
                    self.event("watchdog", silent_s=WATCHDOG_S)
                    self._drop(self.iface)
                if not self.connected and not self.paused:
                    self._connect(SerialInterface)
                if self.connected:
                    if CFG["station"]["mobile"] and time.time() - self._own_refresh > OWN_FIX_REFRESH_S:
                        self._refresh_own_fix()
                    self._track_self()
                    if CFG["station"]["drive_pings"]:
                        self._drive_ping()
                if time.time() - last_snapshot > 86400:
                    self._snapshot_nodedb()
                    last_snapshot = time.time()
                if time.time() - last_follow > 300:
                    follow_base()
                    last_follow = time.time()
            except Exception:  # noqa: BLE001 - this loop must never die
                log.exception("connection loop error")
            time.sleep(5)

    def event(self, kind, **detail):
        try:
            self.store.insert("events", ts=time.time(), kind=kind, detail=dumps(detail) if detail else None)
        except Exception:  # noqa: BLE001 - logging must never break the link
            log.exception("could not record event %s", kind)

    def _pick_port(self):
        if self.host:  # a network radio: the "port" is its address, shown wherever the COM port would be
            return f"{self.host}:{self.tcp_port}"
        if self.port_hint:  # a configured port is used strictly: with several radios on USB, never pick another
            return self.port_hint if self.port_hint in {p.device for p in list_ports.comports()} else None
        return find_port()  # auto-detect (the COM number can change after a replug)

    def _connect(self, SerialInterface):  # noqa: N803
        port = self._pick_port()
        if not port:
            return
        log.info("connecting to %s", port)
        iface = None
        try:
            iface = SerialInterface(port, timeout=CONNECT_TIMEOUT_S, connectNow=False)
            # The library flattens each firmware LogRecord to bare text and drops its level, source
            # and timestamp. Intercept records before that happens (set before connecting so boot-time
            # records are caught too). connect + waitForConfig is exactly what connectNow=True does.
            iface._handleLogRecord = lambda rec, _i=iface: self._on_log_record(rec, _i)
            iface.connect()
            iface.waitForConfig()
        except Exception as e:  # noqa: BLE001 - port busy, radio booting, etc.; retry next loop
            if iface is not None:
                self._close_quietly(iface)  # connectNow=False skips the constructor's own cleanup
            log.warning("connect to %s failed: %s", port, e)
            if str(e) != self.last_connect_error:  # retries every 5 s; record each distinct failure once
                self.event("connect_failed", port=port, error=str(e))
            self.last_connect_error = str(e)
            return
        try:
            with self.lock:
                # paused while it was connecting (up to 45 s, e.g. to flash a radio that was booting): let go now, or
                # the port stays held while the tray says it's free (set_paused only drops a connection already in).
                # Checked in the same lock as the connection is set, so a pause can't slip in between.
                if self.paused:
                    log.info("logging was paused while connecting: letting go of %s", port)
                    threading.Thread(target=self._close_quietly, args=(iface,), daemon=True).start()
                    return
                self.iface = iface
                self.port = port
                self.local_id = node_id(iface.myInfo.my_node_num)
                self.store.claim_station(self.local_id)
                load_station_locations(self.store, self.local_id)
                self._load_remote()
                self.node_cache = iface.nodes or {}
                self.last_rx = time.time()
                self.connected = True
                self.connected_at = time.time()
            log.info("connected: %s on %s, %d nodes", self.local_id, port, len(self.node_cache))
            self.last_connect_error = None
            self.event("connected", port=port, local=self.local_id, nodes=len(self.node_cache),
                       my_info=iface.myInfo, metadata=iface.metadata,
                       config=redacted_config(iface.localNode.localConfig), module_config=redacted_module_config(iface.localNode.moduleConfig),
                       channels=redacted_channels(iface.localNode.channels))
            self._seed_history()
            try:  # what our radio can read: drives the readable/private split in analytics
                chans = radio_channels(iface)
                self.store.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('radio_channels', ?)",
                                   json.dumps({"ts": time.time(), "channels": chans}))
                log.info("radio channels: %s", ", ".join(f"{c['name']}=0x{c['hash']:02x}" for c in chans))
                self.store.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('radio_lora', ?)",
                                   json.dumps({"ts": time.time(), **radio_lora(iface, chans)}))
            except Exception:  # noqa: BLE001
                log.exception("could not read radio channels")
            for n in list(self.node_cache.values()):
                self._track_identity(n.get("user", {}).get("id") or node_id(n["num"]), n.get("user"))
            self.broadcast("status", self.status())
        except Exception:  # noqa: BLE001
            log.exception("post-connect setup failed")
            self._drop(iface)

    def set_paused(self, paused):
        """Pause logging: let go of the radio (its USB port is free for the Meshtastic app, the CLI or a flasher)
        until resumed. The paused time is a gap in the log, like any time Lorakeet wasn't running."""
        paused = bool(paused)
        if paused == self.paused:
            return
        self.paused = paused
        log.info("logging %s", "paused: letting go of the radio" if paused else "resumed")
        self.event("logging_paused" if paused else "logging_resumed")
        if paused and self.iface is not None:
            self._drop(self.iface)
        self.broadcast("status", self.status())

    def _drop(self, iface):
        """Forget a connection. Safe to call from any thread, including the library's reader thread."""
        with self.lock:
            if iface is None or iface is not self.iface:
                return  # a late event from an older connection; the current one is fine
            self.node_cache = dict(iface.nodes or {})
            self.iface = None
            self.connected = False
        self.event("disconnected", port=self.port)
        # close() joins the reader thread, so never run it on that thread, and never let it block us
        threading.Thread(target=self._close_quietly, args=(iface,), daemon=True).start()
        self.broadcast("status", self.status())

    @staticmethod
    def _close_quietly(iface):
        try:
            iface.close()
        except Exception:  # noqa: BLE001 - the device is usually already gone
            pass

    def _on_log_record(self, rec, iface):
        if self.debug_log and (iface is self.iface or not self.connected):
            self.debug_log.add(DebugLog.LEVELS.get(rec.level, str(rec.level)), rec.source or None,
                               rec.message, rec.time or None)
        rx = parse_rx(rec.message)
        tx = None if rx else parse_tx(rec.message)
        self.log_counts["lines"] += 1
        if rx or tx:
            self.log_counts["mined"] += 1
            try:
                self._store_header(rx, tx)
            except Exception:  # noqa: BLE001 - mining must never disturb the radio link
                log.exception("rx/tx log insert failed")

    def _store_header(self, rx, tx):
        h = dict(rx or tx)
        if rx and not keep_recipient(h.get("from_id"), h.get("to_id"), self.local_id):
            h["to_id"] = None  # directed flag is kept; only who it was for is dropped
        self.store.insert("rx_hops" if rx else "tx_log", ts=time.time(), **h)

    def _on_log_line(self, line, interface, **_):
        # Plain-text lines only arrive outside protobuf mode (e.g. boot chatter before the API starts).
        line = ANSI.sub("", line).rstrip()
        if self.debug_log and line.strip():
            self.debug_log.add("TEXT", None, line)
        # the same RX/TX lines appear here as plain text while the radio boots; mine them too
        msg = strip_text_prefix(line)
        rx = parse_rx(msg)
        tx = None if rx else parse_tx(msg)
        if rx or tx:
            try:
                self._store_header(rx, tx)
            except Exception:  # noqa: BLE001
                log.exception("rx/tx log insert failed (text line)")

    def _on_lost(self, interface, **_):
        log.warning("radio connection lost")
        self._drop(interface)

    # -- other listening stations (this server as a sync hub)
    def _load_remote(self):
        """What the other stations have heard, from the log: who, when, how strongly."""
        self.station_ids = {r["station"] for r in self.store.query(
            "SELECT DISTINCT station FROM packets WHERE station IS NOT NULL")} | {self.local_id}
        rr = {}
        for r in self.store.query("SELECT from_id, station, MAX(ts) AS ts, snr, rssi, hops FROM packets "
                                  "WHERE station IS NOT NULL AND station != ? GROUP BY from_id, station", self.local_id):
            if r["from_id"] not in self.station_ids and (r["from_id"] not in rr or r["ts"] > rr[r["from_id"]]["ts"]):
                rr[r["from_id"]] = dict(r)
        self.rx_remote = rr

    def station_names(self):
        return {s: self.describe(s)["name"] if self.describe(s)["name"] != s else
                (self.known.get(s) or {}).get("long_name") or s for s in sorted(s for s in self.station_ids if s)}

    def ingested(self, station, table, src_rowids):
        """A collector's batch was stored (sync.Hub): fold it into the live map and the traffic feed."""
        if not src_rowids:
            return
        self.station_ids.add(station)
        marks = ",".join("?" * len(src_rowids))
        rows = self.store.query(f"SELECT rowid, * FROM {table} WHERE station = ? AND src_rowid IN ({marks}) ORDER BY ts",
                                station, *src_rowids)
        touched = set()
        if table == "positions":
            for r in rows:
                cur = self.pos.get(r["node"])
                if not cur or r["ts"] > (cur.get("ts") or 0):
                    self.pos[r["node"]] = {k: r[k] for k in ("node", "ts", "lat", "lon", "alt", "precision_bits")}
                    touched.add(r["node"])
        elif table == "node_info":
            for r in rows:
                self.known[r["node"]] = {k: r[k] for k in ("node", "long_name", "short_name", "hw_model", "role")}
                touched.add(r["node"])
        elif table == "packets":
            names = self.station_names()
            for r in rows:
                frm = r["from_id"]
                if frm in self.station_ids:
                    continue  # a station hearing another station's own radio: internal traffic
                cur = self.rx_remote.get(frm)
                if not cur or r["ts"] >= cur["ts"]:
                    self.rx_remote[frm] = {"from_id": frm, "station": station, "ts": r["ts"], "snr": r["snr"],
                                           "rssi": r["rssi"], "hops": r["hops"]}
                touched.add(frm)
                if r["ts"] > time.time() - 600:  # live only: a catch-up after an outage doesn't flood the feed
                    raw = json.loads(r["raw"]) if r["raw"] else {}
                    self.broadcast("packet", {k: r[k] for k in ("rowid", "ts", "from_id", "to_id", "portnum", "channel", "snr",
                                                                "rssi", "hops", "via_mqtt", "summary", "relay", "pkt_id", "pki")}
                                   | {"name": self.name(frm), "arrival": arrival_of(raw) if raw else None,
                                      "station": station, "stationName": names.get(station, station)})
        for nid in touched:
            self.broadcast("node", self.node_json(nid))

    def _nodes(self):
        iface = self.iface
        return (iface.nodes if iface is not None else self.node_cache) or {}

    def _track_self(self, fix=None):
        """Log our radio's own GPS fix (positions, source 'own') when it's a new fix and we've moved, or on a
        heartbeat. Only real fixes count: a position without a fix time is a stored or fixed one, which
        says nothing about where a moving station is now."""
        sid = self.local_id
        if not sid:
            return
        if fix is None:
            me = next((n for n in self._nodes().values() if (n.get("user") or {}).get("id") == sid), None)
            fix = (me or {}).get("position") or {}
        lat, lon, t = fix.get("latitude"), fix.get("longitude"), fix.get("time")
        if lat is None or lon is None or not t or (lat == 0 and lon == 0):
            return
        if (fix.get("precisionBits") or 32) < 32:
            return  # rounded to a channel's precision (our broadcast copy): a box kilometres wide, not a track point
        last = self._own_fix
        if last is None:
            r = self.store.query("SELECT ts, lat, lon, fix_time FROM positions WHERE node=? AND station=? "
                                 "AND source='own' ORDER BY ts DESC LIMIT 1", sid, sid)
            last = self._own_fix = r[0] if r else {}
        if last.get("fix_time") == t:
            return  # the same fix, seen again
        now = time.time()
        if last.get("lat") is not None and now - last["ts"] < TRACK_HEARTBEAT_S \
                and analytics.dist_m(last["lat"], last["lon"], lat, lon) < TRACK_MIN_MOVE_M:
            return
        row = dict(ts=now, node=sid, lat=lat, lon=lon, alt=fix.get("altitude"), precision_bits=fix.get("precisionBits"),
                   source="own", fix_time=t)
        self.store.insert("positions", **row)
        self._own_fix = row
        self.pos[sid] = {k: row[k] for k in ("node", "ts", "lat", "lon", "alt", "precision_bits")}
        self.broadcast("node", self.node_json(sid))

    def _drive_ping(self):
        """Drive mode: a short ping on a private channel every drive_ping_m metres moved, at most once per
        drive_ping_min_s, never while parked. drive.py then maps where each one was heard (or not)."""
        st = CFG["station"]
        fix = self.own_fix_now()
        if not fix or time.time() - fix["time"] > 60:
            return  # no fresh exact position: we wouldn't know where the ping was sent from
        here, now = (fix["lat"], fix["lon"]), time.time()
        if self._ping["pos"] is None:
            self._ping.update(pos=here, ts=now)  # start counting from here
            return
        if not ping_due(self._ping["pos"], self._ping["ts"], here, now, st["drive_ping_m"], st["drive_ping_min_s"]):
            return
        chan = next((c for c in radio_channels(self.iface) if c["name"].lower() == st["drive_ping_channel"].lower()), None)
        if not chan or chan["publicKey"] or not chan["encrypted"]:
            if self._ping.get("warned") != st["drive_ping_channel"]:
                log.warning("drive pings: no private channel named %r on this radio; not sending", st["drive_ping_channel"])
                self._ping["warned"] = st["drive_ping_channel"]
            return
        self._ping["n"] += 1
        try:
            self.send_text(f"Lorakeet ping #{self._ping['n']}", channel=chan["index"])
        except Exception as e:  # noqa: BLE001 - a missed ping must never disturb logging
            log.warning("drive ping failed: %s", e)
        self._ping.update(pos=here, ts=now)

    def own_fix_now(self):
        """Our radio's current exact GPS fix from its node list: {lat, lon, alt, time} or None."""
        me = next((n for n in self._nodes().values() if (n.get("user") or {}).get("id") == self.local_id), None)             if self.local_id else None
        p = (me or {}).get("position") or {}
        if p.get("latitude") is None or not p.get("time") or (p.get("precisionBits") or 32) < 32:
            last = self._own_fix or {}
            return ({"lat": last["lat"], "lon": last["lon"], "alt": last.get("alt"), "time": last.get("fix_time") or last["ts"]}
                    if last.get("lat") is not None else None)
        return {"lat": p["latitude"], "lon": p["longitude"], "alt": p.get("altitude"), "time": p["time"]}

    def _refresh_own_fix(self):
        """Ask the radio for its node list (only that), so our own entry carries the exact GPS position.
        Then, once, make sure the system clock is right (set_clock_from_gps)."""
        self._own_refresh = time.time()
        iface = self.iface
        fw = getattr(getattr(iface, "metadata", None), "firmware_version", "") or ""
        try:
            if tuple(int(x) for x in fw.split(".")[:2]) < (2, 7):
                return  # older firmware may answer with the full config (channels again): don't
        except ValueError:
            return
        from meshtastic import mesh_pb2
        try:
            iface._sendToRadio(mesh_pb2.ToRadio(want_config_id=ONLY_NODES_CONFIG_ID))
        except Exception as e:  # noqa: BLE001 - the watchdog handles a dead link
            log.warning("own-position refresh failed: %s", e)
            return
        if not self._clock_checked:
            me = (iface.nodesByNum or {}).get(iface.myInfo.my_node_num) or {}
            p = me.get("position") or {}
            if p.get("locationSource") == "LOC_INTERNAL" and p.get("time"):
                self._clock_checked = True
                set_clock_from_gps(p["time"], self.event)

    def _snapshot_nodedb(self):
        nodes = self._nodes()
        if nodes:
            self.store.insert("nodedb_snapshots", ts=time.time(), data=dumps(nodes))

    def _track_identity(self, nid, user):
        """Insert a node_info row when a node's name/hardware/role/key first appears or changes."""
        if not user:
            return
        # role is omitted from the dict when it's the default; normalise so it matches what we store
        user = {**user, "role": user.get("role") or "CLIENT"}
        sig = "|".join(str(user.get(k) or "") for k in ("longName", "shortName", "hwModel", "role", "publicKey"))
        if self.identity.get(nid) == sig:
            return
        self.identity[nid] = sig
        self.known[nid] = {"node": nid, "long_name": user.get("longName"), "short_name": user.get("shortName"),
                           "hw_model": user.get("hwModel"), "role": user.get("role", "CLIENT")}
        self.store.insert("node_info", ts=time.time(), node=nid, long_name=user.get("longName"),
                          short_name=user.get("shortName"), hw_model=user.get("hwModel"),
                          role=user.get("role", "CLIENT"), public_key=user.get("publicKey"),
                          is_licensed=int(bool(user.get("isLicensed"))), data=dumps(user))

    def status(self):
        return {"connected": self.connected, "paused": self.paused, "port": self.port, "localId": self.local_id,
                "baseId": BASE_ID, "baseName": BASE_NAME, "stations": self.station_names(), "demo": bool(DEMO)}

    # -- history seeding from the radio's own node DB
    def _seed_history(self):
        for n in list(self._nodes().values()):
            nid = n.get("user", {}).get("id") or node_id(n["num"])
            ts = n.get("lastHeard")
            if not ts:
                continue
            p = n.get("position") or {}
            if p.get("latitude") is not None and not self._have("positions", nid, ts):
                self.store.insert("positions", ts=ts, node=nid, lat=p["latitude"], lon=p["longitude"],
                                  alt=p.get("altitude"), precision_bits=p.get("precisionBits"))
            dm = n.get("deviceMetrics") or {}
            if dm and not self._have("telemetry", nid, ts):
                self.store.insert("telemetry", ts=ts, node=nid, battery=dm.get("batteryLevel"),
                                  voltage=dm.get("voltage"), ch_util=dm.get("channelUtilization"),
                                  air_util=dm.get("airUtilTx"), uptime=dm.get("uptimeSeconds"),
                                  temperature=None, humidity=None, pressure=None)

    def _have(self, table, nid, ts):
        return bool(self.store.query(f"SELECT 1 FROM {table} WHERE node=? AND ts=? LIMIT 1", nid, ts))

    # -- live packets
    def _on_node(self, node, interface, **_):
        nid = node.get("user", {}).get("id") or node_id(node["num"])
        self.broadcast("node", self.node_json(nid))

    def _on_receive(self, packet, interface, **_):
        if interface is not self.iface:
            return  # straggler from a connection we've already dropped
        self.last_rx = time.time()
        try:
            self._handle(packet)
        except Exception as e:  # noqa: BLE001 - one bad packet must not kill the reader thread
            log.exception("packet handling error: %r", e)

    def _handle(self, packet):
        now = time.time()
        frm = packet.get("fromId") or node_id(packet.get("from", 0))
        to = packet.get("toId") or node_id(packet.get("to", 0))
        d = packet.get("decoded") or {}
        port = d.get("portnum", "ENCRYPTED" if "encrypted" in packet else "UNKNOWN")
        hop_start, hop_limit = packet.get("hopStart"), packet.get("hopLimit")
        hops = hop_start - hop_limit if hop_start is not None and hop_limit is not None else None
        snr, rssi = packet.get("rxSnr"), packet.get("rxRssi")
        summary = ""
        relay = packet.get("relayNode")
        # Every telemetry variant is kept in full, including our own radio's per-minute local stats
        # (packets tx/rx, bad CRCs, dupes, nodes online: the best view of how busy the mesh is here).
        if port == "TELEMETRY_APP":
            for kind, data in (d.get("telemetry") or {}).items():
                if isinstance(data, dict) and kind != "raw":
                    self.store.insert("telemetry_full", ts=now, node=frm, kind=kind, data=dumps(data))
        if frm == self.local_id and port == "TELEMETRY_APP":
            return  # ...but our USB-powered radio's self-reports stay out of the packet feed
        if BASE_ID and relay and hops and relay == BASE_BYTE:
            self.base_relay_ts = now

        if frm != self.local_id and (snr is not None or rssi is not None):
            r = self.rx.setdefault(frm, {"count": 0})
            r.update(snr=snr, rssi=rssi, ts=now, hops=hops)
            r["count"] += 1

        if port == "TEXT_MESSAGE_APP":
            text = d.get("text", "")
            summary = text[:120]
            msg = dict(ts=now, from_id=frm, to_id=to, channel=packet.get("channel", 0), text=text,
                       pkt_id=packet.get("id"), outgoing=0, status=None)
            self.store.insert("messages", **msg)
            self.broadcast("message", {**msg, "name": self.name(frm)})
            if self.remote:
                self.remote.handle(packet, text)
        elif port == "POSITION_APP":
            p = d.get("position") or {}
            if frm == self.local_id:  # our own radio's position handed to us: a track point, not a reception
                self._track_self(p)
                p = {}
            if p.get("latitude") is not None:
                row = dict(ts=now, node=frm, lat=p["latitude"], lon=p["longitude"],
                           alt=p.get("altitude"), precision_bits=p.get("precisionBits"))
                self.store.insert("positions", **row)
                self.pos[frm] = row
                summary = f"{p['latitude']:.4f}, {p['longitude']:.4f}"
        elif port == "TELEMETRY_APP":
            t = d.get("telemetry") or {}
            dm, em = t.get("deviceMetrics"), t.get("environmentMetrics")
            pm = t.get("powerMetrics")
            if pm:
                summary = ", ".join(f"{k} {v:.2f}" for k, v in pm.items() if isinstance(v, (int, float)))
            if dm or em:
                dm, em = dm or {}, em or {}
                self.store.insert("telemetry", ts=now, node=frm, battery=dm.get("batteryLevel"),
                                  voltage=dm.get("voltage"), ch_util=dm.get("channelUtilization"),
                                  air_util=dm.get("airUtilTx"), uptime=dm.get("uptimeSeconds"),
                                  temperature=em.get("temperature"), humidity=em.get("relativeHumidity"),
                                  pressure=em.get("barometricPressure"))
                if dm.get("voltage") is not None:
                    summary = f"{dm.get('batteryLevel', '?')}% {dm['voltage']:.2f} V"
                elif em:
                    summary = ", ".join(f"{k} {v:.1f}" for k, v in em.items() if isinstance(v, (int, float)))
        elif port == "NEIGHBORINFO_APP":
            ni = d.get("neighborinfo") or {}
            for nb in ni.get("neighbors", []):
                self.store.insert("links", ts=now, a=frm, b=node_id(nb["nodeId"]), snr=nb.get("snr"),
                                  source="neighborinfo")
            summary = f"{len(ni.get('neighbors', []))} neighbors"
        elif port == "TRACEROUTE_APP" and to == self.local_id:
            summary = "traceroute reply"  # parsed (both directions + SNR) by the request's callback
        elif port == "TRACEROUTE_APP":
            route = [frm] + [node_id(x) for x in (d.get("traceroute") or {}).get("route", [])] + [to]
            for a, b in zip(route, route[1:]):
                self.store.insert("links", ts=now, a=a, b=b, snr=None, source="traceroute")
            summary = " → ".join(self.name(x) for x in route)
        elif port == "NODEINFO_APP":
            summary = (d.get("user") or {}).get("longName", "")
            self._track_identity(frm, d.get("user"))
        elif port == "ROUTING_APP":
            err = (d.get("routing") or {}).get("errorReason", "NONE")
            summary = "ack" if err == "NONE" else err.replace("_", " ").lower()
            # A recipient's ACK can arrive after our radio has already given up ("max retransmit") and
            # the library has dropped its response handler. Catch it here so the message shows delivered.
            req = d.get("requestId")
            if req and err == "NONE" and frm != self.local_id:
                m = self.store.query("SELECT to_id, status FROM messages WHERE pkt_id = ? AND outgoing = 1", req)
                if m and m[0]["to_id"] == frm and m[0]["status"] != "delivered":
                    self.store.execute("UPDATE messages SET status = 'delivered' WHERE pkt_id = ? AND outgoing = 1", req)
                    self.broadcast("msgstatus", {"pkt_id": req, "status": "delivered"})
                    self.event("late_ack", pkt_id=req, from_id=frm, previous=m[0]["status"])
                    summary = "ack (late delivery confirmation)"
        elif port == "ENCRYPTED" and to == self.local_id:
            # A DM we couldn't decrypt: almost always a missing public key. Surface it in Messages.
            summary = "direct message we couldn't decrypt"
            msg = dict(ts=now, from_id=frm, to_id=to, channel=packet.get("channel", 0), text="",
                       pkt_id=packet.get("id"), outgoing=0, status=None, encrypted=1)
            self.store.insert("messages", **msg)
            self.broadcast("message", {**msg, "name": self.name(frm)})
        elif port == "NODE_STATUS_APP":
            summary = node_status_text(d.get("payload"))
        elif port == "TELEMETRY_APP" and not summary:
            kinds = [k for k in (d.get("telemetry") or {}) if k not in ("time", "raw")]
            summary = ", ".join(kinds)

        # direct (0-hop) reception is a real RF link from that node to our radio
        if hops == 0 and frm != self.local_id and self.local_id:
            self.store.insert("links", ts=now, a=frm, b=self.local_id, snr=snr, source="direct")

        row = dict(ts=now, from_id=frm, to_id=to, portnum=port, channel=packet.get("channel", 0),
                   snr=snr, rssi=rssi, hops=hops, via_mqtt=int(bool(packet.get("viaMqtt"))), summary=summary,
                   relay=relay, pkt_id=packet.get("id"), pki=int(bool(packet.get("pkiEncrypted"))))
        stored = packet
        if not keep_recipient(frm, to, self.local_id):
            row["to_id"] = None
            stored = {**packet, "to": None, "toId": None}  # the raw record must not leak it either
        rowid = self.store.insert("packets", **row, raw=dumps(stored))
        self.broadcast("packet", {**row, "rowid": rowid, "name": self.name(frm), "arrival": arrival_of(packet)})
        self.broadcast("node", self.node_json(frm))
        if self.base_relay_ts == now:
            self.broadcast("node", self.node_json(BASE_ID))

    # -- sending
    def _require_link(self):
        if not (self.connected and self.iface):
            raise RuntimeError("radio is not connected")

    def hop_limit_for(self, target, iface):
        """Hop limit for a directed packet: enough to reach where we last heard the node, never below the
        radio's configured limit, never above the protocol max of 7. Shared by DMs and traceroutes."""
        known = self._nodes().get(target, {}).get("hopsAway")
        if known is None:
            known = self.rx.get(target, {}).get("hops")
        if known is None:  # fall back to the log: the most recent hop count we recorded for it
            r = self.store.query("SELECT hops FROM packets WHERE from_id = ? AND hops IS NOT NULL "
                                 "ORDER BY ts DESC LIMIT 1", target)
            known = r[0]["hops"] if r else None
        cfg_limit = iface.localNode.localConfig.lora.hop_limit or 3
        return min(7, max(cfg_limit, (known or 0) + 1)), known, cfg_limit

    def send_text(self, text, to=None, channel=0):
        """channel: the radio's channel index (0 = primary); it must be one the radio has enabled."""
        from meshtastic.protobuf import portnums_pb2

        self._require_link()
        text = (text or "").strip()
        if not text:
            raise ValueError("message is empty")
        if len(text.encode("utf-8")) > MAX_TEXT_BYTES:
            raise ValueError(f"message is longer than {MAX_TEXT_BYTES} bytes")
        dest = to or BROADCAST
        state = {"status": "sending", "pkt_id": None}

        def set_status(status):
            if state["status"] in ("delivered", status):  # never downgrade a confirmed delivery
                return
            state["status"] = status
            if state["pkt_id"] is not None:
                self.store.execute("UPDATE messages SET status=? WHERE pkt_id=? AND outgoing=1",
                                   status, state["pkt_id"])
                self.broadcast("msgstatus", {"pkt_id": state["pkt_id"], "status": status})

        def on_response(p):
            err = ((p.get("decoded") or {}).get("routing") or {}).get("errorReason", "NONE")
            frm = p.get("fromId") or node_id(p.get("from", 0))
            if err != "NONE":
                set_status("failed: " + err.replace("_", " ").lower())
            elif dest != BROADCAST and frm == dest:
                set_status("delivered")  # end-to-end ACK from the recipient
            else:
                set_status("relayed")  # implicit ACK: we heard a neighbour rebroadcast it

        # sendText() doesn't forward onResponseAckPermitted, so ACKs would never reach us
        iface = self.iface
        if iface is None:
            raise RuntimeError("radio is not connected")
        try:
            channel = int(channel or 0)
        except (TypeError, ValueError):
            raise ValueError("channel must be a channel index") from None
        chans = getattr(iface.localNode, "channels", None) or []
        if not (0 <= channel < 8) or (channel and not any(c.index == channel and c.role != 0 for c in chans)):
            raise ValueError(f"channel {channel} isn't enabled on this radio")
        if dest == BROADCAST:
            hop_limit, known = iface.localNode.localConfig.lora.hop_limit or 3, None
        else:
            hop_limit, known, _ = self.hop_limit_for(dest, iface)
        pkt = iface.sendData(text.encode("utf-8"), destinationId=dest,
                             portNum=portnums_pb2.PortNum.TEXT_MESSAGE_APP, wantAck=True,
                             onResponse=on_response, onResponseAckPermitted=True, channelIndex=channel,
                             hopLimit=hop_limit)
        state["pkt_id"] = pkt.id
        msg = dict(ts=time.time(), from_id=self.local_id, to_id=dest, channel=channel, text=text,
                   pkt_id=pkt.id, outgoing=1, status=state["status"], hop_limit=hop_limit)
        self.store.insert("messages", **msg)
        self.event("sent_text", to=dest, pkt_id=pkt.id, channel=channel, bytes=len(text.encode("utf-8")),
                   hop_limit=hop_limit, known_hops=known)
        self.broadcast("message", {**msg, "name": self.name(self.local_id)})
        threading.Timer(90, lambda: state["status"] == "sending" and set_status("no ack")).start()
        return msg

    def traceroute(self, target, origin="manual"):
        from meshtastic.protobuf import mesh_pb2, portnums_pb2

        self._require_link()
        if not target or target in (self.local_id, BROADCAST):
            raise ValueError("pick a remote node to trace")
        wait = TRACEROUTE_COOLDOWN_S - (time.time() - self.last_traceroute)
        if wait > 0:
            raise ValueError(f"wait {wait:.0f} s between traceroutes")
        self.last_traceroute = time.time()

        iface = self.iface
        if iface is None:
            raise RuntimeError("radio is not connected")
        hop_limit, _known, _cfg = self.hop_limit_for(target, iface)
        started = time.time()
        row_id = self.store.insert("traceroutes", ts=started, target=target, status="pending",
                                   forward=None, back=None, done_ts=None, origin=origin)
        done = {"v": False}

        def finish(status, forward=None, back=None):
            if done["v"]:
                return
            done["v"] = True
            end = time.time()
            self.store.execute("UPDATE traceroutes SET status=?, forward=?, back=?, done_ts=? WHERE rowid=?",
                               status, json.dumps(forward) if forward else None,
                               json.dumps(back) if back else None, end, row_id)
            self.broadcast("traceroute", self._trace_json(dict(
                ts=started, target=target, status=status, forward=forward, back=back, done_ts=end)))

        def on_response(p):
            d = p.get("decoded") or {}
            if d.get("portnum") == "ROUTING_APP":
                err = (d.get("routing") or {}).get("errorReason", "NONE")
                if err != "NONE":
                    finish("failed: " + err.replace("_", " ").lower())
                return
            rd = mesh_pb2.RouteDiscovery()
            rd.ParseFromString(d["payload"])
            forward = self._hops(self.local_id, list(rd.route), target, list(rd.snr_towards))
            back = None
            if len(rd.snr_back) == len(rd.route_back) + 1:
                back = self._hops(target, list(rd.route_back), self.local_id, list(rd.snr_back))
            for path in (forward, back or []):
                for a, b in zip(path, path[1:]):
                    if a["id"] and b["id"]:
                        self.store.insert("links", ts=time.time(), a=a["id"], b=b["id"], snr=b["snr"],
                                          source="traceroute")
            finish("ok", forward, back)

        iface.sendData(mesh_pb2.RouteDiscovery(), destinationId=target,
                            portNum=portnums_pb2.PortNum.TRACEROUTE_APP, wantResponse=True,
                            onResponse=on_response, channelIndex=0, hopLimit=hop_limit)
        threading.Timer(30 * hop_limit, lambda: finish("timed out")).start()
        return {"target": target, "status": "pending", "hopLimit": hop_limit, "ts": started}

    @staticmethod
    def _hops(start, middle, end, snrs):
        """Path as [{id, snr}]; snr is what that hop measured receiving from the previous one."""
        ids = [start] + [None if n == UNKNOWN_HOP else node_id(n) for n in middle] + [end]
        out = [{"id": ids[0], "snr": None}]
        for i, nid in enumerate(ids[1:]):
            s = snrs[i] if i < len(snrs) and snrs[i] != UNK_SNR else None
            out.append({"id": nid, "snr": s / 4 if s is not None else None})
        return out

    def _trace_json(self, r):
        def named(path):
            if isinstance(path, str):
                path = json.loads(path)
            if not path:
                return None
            return [{**h, "name": self.name(h["id"]) if h["id"] else "unknown relay"} for h in path]
        return {**r, "forward": named(r.get("forward")), "back": named(r.get("back"))}

    # -- serialization
    def describe(self, nid):
        """Display identity for analytics: live node DB first, then our identity log."""
        n = (self._nodes().get(nid) or {}).get("user") or {}
        k = self.known.get(nid) or {}
        name = n.get("longName") or k.get("long_name") or (BASE_NAME if nid == BASE_ID else nid)
        announced = bool(n.get("longName") or k)
        # The library omits role when it's the default CLIENT, so CLIENT is only implied for nodes that
        # actually announced themselves; nodes we've never heard a NodeInfo from have an unknown role.
        role = n.get("role") or k.get("role") or ("CLIENT" if announced else None)
        return {"name": name, "short": n.get("shortName") or k.get("short_name") or nid[-4:],
                "hw": n.get("hwModel") or k.get("hw_model"), "role": role, "announced": announced,
                "isBase": nid == BASE_ID, "isLocal": nid == self.local_id, "keyFlag": key_flag(nid),
                "v28": likely_28(nid)}

    def name(self, nid):
        n = self._nodes().get(nid)
        return ((n or {}).get("user", {}).get("longName") or (self.known.get(nid) or {}).get("long_name")
                or (BASE_NAME if nid == BASE_ID else nid))

    def node_json(self, nid):
        n = self._nodes().get(nid)
        if not n:
            if BASE_ID and nid == BASE_ID:
                # not in the radio's node DB yet: placeholder so the base card is always there
                n = {"num": int(BASE_ID[1:], 16), "user": {"longName": BASE_NAME, "shortName": BASE_NAME}}
            elif nid in self.pos or nid in self.rx or nid in self.rx_remote:
                n = {"num": int(nid[1:], 16), "user": {}}
            else:
                return None
        u, p, dm = n.get("user", {}), dict(n.get("position") or {}), n.get("deviceMetrics") or {}
        if not u.get("longName") and nid in self.known:  # radio forgot it; our identity log didn't
            k = self.known[nid]
            u = {"longName": k["long_name"], "shortName": k["short_name"], "hwModel": k["hw_model"], "role": k["role"]}
        if p.get("latitude") is None and nid in self.pos:
            lp = self.pos[nid]
            p.update(latitude=lp["lat"], longitude=lp["lon"], altitude=lp["alt"], precisionBits=lp["precision_bits"])
        station_loc = analytics.station_location(nid)
        if p.get("latitude") is None and station_loc:  # a listening station: its configured antenna location
            p.update(latitude=station_loc[0], longitude=station_loc[1], altitude=station_loc[2], precisionBits=32)
        em = n.get("environmentMetrics") or {}
        rx = self.rx.get(nid, {})
        rr = self.rx_remote.get(nid)
        return {
            "id": nid, "num": n.get("num"),
            "longName": u.get("longName") or nid, "shortName": u.get("shortName") or nid[-4:],
            "hwModel": u.get("hwModel"), "role": u.get("role", "CLIENT"),
            "lat": p.get("latitude"), "lon": p.get("longitude"), "alt": p.get("altitude"),
            "precisionBits": p.get("precisionBits"), "positionTime": p.get("time"),
            # nodes without a set clock report 1970-era times; treat those as unknown
            "lastHeard": max((n.get("lastHeard") or 0) if (n.get("lastHeard") or 0) > 1.6e9 else 0, rx.get("ts") or 0,
                             (self.base_relay_ts or 0) if nid == BASE_ID else 0, (rr or {}).get("ts") or 0) or None,
            "hopsAway": n.get("hopsAway", rx.get("hops")),
            # heard by another listening station: its own reading (ours stay ours)
            "remote": {"station": rr["station"], "stationName": self.station_names().get(rr["station"], rr["station"]),
                       "ts": rr["ts"], "snr": rr["snr"], "rssi": rr["rssi"], "hops": rr["hops"]} if rr else None,
            "heardHere": bool(rx.get("ts") or n.get("lastHeard")),
            "isStation": nid in self.station_ids and nid != self.local_id,
            "positionFromStation": bool(station_loc) and not (n.get("position") or {}).get("latitude") and nid not in self.pos,
            "snr": n.get("snr", rx.get("snr")),
            "rssi": rx.get("rssi"), "rxCount": rx.get("count", 0), "viaMqtt": n.get("viaMqtt", False),
            "battery": dm.get("batteryLevel"), "voltage": dm.get("voltage"),
            "chUtil": dm.get("channelUtilization"), "airUtil": dm.get("airUtilTx"),
            "uptime": dm.get("uptimeSeconds"),
            "temperature": em.get("temperature"), "humidity": em.get("relativeHumidity"),
            "isLocal": nid == self.local_id, "isBase": nid == BASE_ID,
            "relayOnly": nid == BASE_ID and rx.get("ts") is None and n.get("lastHeard") is None,
            "v28": likely_28(nid),
            "keyFlag": dict(kf, withNames=[self.name(o) for o in kf["with"]]) if (kf := key_flag(nid)) else None,
        }

    def state(self):
        nodes = []
        for nid in set(self._nodes()) | set(self.pos) | set(self.rx) | set(self.rx_remote) | ({BASE_ID} if BASE_ID else set()):
            j = self.node_json(nid)
            if j:
                nodes.append(j)
        return {"status": self.status(), "nodes": nodes, "now": time.time()}

    # -- SSE fan-out
    def subscribe(self):
        q = queue.Queue(maxsize=500)
        with self.sub_lock:
            self.subscribers.add(q)
        return q

    def unsubscribe(self, q):
        with self.sub_lock:
            self.subscribers.discard(q)

    def broadcast(self, kind, data):
        if data is None:
            return
        msg = f"event: {kind}\ndata: {json.dumps(data, default=str)}\n\n"
        with self.sub_lock:
            for q in list(self.subscribers):
                try:
                    q.put_nowait(msg)
                except queue.Full:
                    self.subscribers.discard(q)


# ---------------------------------------------------------------- http

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
    login = login or login_mod.Login(DATA_DIR / "login.json")
    pairings = pairings or pairing.Pairings(DATA_DIR / "stations.json")
    if isinstance(syncer, sync_mod.Hub):
        syncer.is_revoked = pairings.is_revoked
    hub_id = peering.hub_id if peering else pairing.install_id(DATA_DIR)

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
                            "ORDER BY ts DESC LIMIT 1", sid)
            name = r[0]["long_name"] if r else sid
        return name

    class Handler(BaseHTTPRequestHandler):
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

        def _ingest(self, hello=False):
            if not isinstance(syncer, sync_mod.Hub):
                return self._json({"error": "this Lorakeet isn't a hub"}, 404)
            ip = self.client_address[0]
            if not sync_mod.allowed_source(ip, CFG["sync"]["allow"]):
                log.warning("ingest refused from %s: not an allowed network", ip)
                return self._json({"error": "not from an allowed network"}, 403)
            claimed = self.headers.get("X-Lorakeet-Station") or ""
            if claimed and not analytics.STATION_RE.fullmatch(claimed):
                return self._json({"error": "bad station header"}, 400)
            given = (self.headers.get("Authorization") or "").removeprefix("Bearer ").strip()
            from_hub = (self.headers.get("X-Lorakeet-Hub") or "")[:64]
            hi = {"ok": True, "hub": hub_name(), "version": VERSION, "hubId": hub_id}
            e = pairings.find(given)
            if e is not None:  # made on the Stations page: a station's pairing, or a peer hub's
                if e["revoked"]:
                    log.warning("ingest refused from %s (%s): pairing revoked", ip, claimed)
                    return self._json({"error": "this pairing was revoked"}, 401)
                if e.get("kind") == "peer":
                    if e.get("hub") and from_hub and e["hub"] != from_hub:
                        log.warning("ingest refused from %s: peer pairing belongs to another hub", ip)
                        return self._json({"error": "this pairing code belongs to a different hub"}, 403)
                    if hello:
                        return self._json({**hi, "paused": True} if e.get("paused") else hi)
                    if not claimed or not from_hub:
                        return self._json({"error": "send X-Lorakeet-Station and X-Lorakeet-Hub"}, 401)
                    if not pairings.bind(e["id"], hub=from_hub):
                        return self._json({"error": "this pairing code belongs to a different hub"}, 403)
                    pairings.seen(e["id"])
                    if e.get("paused"):  # the sender keeps these pending (not acknowledged) and sends them on resume
                        return self._json({"error": "paused by the receiving hub (nothing is lost: it's sent when they "
                                                    "resume)"}, 423)
                    return self._ingest_body(ip, claimed, via=from_hub, writer=f"peer:{from_hub}")
                if e["station"] and claimed and e["station"] != claimed:
                    log.warning("ingest refused from %s (%s): pairing belongs to another station", ip, claimed)
                    return self._json({"error": "this pairing code belongs to a different station"}, 403)
                if hello:
                    return self._json(hi)
                if not claimed:
                    return self._json({"error": "send X-Lorakeet-Station"}, 401)
                writer = f"pair:{e['id']}"
                if e["station"] is None:  # first use: the station must be free for it before the code is claimed
                    try:
                        syncer.claim(claimed, writer)
                    except sync_mod.IngestError as err:
                        log.warning("ingest refused from %s (%s): %s", ip, claimed, err)
                        return self._json({"error": str(err)}, err.status)
                    if not pairings.bind(e["id"], station=claimed):
                        return self._json({"error": "this pairing code belongs to a different station"}, 403)
                return self._ingest_body(ip, claimed, writer=writer)
            tokens = sync_mod.follow_tokens(sync_mod.station_tokens(CFG["sync"]["station_tokens"]),
                                            nodeids.info(DATA_DIR / "mesh.db")["aliases"])
            bound = sync_mod.authorize(self.headers.get("Authorization"), claimed or None, CFG["sync"]["token"], tokens,
                                       CFG["sync"]["require_station_tokens"])
            if bound is None and claimed:
                log.warning("ingest refused from %s (%s): bad token", ip, claimed)
                return self._json({"error": "bad token"}, 401)
            if bound is None and not sync_mod.check_token(self.headers.get("Authorization"), CFG["sync"]["token"]):
                # an older collector that doesn't name itself: shared token only
                log.warning("ingest refused from %s: bad token", ip)
                return self._json({"error": "bad token"}, 401)
            if not claimed and (CFG["sync"]["require_station_tokens"] or sync_mod.station_tokens(CFG["sync"]["station_tokens"])):
                return self._json({"error": "send X-Lorakeet-Station (this hub uses per-station tokens)"}, 401)
            if hello:
                return self._json(hi)
            return self._ingest_body(ip, claimed, writer="token" if claimed in tokens else "shared")

        def _ingest_body(self, ip, claimed, via=None, writer="shared"):
            if not INGEST_SLOTS.acquire(timeout=30):  # a few batches at a time: each may decompress to 50 MB
                return self._json({"error": "busy, try again shortly"}, 503)
            try:
                n = self._length()
                if n > sync_mod.MAX_BODY:
                    raise sync_mod.IngestError(413, "batch too large")
                body, self._body_read = self.rfile.read(n), True
                batch = sync_mod.decode_body(body, self.headers.get("Content-Encoding"))
                return self._json(syncer.ingest(batch, bound_station=claimed or None, via=via, writer=writer))
            except sync_mod.IngestError as e:
                log.warning("ingest from %s refused: %s", ip, e)
                return self._json({"error": str(e)}, e.status)
            except ValueError as e:
                return self._json({"error": str(e)}, 400)
            except Exception as e:  # noqa: BLE001
                log.exception("ingest failed")
                return self._json({"error": f"ingest failed: {e}"}, 500)
            finally:
                INGEST_SLOTS.release()

        def _drain(self, cap=DRAIN_MAX):
            """After refusing a request without reading its body: read the rest (up to `cap`) before the connection
            closes. Closing with unread data resets the connection (Windows: WinError 10053), so the sender saw
            "unreachable" instead of the refusal."""
            if self._body_read:
                return
            try:
                n = self._length()
            except ValueError:
                n = cap + 1
            if n > cap:
                self.close_connection = True
                return
            while n > 0:
                chunk = self.rfile.read(min(n, 65536))
                if not chunk:
                    break
                n -= len(chunk)

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
            LAST_REQUEST[0] = time.time()
            if not self._host_ok():
                return self._wrong_host()
            t0 = time.time()
            try:
                return self._get()
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
                return self._json({"version": VERSION, "software": SOFTWARE, "python": sys.version.split()[0],
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
                return self._json(api_index.index(VERSION))
            if path == "/api/keyflags":  # radios with a compromised or shared public key (keyflags.py)
                f = keyflags.flags(DATA_DIR / "mesh.db")
                return self._json({nid: dict(v, name=mesh.name(nid), text=keyflags.text(v, mesh.name)) for nid, v in f.items()})
            if path == "/api/stations/timeline":  # power and network changes each station recorded (HealthWatch)
                hours = min(24 * 31, max(1, float(qs.get("hours", 24))))
                out = {}
                for r in store.query("SELECT ts, kind, detail, station FROM events WHERE kind IN ('power', 'network') "
                                     "AND ts >= ? ORDER BY ts", time.time() - hours * 3600):
                    out.setdefault(r["station"] or "", []).append({"ts": r["ts"], "kind": r["kind"], **json.loads(r["detail"] or "{}")})
                return self._json({"hours": hours, "stations": out})
            if path == "/api/brief":  # one-line status for the tray icon
                return self._json({"version": VERSION, "connected": mesh.connected, "paused": mesh.paused,
                                   "port": mesh.port, "id": mesh.local_id,
                                   "name": mesh.name(mesh.local_id) if mesh.local_id else None})
            if path == "/api/whoami":
                return self._json({"access": access, "readOnly": access != "full", "version": VERSION, "paused": mesh.paused,
                                   "demo": bool(DEMO),
                                   "login": {"enabled": login.enabled, "loggedIn": access == "full" and base != "full",
                                             "lan": CFG["http"]["lan"]}})
            if DEMO and _demo_refuses("GET", path):
                return self._json(DEMO_REFUSAL, 403)
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
            if path in ("/", "/index.html") and CFG["_path"] is None and base == "full" and "skipsetup" not in qs and not DEMO:
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
                return self._json(store.query(
                    "WITH st AS (SELECT DISTINCT station AS id FROM packets WHERE station IS NOT NULL), "
                    "p AS (SELECT rowid AS rid, *, ROW_NUMBER() OVER (PARTITION BY CASE WHEN pkt_id IS NULL THEN rowid "
                    "ELSE from_id || ':' || pkt_id END ORDER BY ts) AS n FROM packets WHERE ts > ? "
                    "AND NOT (from_id IN (SELECT id FROM st) AND from_id != station)) "
                    "SELECT rid AS rowid, ts, from_id, to_id, portnum, channel, snr, rssi, hops, via_mqtt, summary, relay, "
                    f"pkt_id, pki, station, {packetsearch.ARRIVAL_SQL} AS arrival, "
                    "(SELECT GROUP_CONCAT(DISTINCT q.station) FROM packets q WHERE q.from_id = p.from_id AND q.pkt_id = p.pkt_id) "
                    "AS stations FROM p WHERE n = 1 ORDER BY ts DESC LIMIT ?", time.time() - 3 * 86400, limit))
            if path.startswith("/api/packet/") and path.endswith("/anatomy"):
                try:
                    return self._json(packet_anatomy(store, mesh, int(path.split("/")[3])))
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
                    return self._json(airtime.compute(DATA_DIR / "mesh.db", qs.get("range", "7d"), station, describe))
                except ValueError as e:
                    return self._json({"error": str(e)}, 400)
                except Exception as e:  # noqa: BLE001
                    log.exception("airtime failed")
                    return self._json({"error": f"airtime failed: {e}"}, 500)
            if path == "/api/analytics/stations":
                try:
                    return self._json(compare.matrix(DATA_DIR / "mesh.db", qs.get("range", "7d"),
                                                     lambda i: {**mesh.describe(i), "name": station_name(i)}))
                except ValueError as e:
                    return self._json({"error": str(e)}, 400)
                except Exception as e:  # noqa: BLE001
                    log.exception("station matrix failed")
                    return self._json({"error": f"station matrix failed: {e}"}, 500)
            if path == "/api/analytics/compare":
                try:
                    others = [s["id"] for s in analytics.stations(DATA_DIR / "mesh.db") if s["id"] != mesh.local_id]
                    a = qs.get("a") or mesh.local_id
                    b = qs.get("b") or (others[0] if others else None)
                    out = compare.compute(DATA_DIR / "mesh.db", qs.get("range", "7d"), a, b,
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
                    return self._json(topology.compute(DATA_DIR / "mesh.db", qs.get("range", "7d"), station, describe))
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
                    return self._json(drive.compute(DATA_DIR / "mesh.db", qs.get("range", "24h"), qs.get("station") or "*", describe,
                                                    int(qs.get("bin", 250) or 250)))
                except ValueError as e:
                    return self._json({"error": str(e)}, 400)
                except Exception as e:  # noqa: BLE001
                    log.exception("drive coverage failed")
                    return self._json({"error": f"drive coverage failed: {e}"}, 500)
            if path == "/api/analytics/replay":
                try:
                    cache = {}
                    describe = lambda i: cache.setdefault(i, mesh.describe(i))  # noqa: E731
                    return self._json(topology.replay(DATA_DIR / "mesh.db", qs.get("range", "24h"), station, describe))
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
                        return self._send(node_analytics.node_csv(DATA_DIR / "mesh.db", nid, rng, station).encode("utf-8"),
                                          "text/csv; charset=utf-8",
                                          {"Content-Disposition": f'attachment; filename="mesh-{safe}-{rng}.csv"'})
                    cache = {}
                    describe = lambda i: cache.setdefault(i, mesh.describe(i))  # noqa: E731
                    return self._json(node_analytics.compute_node(DATA_DIR / "mesh.db", nid, rng, station, describe))
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
                rows = analytics.stations(DATA_DIR / "mesh.db")
                metas = station_meta()
                hub = (syncer.status()["stations"] if isinstance(syncer, sync_mod.Hub) else {})
                for r in rows:
                    here = r["id"] == mesh.local_id
                    m = station_report(mesh) if here else (metas.get(r["id"]) or {})
                    loc = analytics.station_location(r["id"])
                    r.update(name=station_name(r["id"]), current=here, location=list(loc) if loc else None,
                             report=m, lastContact=time.time() if here and mesh.connected else
                             (hub.get(r["id"]) or {}).get("last") or m.get("received"))
                return self._json({"stations": rows, "current": mesh.local_id, "software": SOFTWARE, "version": VERSION})
            if path == "/api/analytics":
                cache = {}
                describe = lambda nid: cache.setdefault(nid, mesh.describe(nid))  # noqa: E731
                try:
                    out = analytics.compute(DATA_DIR / "mesh.db", qs.get("range", "7d"), station, describe)
                    out.update(station=station, stationName=station_name(station), stationIsLocal=station == mesh.local_id,
                               combined=station == analytics.ALL_STATIONS, homeName=station_name(mesh.local_id),
                               stationNames=[station_name(s["id"]) for s in analytics.stations(DATA_DIR / "mesh.db")])
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
                dbp, rng = DATA_DIR / "mesh.db", qs.get("range", "7d")
                try:
                    if path == "/api/insights/estimates":
                        return self._json(insights.estimate_positions(dbp, station, BASE_ID, describe))
                    if path == "/api/insights/health":
                        return self._json(insights.health(dbp, rng, station or "", describe))
                    if path == "/api/insights/coverage":
                        return self._json(insights.coverage(dbp, rng, qs.get("node") or None, qs.get("relayed") == "1", station))
                    if path == "/api/insights/traceroutes":
                        return self._json(insights.traceroutes(dbp, rng, describe, station))
                    if path == "/api/packets/search":
                        if qs.get("keyflag"):
                            qs = dict(qs, keyflagNodes=keyflags.flags(dbp))
                        r = packetsearch.search(dbp, qs.get("source", "packets"), qs, qs.get("limit", 200), qs.get("offset", 0), station)
                        flagged = keyflags.flags(dbp)
                        for row in r["rows"]:
                            if row.get("from_id") in flagged:
                                row["from_keyflag"] = flagged[row["from_id"]]["kind"]
                            for k in ("from_id", "to_id"):
                                if row.get(k) and row[k].startswith("!"):
                                    row[k.replace("_id", "_name")] = describe(row[k])["name"]
                        return self._json(r)
                    if path == "/api/packets/search.csv":
                        src = qs.get("source", "packets")
                        if qs.get("keyflag"):
                            qs = dict(qs, keyflagNodes=keyflags.flags(dbp))
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
                st = {**storage.status(), "demo": bool(DEMO)}
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
            f = (STATIC / rel).resolve()
            if STATIC in f.parents and f.is_file():
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
                threading.Timer(1.0, lambda: os._exit(0)).start()

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
            if DEMO and _demo_refuses("POST", path):
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
                    if DEMO:
                        return self._json({"url": "/"})
                    out = start_demo(CFG["http"]["port"] + 1)
                    return self._json(out, 200 if out.get("url") else 503)
                if path.startswith("/api/hub/"):
                    if base != "full":
                        return self._json({"error": "the Stations page is only available on the dashboard PC"}, 403)
                    return self._hub_post(path, body)
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
                    threading.Timer(1.0, lambda: os._exit(0)).start()
                    return None
                if path == "/api/logging":  # pause / resume logging (the tray icon, the dashboard's paused chip)
                    mesh.set_paused(bool(body.get("paused")))
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

        # ---- the Radio page (radio_setup.py)
        def _radio_get(self, path, qs):
            iface = mesh.iface
            if path == "/api/radio":
                ports = serial_ports()
                out = {"connected": bool(mesh.connected and iface), "port": mesh.port, "network": bool(mesh.host),
                       "connectedAt": mesh.connected_at, "ports": ports,
                       "otherRadios": [p for p in ports if p["radio"] and p["device"] != mesh.port],
                       "error": mesh.last_connect_error, "hint": radio_setup.connect_hint(mesh.last_connect_error),
                       "noPortHint": None if mesh.host or any(p["radio"] for p in ports) else radio_setup.NO_PORT_HINT,
                       "backupDir": str(RADIO_BACKUPS), "mobile": CFG["station"]["mobile"],
                       "station": station_settings(mesh)}
                if out["connected"]:
                    node = iface.localNode
                    fw = getattr(getattr(iface, "metadata", None), "firmware_version", "") or ""
                    user = (iface.getMyNodeInfo() or {}).get("user", {})
                    out["radio"] = {"id": mesh.local_id, "longName": user.get("longName"), "shortName": user.get("shortName"),
                                    "hw": user.get("hwModel"), "firmware": fw}
                    out["checklist"] = radio_setup.checklist(node, fw, usb=not mesh.host, mobile=CFG["station"]["mobile"],
                                                             has_base=bool(BASE_ID))
                    out["channels"] = radio_setup.channel_list(node, _preset_name(node))
                    out["backups"] = radio_setup.backups(RADIO_BACKUPS, mesh.local_id)[:10]
                return self._json(out)
            if path == "/api/radio/location-check":  # would this antenna position be far from the radios we hear?
                try:
                    loc = [float(qs["lat"]), float(qs["lon"])]
                except (KeyError, ValueError):
                    return self._json({"error": "lat and lon, please"}, 400)
                return self._json({"check": radio_setup.location_check(loc, heard_points(mesh))})
            if path == "/api/radio/logging":  # is it logging? (the walkthrough's last step)
                since = float(qs.get("since") or time.time() - 600)
                st = mesh.local_id
                one = lambda sql: (store.query(sql, st, since) or [{"n": 0}])[0]["n"]  # noqa: E731
                debug = bool(iface and iface.localNode.localConfig.security.debug_log_api_enabled)
                return self._json({"connected": bool(mesh.connected and iface), "usb": not mesh.host, "since": since,
                                   "packets": one("SELECT COUNT(*) AS n FROM packets WHERE station = ? AND ts >= ?"),
                                   "receptions": one("SELECT COUNT(*) AS n FROM rx_hops WHERE station = ? AND ts >= ?"),
                                   "lastPacket": (store.query("SELECT MAX(ts) AS t FROM packets WHERE station = ?", st) or [{}])[0].get("t"),
                                   "debugLog": debug, "logLines": mesh.log_counts["lines"], "mined": mesh.log_counts["mined"]})
            if path == "/api/radio/share":  # a private channel's link and QR code, to add it in the Meshtastic app
                if not iface:
                    return self._json({"error": "no radio connected"}, 409)
                try:
                    c = radio_setup.find_channel(iface.localNode, int(qs.get("index", -1)))
                except (ValueError, radio_setup.RadioError) as e:
                    return self._json({"error": str(e)}, 400)
                if radio_setup.key_kind(c.settings.psk, c.role) != "private":
                    return self._json({"error": "only channels with their own private key are shared here"}, 400)
                url = radio_setup.share_url(c.settings)
                log.info("radio: share link shown for channel %d", c.index)
                return self._json({"name": c.settings.name, "url": url, "svg": radio_setup.qr_svg(url)})
            return self._json({"error": "not found"}, 404)

        def _radio_post(self, path, body):
            if path == "/api/radio/station":  # [station] name and location in lorakeet.toml
                import config as cfgmod
                if CFG["station"]["mobile"] and body.get("location"):
                    return self._json({"error": "this station moves ([station] mobile): its position comes from its radio's GPS"}, 400)
                try:
                    loc = body.get("location") or []
                    if loc and not (len(loc) == 2 and -90 <= float(loc[0]) <= 90 and -180 <= float(loc[1]) <= 180):
                        raise ValueError("latitude -90..90 and longitude -180..180, please")
                    path_ = cfgmod.update_station(body.get("name"), loc)
                except (ValueError, TypeError) as e:
                    return self._json({"error": str(e)}, 400)
                log.info("station settings saved to %s (name %r, location %s)", path_, body.get("name"), "set" if loc else "none")
                return self._json({"ok": True, "path": str(path_), "station": station_settings(mesh)})
            if path == "/api/radio/restart":
                if not supervised_now():
                    return self._json({"error": "Lorakeet isn't running under its background runner, so it can't restart "
                                                "itself: stop it and start it again (the Lorakeet shortcut starts it)"}, 409)
                log.info("restart requested from the Radio page")
                self._json({"ok": True})
                threading.Timer(1.0, lambda: os._exit(0)).start()
                return None
            iface = mesh.iface
            if not (mesh.connected and iface):
                return self._json({"error": "no radio connected"}, 409)
            if not RADIO_LOCK.acquire(timeout=1):
                return self._json({"error": "another change to the radio is still in progress"}, 409)
            try:
                node = iface.localNode
                if path == "/api/radio/apply":
                    fw = getattr(getattr(iface, "metadata", None), "firmware_version", "") or ""
                    items = radio_setup.checklist(node, fw, usb=not mesh.host, mobile=CFG["station"]["mobile"],
                                                  has_base=bool(BASE_ID))
                    changes = radio_setup.plan(items, body.get("changes"))
                    names = None
                    if body.get("names"):
                        names = radio_setup.check_names(body["names"].get("long"), body["names"].get("short"))
                        user = (iface.getMyNodeInfo() or {}).get("user", {})
                        if names == (user.get("longName"), user.get("shortName")):
                            names = None
                    if not changes and not names:
                        return self._json({"ok": True, "nothing": True})
                    bk = radio_setup.backup(iface, RADIO_BACKUPS, mesh.local_id, "before-settings", mesh.port)
                    sections = radio_setup.apply(iface, changes, names)
                    log.info("radio: wrote %s%s (backup %s)", ", ".join(sections) or "-", " + names" if names else "", bk.name)
                    mesh.event("radio_settings", changes=changes, names=bool(names), backup=bk.name)
                    # the radio restarts to apply most settings; if it doesn't, reconnect anyway so what the page
                    # shows next is read back from the radio, not our own edited copy
                    threading.Timer(15, lambda: mesh._drop(iface) if mesh.iface is iface else None).start()
                    return self._json({"ok": True, "backup": bk.name, "expected": changes,
                                       "names": list(names) if names else None})
                if path == "/api/radio/channel":
                    action = body.get("action")
                    if action == "create":
                        new = [radio_setup.new_channel(body.get("name"), 32 if body.get("exactPositions", True) else 13)]
                    elif action == "add-link":
                        new = radio_setup.parse_url(body.get("url"))
                    elif action == "copy-to":  # one of this radio's channels onto another radio plugged in here
                        c = radio_setup.find_channel(node, int(body.get("index", -1)))
                        if radio_setup.key_kind(c.settings.psk, c.role) != "private":
                            raise radio_setup.RadioError("only channels with their own private key can be copied")
                        return self._json(self._copy_channel(c.settings, str(body.get("port") or "")))
                    else:
                        raise radio_setup.RadioError("unknown channel action")
                    bk = radio_setup.backup(iface, RADIO_BACKUPS, mesh.local_id, "before-channel", mesh.port)
                    done = radio_setup.add_channels(node, new)
                    log.info("radio: channels %s (backup %s)", ", ".join(f"{n}@{i} {w}" for n, i, w in done), bk.name)
                    mesh.event("radio_channels", channels=[{"name": n, "index": i, "result": w} for n, i, w in done], backup=bk.name)
                    threading.Timer(15, lambda: mesh._drop(iface) if mesh.iface is iface else None).start()
                    return self._json({"ok": True, "backup": bk.name, "channels": [{"name": n, "index": i, "result": w} for n, i, w in done]})
                return self._json({"error": "not found"}, 404)
            except radio_setup.RadioError as e:
                return self._json({"error": str(e)}, 400)
            finally:
                RADIO_LOCK.release()

        def _copy_channel(self, settings, port):
            """Write a channel to ANOTHER radio on USB here (not the logging one): open it, back it up, add, close."""
            if not port or port == mesh.port or port not in {p["device"] for p in serial_ports()}:
                raise radio_setup.RadioError("pick another radio plugged into this computer")
            from meshtastic.serial_interface import SerialInterface
            try:
                other = SerialInterface(port, timeout=CONNECT_TIMEOUT_S)
            except Exception as e:  # noqa: BLE001
                raise radio_setup.RadioError(f"couldn't open {port}: {radio_setup.connect_hint(str(e)) or e}") from e
            try:
                oid = node_id(other.myInfo.my_node_num)
                bk = radio_setup.backup(other, RADIO_BACKUPS, oid, "before-channel", port)
                done = radio_setup.add_channels(other.localNode, [settings])
                log.info("radio: channel %s copied to %s on %s (backup %s)", settings.name, oid, port, bk.name)
                mesh.event("radio_channels", radio=oid, channels=[{"name": n, "index": i, "result": w} for n, i, w in done],
                           backup=bk.name)
                return {"ok": True, "radio": oid, "backup": bk.name, "channels": [{"name": n, "index": i, "result": w} for n, i, w in done]}
            finally:
                Mesh._close_quietly(other)

        # ---- the Stations page (pairing.py): hub mode, pairing, joining a hub, managing stations
        def _hub_info(self):
            import config as cfgmod
            try:
                saved = cfgmod.load()["sync"]
            except Exception:  # noqa: BLE001
                saved = CFG["sync"]
            port = CFG["http"]["port"]
            out = {"mode": CFG["sync"]["mode"], "savedMode": saved["mode"],
                   "pendingRestart": (saved["mode"], saved["allow"], saved["hub_url"]) !=
                                     (CFG["sync"]["mode"], CFG["sync"]["allow"], CFG["sync"]["hub_url"]),
                   "supervised": supervised_now(), "port": port, "hubName": hub_name(), "windows": sys.platform == "win32",
                   "addresses": pairing.hub_addresses(port), "allow": pairing.allow_flags(saved["allow"]),
                   "sharedToken": bool(CFG["sync"]["token"]), "pairings": pairings.list(), "stations": self._hub_stations(),
                   "hubId": hub_id, "peersOut": peering.list() if peering else [], "shareLevels": list(sync_mod.SHARE_LEVELS)}
            if isinstance(syncer, sync_mod.Collector):
                out["collector"] = {**syncer.status, "hubUrl": CFG["sync"]["hub_url"]}
            return out

        def _hub_stations(self):
            """Every station this hub knows: ones with logged data, ones that reported, ones paired."""
            metas, over = station_meta(), station_overrides(store)
            hubst = syncer.status()["stations"] if isinstance(syncer, sync_mod.Hub) else {}
            plist = pairings.list()
            ids = {r["id"] for r in analytics.stations(DATA_DIR / "mesh.db")} | set(metas) | {e["station"] for e in plist if e["station"]}
            rows = []
            for sid in sorted(ids, key=lambda x: (x != mesh.local_id, x)):
                m, p = metas.get(sid) or {}, next((e for e in plist if e["station"] == sid), None)
                loc = analytics.station_location(sid)
                via = (hubst.get(sid) or {}).get("via")
                vp = pairings.by_hub(via) if via else None
                rows.append({"id": sid, "name": station_name(sid), "here": sid == mesh.local_id,
                             "via": via and {"hub": via, "label": vp["label"] if vp else "a peer hub"},
                             "pairing": p and {"id": p["id"], "label": p["label"], "revoked": p["revoked"]},
                             "sharedToken": sid != mesh.local_id and not p and not via and bool(m or hubst.get(sid)),
                             "lastContact": (hubst.get(sid) or {}).get("last") or m.get("received"),
                             "version": m.get("version"), "backlog": m.get("backlog"), "mobile": m.get("mobile"),
                             "location": list(loc) if loc else None, "override": over.get(sid) or {}})
            return rows

        def _set_override(self, sid, **kw):
            with store.lock:
                r = store.db.execute("SELECT value FROM settings WHERE key='station_overrides'").fetchone()
                allo = json.loads(r[0]) if r else {}
                o = {**allo.get(sid, {}), **kw}
                o = {k: v for k, v in o.items() if v}
                if o:
                    allo[sid] = o
                else:
                    allo.pop(sid, None)
                store.db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('station_overrides', ?)", (json.dumps(allo),))
                store.db.commit()

        def _hub_post(self, path, body):
            import config as cfgmod
            try:
                if path == "/api/hub/mode":
                    mode = body.get("mode")
                    if mode == "hub":
                        nets = pairing.allow_networks(bool(body.get("lan")), bool(body.get("tailscale")))
                        if not nets:
                            raise ValueError("allow at least one kind of network, or stations can't reach this hub")
                        cfgmod.update_section("sync", {"mode": "hub", "allow": nets})
                    elif mode == "off":
                        cfgmod.update_section("sync", {"mode": "off"})
                    else:
                        raise ValueError("mode must be hub or off")
                    log.info("stations: sync mode set to %s (restart to apply)", mode)
                    return self._json({"ok": True, **self._hub_info()})
                if path == "/api/hub/pair":
                    if CFG["sync"]["mode"] != "hub":
                        raise ValueError("turn on hub mode first (and restart Lorakeet)")
                    allow = pairing.allow_flags(CFG["sync"]["allow"])
                    urls = [a["url"] for a in pairing.hub_addresses(CFG["http"]["port"]) if allow.get(a["kind"])]
                    if not urls:
                        raise ValueError("this computer has no address in the networks this hub allows")
                    e, token = pairings.create(body.get("label"))
                    log.info("stations: pairing code made for %r (%s)", e["label"], e["id"])
                    return self._json({"ok": True, "id": e["id"], "label": e["label"], "addresses": urls,
                                       "code": pairing.make_code(urls, token, hub_name())})
                if path == "/api/hub/station":
                    act, sid, pid = body.get("action"), body.get("station"), body.get("pairing")
                    if sid and not analytics.STATION_RE.match(str(sid)):
                        raise ValueError("bad station id")
                    if act == "rename":
                        self._set_override(sid, name=str(body.get("name") or "").strip()[:60] or None)
                    elif act == "locate":
                        loc = body.get("location") or []
                        if loc and not (len(loc) == 2 and -90 <= float(loc[0]) <= 90 and -180 <= float(loc[1]) <= 180):
                            raise ValueError("latitude -90..90 and longitude -180..180, please")
                        loc = [round(float(x), 6) for x in loc]
                        self._set_override(sid, location=loc or None)
                        if loc:
                            analytics.STATION_LOCATIONS[sid] = _as_location(loc)
                        else:
                            analytics.STATION_LOCATIONS.pop(sid, None)
                            load_station_locations(store, mesh.local_id)
                    elif act == "revoke":
                        if not pairings.revoke(pid):
                            raise ValueError("no such pairing (or already revoked)")
                        log.info("stations: pairing %s revoked", pid)
                    elif act in ("pause", "resume"):  # a peer hub sending here: decline its batches for now
                        if not pairings.set_paused(pid, act == "pause"):
                            raise ValueError("no such peer (or it was revoked)")
                        log.info("peering: receiving from %s %s", pid, "paused" if act == "pause" else "resumed")
                    elif act == "forget":
                        if sid == mesh.local_id:
                            raise ValueError("that's this computer's own radio")
                        with store.lock:
                            for key in ("station_meta", "sync_hub", "station_overrides"):
                                r = store.db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
                                if r:
                                    d = json.loads(r[0])
                                    d.pop(sid, None)
                                    store.db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, json.dumps(d)))
                            store.db.commit()
                        pairings.forget(pid=pid, station=sid)
                        analytics.STATION_LOCATIONS.pop(sid, None)
                        mesh.station_ids.discard(sid)
                        log.info("stations: forgot %s (its logged data stays)", sid or pid)
                    else:
                        raise ValueError("unknown action")
                    return self._json({"ok": True, **self._hub_info()})
                if path == "/api/hub/peer-code":  # let another hub send to this one
                    if CFG["sync"]["mode"] != "hub":
                        raise ValueError("turn on hub mode first (and restart Lorakeet)")
                    allow = pairing.allow_flags(CFG["sync"]["allow"])
                    urls = [a["url"] for a in pairing.hub_addresses(CFG["http"]["port"]) if allow.get(a["kind"])]
                    if not urls:
                        raise ValueError("this computer has no address in the networks this hub allows")
                    e, token = pairings.create(body.get("label"), kind="peer")
                    log.info("peering: peer code made for %r (%s)", e["label"], e["id"])
                    return self._json({"ok": True, "id": e["id"], "label": e["label"], "addresses": urls,
                                       "code": pairing.make_code(urls, token, hub_name(), kind="peer", hub_id=hub_id)})
                if path == "/api/hub/peer-add":  # send this hub's log to another hub
                    if peering is None:
                        raise ValueError("peering isn't available on this install")
                    c = pairing.parse_code(body.get("code"))
                    if c["kind"] != "peer":
                        raise ValueError("that's a station pairing code: for peering, the other hub makes a code with "
                                         "'Let another hub send here'")
                    if c["hubId"] and c["hubId"] == hub_id:
                        raise ValueError("that code is from this hub itself")
                    share = body.get("share") or "default"
                    if share not in sync_mod.SHARE_LEVELS:
                        raise ValueError("unknown share level")
                    result, tried = pairing.test_hubs(c["hubs"], c["token"], hub_id=hub_id)
                    if not result["ok"] and not body.get("anyway"):
                        return self._json({**result, "tried": tried, "saved": False}, 409)
                    peer = peering.add(c["name"] or result.get("hubName") or "Peer hub", result["url"] if result["ok"] else c["hubs"][0],
                                       c["token"], share, bool(body.get("forward")), c["hubId"] or result.get("hubId") or "",
                                       exact=bool(body.get("exact")))
                    log.info("peering: now sending to %s (%s, share=%s, forward=%s)", peer["name"], peer["url"], share, peer["forward"])
                    return self._json({**result, "saved": True, **self._hub_info()})
                if path == "/api/hub/peer-forget":  # delete everything a peer hub sent here (and stop it sending)
                    e = next((x for x in pairings.list() if x["id"] == body.get("pairing") and x.get("kind") == "peer"), None)
                    if e is None:
                        raise ValueError("no such peer")
                    if not isinstance(syncer, sync_mod.Hub):
                        raise ValueError("this computer isn't a hub")
                    writer = f"peer:{e['hub']}" if e.get("hub") else None
                    counts = syncer.owned_by(writer) if writer else {}
                    if body.get("dryRun"):
                        return self._json({"ok": True, "stations": [{"id": st, "name": station_name(st), "rows": n}
                                                                    for st, n in counts.items()],
                                           "rows": sum(counts.values()), "revoked": bool(e["revoked"])})
                    if not e["revoked"]:
                        pairings.revoke(e["id"])
                    deleted = syncer.delete_from(writer) if writer else {}
                    for st in deleted:
                        analytics.STATION_LOCATIONS.pop(st, None)
                        mesh.station_ids.discard(st)
                    log.info("peering: deleted %d rows from %d stations sent by %r (pairing %s, revoked)",
                             sum(deleted.values()), len(deleted), e["label"], e["id"])
                    mesh.event("peer_data_deleted", pairing=e["id"], label=e["label"], stations=list(deleted),
                               rows=sum(deleted.values()))
                    return self._json({**self._hub_info(), "ok": True, "deleted": sum(deleted.values()), "deletedStations": len(deleted)})
                if path == "/api/hub/peer":
                    if peering is None:
                        raise ValueError("peering isn't available on this install")
                    act, pid = body.get("action"), body.get("id")
                    if act == "set":
                        kw = {}
                        if "share" in body:
                            if body["share"] not in sync_mod.SHARE_LEVELS:
                                raise ValueError("unknown share level")
                            kw["share"] = body["share"]
                        if "forward" in body:
                            kw["forward"] = bool(body["forward"])
                        if "exact" in body:
                            kw["exact"] = bool(body["exact"])
                        if "paused" in body:
                            kw["paused"] = bool(body["paused"])
                            log.info("peering: sending to %s %s", pid, "paused" if kw["paused"] else "resumed")
                        if not peering.update(pid, **kw):
                            raise ValueError("no such peer")
                    elif act == "remove":
                        peering.remove(pid)
                        log.info("peering: stopped sending to %s", pid)
                    elif act == "sync":
                        if pid in peering.senders:
                            peering.senders[pid].sync_now()
                    elif act == "test":
                        peer = peering.peers.get(pid)
                        if not peer:
                            raise ValueError("no such peer")
                        result, tried = pairing.test_hubs([peer["url"]], peer["token"], hub_id=hub_id)
                        return self._json({**result, "tried": tried})
                    else:
                        raise ValueError("unknown action")
                    return self._json({"ok": True, **self._hub_info()})
                if path == "/api/hub/test":
                    if body.get("code"):
                        c = pairing.parse_code(body["code"])
                        urls, token = c["hubs"], c["token"]
                    else:
                        if not CFG["sync"]["hub_url"]:
                            raise ValueError("this station isn't set up to send to a hub")
                        urls, token = [CFG["sync"]["hub_url"]], CFG["sync"]["token"]
                    result, tried = pairing.test_hubs(urls, token, mesh.local_id)
                    return self._json({**result, "tried": tried})
                if path == "/api/hub/join":
                    c = pairing.parse_code(body.get("code"))
                    if c["kind"] == "peer":
                        raise ValueError("that's a peer code (for another hub to share with it): use Peers → Send to another hub")
                    result, tried = pairing.test_hubs(c["hubs"], c["token"], mesh.local_id)
                    if not result["ok"] and not body.get("anyway"):
                        return self._json({**result, "tried": tried, "saved": False}, 409)
                    url = result["url"] if result["ok"] else c["hubs"][0]
                    cfgmod.update_section("sync", {"mode": "collector", "hub_url": url, "token": c["token"]})
                    log.info("stations: joined hub %s (restart to apply)", url)
                    return self._json({**result, "saved": True, "hubUrl": url, **self._hub_info()})
                if path == "/api/hub/leave":
                    cfgmod.update_section("sync", {"mode": "off"})
                    log.info("stations: stopped sending to the hub (restart to apply)")
                    return self._json({"ok": True, **self._hub_info()})
                return self._json({"error": "not found"}, 404)
            except ValueError as e:
                return self._json({"error": str(e)}, 400)

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


def _preset_name(node):
    from meshtastic.protobuf import config_pb2
    lora = node.localConfig.lora
    return PRESET_NAMES.get(config_pb2.Config.LoRaConfig.ModemPreset.Name(lora.modem_preset), "Primary") if lora.use_preset else "Primary"


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


def set_password_interactive(path):
    """--set-password: asks twice in the terminal (never on the command line, so it stays out of shell history)."""
    import getpass
    lg = login_mod.Login(path)
    print("Set the password other devices on your network log in with (at least "
          f"{login_mod.MIN_LENGTH} characters)." + (" This replaces the old one and logs every device out." if lg.enabled else ""))
    while True:
        a = getpass.getpass("New password: ")
        if a != getpass.getpass("Again: "):
            print("They didn't match. Try again.")
            continue
        try:
            lg.set_password(a)
        except ValueError as e:
            print(f"Not set: {e}.")
            continue
        break
    print(f"Saved in {path}, as a salted hash (the password itself is never stored).")
    if CFG["http"]["lan"] == "off":
        print("Other devices can't reach the dashboard yet: set [http] lan = \"view\" or \"login\" in lorakeet.toml, then restart.")
    else:
        print("Restart Lorakeet to use it.")


INGEST_SLOTS = threading.BoundedSemaphore(4)
DRAIN_MAX = 16 * 1024 * 1024  # a refused station's batch is read and dropped up to this size (Handler._drain)
MAX_CONNECTIONS = 64  # concurrent requests from other machines; live pages hold one each (the event stream)
MAX_PER_ADDRESS = 8   # ... and from any one of them, so one device can't take every slot
MAX_LOCAL = 64        # this computer has its own allowance: other machines can never lock it out


class ExclusiveHTTPServer(ThreadingHTTPServer):
    # HTTPServer sets SO_REUSEADDR, which on Windows lets a second process bind the same port
    # and silently share it. Exclusive binding makes a second instance fail fast instead.
    allow_reuse_address = False

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
    cmd = [sys.executable, str(HERE / "server.py"), "--demo", "--http", str(p)]
    # not supervised, whatever this process is: the demo's Restart must say it can't, not exit for good
    env = {k: v for k, v in os.environ.items() if k not in ("LORAKEET_SUPERVISED", "INVOCATION_ID")}
    if sys.platform == "win32":
        pyw = Path(sys.executable).with_name("pythonw.exe")
        cmd[0] = str(pyw if pyw.exists() else sys.executable)
        subprocess.Popen(cmd, cwd=HERE, env=env, close_fds=True, creationflags=subprocess.DETACHED_PROCESS
                         | subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW)
    else:
        subprocess.Popen(cmd, cwd=HERE, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    end = time.time() + wait_s
    while time.time() < end:
        if probe(p):
            return {"url": f"http://127.0.0.1:{p}/"}
        time.sleep(0.5)
    return {"error": f"the demo didn't start (see {DATA_DIR / 'demo' / 'server.log'})"}


def setup_demo():
    """--demo: a made-up mesh around Portland in <data>/demo, rebuilt when stale; nothing personal, no radio."""
    global DATA_DIR, DEMO, BASE_ID, BASE_NAME, BASE_BYTE
    import demo
    DATA_DIR = DATA_DIR / "demo"
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    db = DATA_DIR / "mesh.db"
    if demo.stale(db):
        demo.build(db)
    DEMO = demo.home_of(db)
    BASE_ID = BASE_NAME = BASE_BYTE = None
    CFG["map"].update(center=list(demo.PORTLAND), zoom=11)
    CFG["base"].update(id="", name="")
    CFG["station"].update(name="Demo Home", location=[], mobile=False, drive_pings=False)
    CFG["http"]["lan"] = "off"
    CFG["sync"]["mode"] = "off"
    CFG["remote"]["enabled"] = False
    CFG["backup"]["destinations"] = []

    def idle_exit():
        # an open tab keeps polling, so idleness alone could keep a demo running for days while "the last 24 hours"
        # empties out: past demo.stale's age it exits too, and the next "Explore a demo" builds a fresh one
        while time.time() - LAST_REQUEST[0] < DEMO_IDLE_S and not demo.stale(db, 18 * 3600):
            time.sleep(60)
        log.info("demo: idle or out of date, exiting")
        os._exit(0)
    threading.Thread(target=idle_exit, daemon=True, name="demo-idle").start()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default=CFG["radio"]["port"] or None,
                    help="serial port (default: lorakeet.toml radio.port, else auto-detect)")
    ap.add_argument("--host", default=CFG["radio"]["host"] or None,
                    help="a radio on the network (IP or name) instead of USB (default: lorakeet.toml radio.host)")
    ap.add_argument("--http", type=int, default=None, help="dashboard port (default: lorakeet.toml http.port; "
                                                                "with --demo, one above it)")
    ap.add_argument("--demo", action="store_true", help="explore a made-up mesh: no radio, its own data folder "
                                                         "(<data>/demo) and port")
    ap.add_argument("--version", action="version", version=f"Lorakeet {VERSION}")
    ap.add_argument("--set-password", action="store_true",
                    help="set the password other devices on your network log in with, then exit")
    ap.add_argument("--clear-password", action="store_true", help="remove the password (and every login), then exit")
    ap.add_argument("--join", metavar="CODE", help="send what this station logs to a hub: the pairing code from the hub's "
                                                   "Stations page (or - to type or pipe it in), then restart Lorakeet")
    ap.add_argument("--force", action="store_true", help="with --join: save even if the hub can't be reached right now")
    args = ap.parse_args()
    if args.join:
        import config as cfgmod
        if args.join == "-":  # read it, so the code stays out of shell history and the process list
            if sys.stdin.isatty():
                print("Paste the pairing code from the hub's Stations page, then press Enter:")
            args.join = sys.stdin.readline().strip()
        try:
            c = pairing.parse_code(args.join)
        except ValueError as e:
            sys.exit(f"Not joined: {e}.")
        print(f"Testing the hub{' ' + c['name'] if c['name'] else ''} at {', '.join(c['hubs'])} ...")
        result, _ = pairing.test_hubs(c["hubs"], c["token"])
        if result["ok"]:
            print(f"Reached {result.get('hubName') or 'the hub'} at {result['url']}.")
        else:
            print(f"Couldn't reach the hub: {result['error']}\n{result['hint']}")
            if not args.force:
                sys.exit("Not joined. Fix that and try again, or add --force to save anyway.")
        try:
            path = cfgmod.update_section("sync", {"mode": "collector", "hub_url": result["url"] if result["ok"] else c["hubs"][0],
                                                  "token": c["token"]})
        except ValueError as e:
            sys.exit(f"Not joined: {e}")
        print(f"Saved in {path}. Restart Lorakeet: it then sends what it logs to the hub, and catches up after any outage.")
        return
    if args.set_password or args.clear_password:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        if args.set_password:
            set_password_interactive(DATA_DIR / "login.json")
        else:
            login_mod.Login(DATA_DIR / "login.json").clear_password()
            print("Password removed; other devices are logged out.")
        return
    if args.host and args.port:
        ap.error("use --port (USB) or --host (network), not both")
    if args.http is None:
        args.http = CFG["http"]["port"] + (1 if args.demo else 0)
    if args.demo:
        setup_demo()

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    handlers = [logging.handlers.RotatingFileHandler(DATA_DIR / "server.log", maxBytes=2_000_000,
                                                     backupCount=3, encoding="utf-8")]
    if sys.stdout is not None:  # None under pythonw
        handlers.append(logging.StreamHandler(sys.stdout))
    logging.basicConfig(level=logging.INFO, handlers=handlers,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    logging.getLogger("meshtastic").setLevel(logging.WARNING)

    store = Store(DATA_DIR / "mesh.db")
    if DEMO:
        store.claim_station(DEMO)
    scrub_logged_secrets(store)
    PAIRINGS = pairing.Pairings(DATA_DIR / "stations.json")
    debug_log = DebugLog(DATA_DIR / "debug.db")
    follow_base()  # [base] id: the base station's current number if firmware 2.8 renumbered it
    mesh = Mesh(store, args.port, debug_log, host=args.host, tcp_port=CFG["radio"]["tcp_port"])
    if DEMO:
        mesh.local_id = DEMO
    debug_log.on_batch = lambda rows: mesh.broadcast("debug", rows)
    # Bind before touching the radio: a second instance must exit here, not fight over the COM port.
    try:
        storage = Storage(DATA_DIR / "mesh.db", DATA_DIR / "debug.db", DATA_DIR,
                          dests=CFG["backup"]["destinations"],
                          on_change=lambda st: mesh.broadcast("storage", st), on_event=mesh.event)
        alert_engine = alerts_mod.Alerts(store, mesh, DATA_DIR / "mesh.db", BASE_ID)
        # http.lan = "view" / "login": all interfaces; this PC full access, other private-LAN devices read-only
        # or nothing until they log in (login.access); "off": this computer only.
        bind = "0.0.0.0" if CFG["http"]["lan"] in ("view", "login") else "127.0.0.1"
        syncer = None
        if CFG["sync"]["mode"] == "hub":
            def on_report(station, rep):
                if (station_overrides(store).get(station) or {}).get("location"):
                    pass  # the hub's own placement wins over what the station reports
                elif rep.get("location"):
                    loc = rep["location"]
                    analytics.STATION_LOCATIONS[station] = tuple(loc) + ((None,) if len(loc) == 2 else ())
                else:
                    analytics.STATION_LOCATIONS.pop(station, None)
                mesh.station_ids.add(station)
            syncer = sync_mod.Hub(store, on_rows=mesh.ingested, on_meta=on_report, is_revoked=PAIRINGS.is_revoked)
            # collectors arrive over Tailscale, so the hub must listen beyond this PC even with lan = "off";
            # client_access() still gives non-LAN addresses no dashboard access, only /api/ingest
            bind = "0.0.0.0"
        elif CFG["sync"]["mode"] == "collector":
            syncer = sync_mod.Collector(store, CFG["sync"]["hub_url"], CFG["sync"]["token"], CFG["sync"]["interval_s"],
                                        report=lambda: station_report(mesh))
        login = login_mod.Login(DATA_DIR / "login.json")
        peering = PeerManager(store, mesh, pairing.install_id(DATA_DIR))
        srv = ExclusiveHTTPServer((bind, args.http), make_handler(mesh, store, storage, alert_engine, syncer, login, peering,
                                                                  PAIRINGS))
    except OSError as e:
        log.error("port %d unavailable (%s); another instance is probably running", args.http, e)
        sys.exit(EXIT_PORT_IN_USE)
    srv.daemon_threads = True
    if CFG["remote"]["enabled"]:
        mesh.remote = remote_mod.Remote(
            mesh, CFG["remote"]["allow"], pinned_key=lambda n: first_public_key(store, n), key_flag=key_flag,
            status=lambda: remote_mod.status_text(
                CFG["station"]["name"] or mesh.name(mesh.local_id), _uptime_s(), remote_mod.network(),
                # the backlog counted now: the sync status keeps the last SUCCESSFUL pass's (0 while offline)
                {**syncer.status, "backlog": syncer.backlog()} if isinstance(syncer, sync_mod.Collector) else None,
                _pi_health(), mesh.own_fix_now()),
            fix=mesh.own_fix_now, aliases=lambda: nodeids.info(DATA_DIR / "mesh.db")["aliases"])
        log.info("remote commands: on, from %s", ", ".join(CFG["remote"]["allow"]))
    if DEMO:  # nothing to connect to, back up, alert on or share
        log.info("Lorakeet %s demo (a made-up mesh) on http://127.0.0.1:%d", VERSION, args.http)
        srv.serve_forever()
        return
    threading.Thread(target=mesh.run, daemon=True, name="radio").start()
    if sys.platform.startswith("linux"):
        HealthWatch(mesh.event).start()
    storage.start()
    alert_engine.start()
    if isinstance(syncer, sync_mod.Collector):
        syncer.start()
    elif CFG["sync"]["mode"] != "collector":
        peering.start_all()  # a station sends to its hub; peering is between hubs (or single installs)
    log.info("sync: %s", CFG["sync"]["mode"])
    log.info("config: %s", CFG["_path"] or "none (defaults)")
    lan = CFG["http"]["lan"]
    log.info("Lorakeet %s, dashboard on http://127.0.0.1:%d%s", VERSION, args.http,
             " (this computer only)" if lan == "off" else
             f" (full) and on the LAN ({'view only' if lan == 'view' else 'log in to see it'}"
             f"{', full access after logging in' if login.enabled else ''})")
    if lan == "login" and not login.enabled:
        log.warning('http.lan = "login" but no password is set, so no other device can get in: '
                    "run  python server.py --set-password")
    srv.serve_forever()


if __name__ == "__main__":
    main()
