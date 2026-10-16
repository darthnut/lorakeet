"""The link to the radio (USB serial or TCP): connecting and reconnecting, logging every packet and
reception, live state for the pages, sending text and traceroutes.
"""
import json
import logging
import os
import re
import threading
import time

from pubsub import pub
from serial.tools import list_ports

import analytics
from channels import radio_channels, radio_lora
from config import CFG
from fwlog import ANSI, parse_rx, parse_tx, strip_text_prefix
import keyflags
import nodeids
import radioid
import runtime
from station import load_station_locations, set_clock_from_gps
from store import DebugLog, dumps, node_id, redacted_channels, redacted_config, redacted_module_config
import topology
from link import RadioLink


log = logging.getLogger("meshdash")


def key_flag(nid):
    """A radio's compromised / shared public key flag (keyflags.py), or None."""
    return keyflags.flags(runtime.DATA_DIR / "mesh.db").get(nid)


def ping_due(last_pos, last_ts, pos, now, every_m, min_s):
    """A drive ping is due once we've moved every_m metres from the last one and min_s has passed."""
    return now - last_ts >= min_s and analytics.dist_m(*last_pos, *pos) >= every_m


def messages_for_this_radio(store, local, limit=200):
    """The Messages tab: every message once (this radio's copy first), with channel numbers as THIS radio numbers
    them. Channel indexes differ between radios (the Pi's 2 is the desk's 1, both "Lorakeet"), so another
    station's copy is renumbered by channel name; one on a channel this radio doesn't have gets channel None
    (shown in no channel view). Phone-log imports (no packet id, no channel recorded) are left out: they can't be
    matched with the stations' copies and only ever showed up as unlabelled duplicates. MeshCore messages carry no packet
    id either, but they're real: a MeshCore station's copies stay."""
    rows = store.query(
        "SELECT * FROM (SELECT *, ROW_NUMBER() OVER (PARTITION BY CASE WHEN pkt_id IS NULL THEN rowid "
        "ELSE from_id || ':' || pkt_id END ORDER BY (station IS ?) DESC, outgoing DESC, ts) AS _copy "
        "FROM messages WHERE NOT (pkt_id IS NULL AND station IS NOT ? AND station NOT LIKE 'mc:%')) WHERE _copy = 1 "
        "ORDER BY ts DESC LIMIT ?", local, local, limit)
    for m in rows:
        st = m.get("station")
        if st and st != local and radioid.is_meshcore(st) and m.get("to_id") == "^all":
            # a MeshCore channel: another mesh, never one of this radio's channels
            m["channelName"], m["channel"] = f"MeshCore channel {m.get('channel') or 0}", None
    with store.lock:
        names = topology._channel_names(store.db)
    if not local or local not in names:
        return rows  # radio not connected yet (we don't know whose numbering to use): leave channels as logged
    mine = {name: idx for idx, name in names.get(local, {}).items()}
    for m in rows:
        st = m.get("station")
        if st and st != local and not radioid.is_meshcore(st) and m.get("to_id") in ("^all", "!ffffffff"):
            name = names.get(st, {}).get(m.get("channel") or 0)
            m["channelName"] = name
            m["channel"] = mine.get(name) if name else None
    return rows


def short_label(nid, long_name=None):
    """A radio's short label when it gives none: a MeshCore radio has no short name, so the first word of its name
    (up to 8 characters); otherwise the id's last 4 digits, as before."""
    if radioid.is_meshcore(nid) and long_name and long_name != nid:
        return long_name.split()[0][:8]
    return nid[-4:]


def first_public_key(store, nid):
    """The first public key this database recorded for a radio: what remote commands are checked against."""
    r = store.query("SELECT public_key FROM node_info WHERE node = ? AND public_key IS NOT NULL AND public_key != '' "
                    "ORDER BY ts LIMIT 1", nid)
    return r[0]["public_key"] if r else None


def likely_28(nid):
    """Numbered the firmware 2.8 way (crc32 of its key; nodeids.py)."""
    return nid in nodeids.info(runtime.DATA_DIR / "mesh.db")["v28"]
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
WATCHDOG_S = 10 * 60       # the radio reports its own telemetry every ~60 s; silence this long means it's wedged


def keep_recipient(frm, to, local):
    """lorakeet.toml logging.store_recipients = false drops recipients of packets overheard between others."""
    return CFG["logging"]["store_recipients"] or to in (None, "^all", "!ffffffff") or local in (frm, to)


def arrival_of(packet):
    """How a packet reached the dashboard, from fields the radio reported (mirrors packetsearch.ARRIVAL_SQL)."""
    tm = packet.get("transportMechanism")
    if "meshcore" in packet:
        return "lora"   # a MeshCore station logs only what its radio heard on the air
    if isinstance(tm, str) and tm.startswith("TRANSPORT_LORA"):
        return "lora"
    if tm in ("TRANSPORT_MQTT", "TRANSPORT_MULTICAST_UDP", "TRANSPORT_API"):
        return {"TRANSPORT_MQTT": "mqtt", "TRANSPORT_MULTICAST_UDP": "udp", "TRANSPORT_API": "api"}[tm]
    if packet.get("rxSnr") is None and packet.get("rxRssi") is None:
        return "local"
    return "unknown"
BROADCAST = "^all"
MAX_TEXT_BYTES = 200
TRACEROUTE_COOLDOWN_S = 30  # firmware rate-limits traceroutes too; be a good neighbour on a shared channel
UNKNOWN_HOP = 0xFFFFFFFF
UNK_SNR = -128


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


# USB vendors of Meshtastic radios: Espressif (ESP32 boards like the Heltec V4), Adafruit's ID, used by most nRF52
# boards' firmware (RAK4631, T-Echo, SenseCAP T1000-E: 239a:8029), and Seeed Studio's own. The meshtastic library
# whitelists the first two. Which one we log from: pick_usb_port.
RADIO_VIDS = (ESPRESSIF_VID, 0x239A, 0x2886)


RADIO_USB_KEY = "radio_usb"   # settings: the USB serial number of the radio we log from, to find it on any port
_MAC = re.compile(r"^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$")


def usb_node_id(serial):
    """The Meshtastic id a radio will have, from its USB serial number where that's its MAC (ESP32-S3 boards with
    native USB, like the Heltec V4: the id is the MAC's last four bytes). None when it can't be told."""
    if not serial or not _MAC.match(serial):
        return None
    return "!" + "".join(serial.split(":")[2:]).lower()


def serial_ports():
    """USB serial ports for the setup page, likely radios first."""
    out = [{"device": p.device, "description": p.description or "", "radio": p.vid in RADIO_VIDS,
            "vid": f"{p.vid:04x}" if p.vid else None, "serial": p.serial_number,
            "nodeId": usb_node_id(p.serial_number) if p.vid in RADIO_VIDS else None} for p in list_ports.comports()]
    return sorted(out, key=lambda x: (not x["radio"], x["device"]))


def radio_ports():
    """[(device, USB serial number)] for the ports that look like radios."""
    return [(p.device, p.serial_number) for p in sorted(list_ports.comports(), key=lambda p: p.device)
            if p.vid in RADIO_VIDS]


def resolve_port(hint):
    """A configured port, as the device it is now: a plain name (COM7, /dev/ttyACM0) or a stable link to one
    (/dev/serial/by-id/..., which names the radio itself and survives replugging into another port)."""
    devices = {p.device for p in list_ports.comports()}
    if hint in devices:
        return hint
    real = os.path.realpath(hint)
    return real if real in devices else None


def pick_usb_port(remembered, ports, claimed=()):
    """Which radio port to log from: (device, None), or (None, "several") when there's more than one to choose
    from and none is the remembered radio. The remembered radio is found on whatever port it's in. Without it, a
    single radio is taken (a radio swapped for another just works), but several are never guessed between: two
    identical radios get their port names in either order at boot. `claimed`: ports another link uses."""
    free = [(d, s) for d, s in ports if d not in claimed]
    if remembered:
        hit = next((d for d, s in free if s == remembered), None)
        if hit:
            return hit, None
    if len(free) == 1:
        return free[0][0], None
    return None, ("several" if free else None)


class Mesh(RadioLink):
    """The Meshtastic link (USB serial or TCP), through the official meshtastic library."""
    PROTOCOL = "meshtastic"
    WATCHDOG_S = WATCHDOG_S

    def __init__(self, store, port_hint, debug_log=None, host=None, tcp_port=4403):
        """port_hint: a serial port (else auto-detect). host: a radio on the network instead (TCP API)."""
        super().__init__(store)
        self.debug_log = debug_log
        self.port_hint = port_hint
        self.host, self.tcp_port = host or None, tcp_port
        self.iface = None
        self._connecting = None    # the interface while it connects (boot lines arrive before it's self.iface)
        self.extras = []           # [[radios]]: the other radios this Lorakeet logs (server.py); this is the main one
        self.claimed_ports = set()   # their ports: auto-detect leaves them alone
        self.station_name = ""     # an extra radio's [[radios]] name (the main radio's is [station] name)
        self.needs_choice = False  # several radios on USB and none of them the remembered one
        self.rx = {}  # node id -> {"rssi","snr","ts","count"} as heard by our radio
        # firmware debug-log records received / RX-TX header lines mined from them (alerts.py watches the
        # pair: lines still arriving but nothing mined means the firmware's log format changed)
        self.log_counts = {"lines": 0, "mined": 0}
        self.remote = None  # remote.Remote when [remote] is enabled: "lk ..." commands by direct message
        self.rx_remote = {}  # node id -> newest reception at ANOTHER listening station (sync hub): {station, ts, snr, rssi, hops}
        self.station_ids = set()  # every listening station seen (ours + collectors)
        r = store.query("SELECT MAX(ts) AS ts FROM packets WHERE relay=? AND hops>0", runtime.BASE_BYTE) if runtime.BASE_ID else None
        self.base_relay_ts = r[0]["ts"] if r else None
        # (a MeshCore packet usually names no sender: from_id is empty, and isn't a radio to list)
        # ...and only from this link's own kind of station: a MeshCore radio's receptions aren't "heard here" on a
        # Meshtastic radio's map (several radios, one Lorakeet), nor the other way round
        mine = "station LIKE 'mc:%'" if self.PROTOCOL == "meshcore" else "(station IS NULL OR station NOT LIKE 'mc:%')"
        for r in store.query("SELECT from_id, MAX(ts) AS ts, snr, rssi, hops FROM packets WHERE from_id IS NOT NULL "
                             f"AND {mine} GROUP BY from_id"):
            self.rx[r["from_id"]] = {"ts": r["ts"], "snr": r["snr"], "rssi": r["rssi"], "hops": r["hops"], "count": 0}
        # latest position per node from our own log; the library's node DB doesn't always keep them
        self.pos = {r["node"]: r for r in store.query(
            "SELECT node, MAX(ts) AS ts, lat, lon, alt, precision_bits FROM positions GROUP BY node")}
        self.last_traceroute = 0
        self._own_fix = None  # our radio's last logged GPS fix (_track_self); None = not looked up yet
        self._own_refresh = 0  # last node-list request for our exact position (_refresh_own_fix)
        self._ping = {"n": 0, "pos": None, "ts": 0}  # drive pings: count, where and when the last one went
        self._clock_checked = False
        self._last_snapshot = self._last_follow = 0  # tick(): node DB snapshot, follow the base
        self.known = {r["node"]: r for r in store.query(
            "SELECT node, MAX(ts), long_name, short_name, hw_model, role FROM node_info GROUP BY node")}
        self.identity = {r["node"]: r["sig"] for r in store.query(
            "SELECT node, MAX(ts), IFNULL(long_name,'')||'|'||IFNULL(short_name,'')||'|'||IFNULL(hw_model,'')||'|'"
            "||IFNULL(role,'')||'|'||IFNULL(public_key,'') AS sig FROM node_info GROUP BY node")}
        self.node_cache = {}      # last known node DB, served while the radio is unplugged
        pub.subscribe(self._on_receive, "meshtastic.receive")
        pub.subscribe(self._on_lost, "meshtastic.connection.lost")
        pub.subscribe(self._on_node, "meshtastic.node.updated")
        pub.subscribe(self._on_log_line, "meshtastic.log.line")

    # -- the RadioLink hooks (link.RadioLink runs the reconnect loop and the watchdog)
    def _interface_class(self):
        if self.host:
            from meshtastic.tcp_interface import TCPInterface

            def SerialInterface(_label, timeout, connectNow):  # noqa: N802 - same shape for _connect
                # connectNow=False leaves the socket closed; _connect's connect() opens it (TCPInterface.connect
                # calls myConnect itself: opening it here as well made a second connection, and a radio serves
                # one client at a time, so it kept dropping us)
                return TCPInterface(self.host, portNumber=self.tcp_port, timeout=timeout, connectNow=False)
            return SerialInterface
        from meshtastic.serial_interface import SerialInterface
        return SerialInterface

    def connect(self):
        self._connect(self._interface_class())

    def drop_link(self):
        if self.iface is not None:
            self._drop(self.iface)

    def tick(self):
        if self.connected:
            if CFG["station"]["mobile"] and time.time() - self._own_refresh > OWN_FIX_REFRESH_S:
                self._refresh_own_fix()
            self._track_self()
            if CFG["station"]["drive_pings"]:
                self._drive_ping()
        if time.time() - self._last_snapshot > 86400:
            self._snapshot_nodedb()
            self._last_snapshot = time.time()
        if time.time() - self._last_follow > 300:
            runtime.follow_base()
            self._last_follow = time.time()

    def remembered_radio(self):
        r = self.store.query("SELECT value FROM settings WHERE key = ?", RADIO_USB_KEY)
        return r[0]["value"] if r else None

    def remember_radio(self, serial):
        self.store.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", RADIO_USB_KEY, serial)

    def _pick_port(self):
        self.needs_choice = False
        if self.host:  # a network radio: the "port" is its address, shown wherever the COM port would be
            return f"{self.host}:{self.tcp_port}"
        if self.port_hint:  # a configured port is used strictly: with several radios on USB, never pick another
            return resolve_port(self.port_hint)
        claimed = {resolve_port(p) or p for p in self.claimed_ports}
        port, problem = pick_usb_port(self.remembered_radio(), radio_ports(), claimed)
        if problem:
            self.needs_choice = True
            err = "several radios are plugged in: choose the one to log from on the Radio page"
            if err != self.last_connect_error:
                log.warning(err)
                self.event("connect_failed", port=None, error=err)
            self.last_connect_error = err
        return port

    def _connect(self, SerialInterface):  # noqa: N803
        port = self._pick_port()
        if not port:
            return
        log.info("connecting to %s", port)
        iface = None
        try:
            iface = SerialInterface(port, timeout=CONNECT_TIMEOUT_S, connectNow=False)
            self._connecting = iface
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
                self.last_rx = time.monotonic()
                self.connected = True
                self.connected_at = time.time()
            log.info("connected: %s on %s, %d nodes", self.local_id, port, len(self.node_cache))
            serial = dict(radio_ports()).get(port) if not self.host else None
            if serial and serial != self.remembered_radio():
                self.remember_radio(serial)  # found on any port from now on
                log.info("remembering radio %s (USB serial %s)", self.local_id, serial)
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
        if interface is not self.iface and interface is not self._connecting:
            return  # another radio's (several radios, one Lorakeet)
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
                                  "WHERE station IS NOT NULL AND station != ? AND from_id IS NOT NULL GROUP BY from_id, station",
                                  self.local_id):
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
                if frm:   # (a MeshCore packet often names no sender: nothing to place, still a packet for the feed)
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
                "protocol": self.PROTOCOL, "radios": [x.status() for x in self.extras], "baseId": runtime.BASE_ID, "baseName": runtime.BASE_NAME, "stations": self.station_names(), "demo": bool(runtime.DEMO)}

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
        if interface is not self.iface:
            return  # another radio in this Lorakeet ([[radios]]): the library's events go to every listener
        nid = node.get("user", {}).get("id") or node_id(node["num"])
        self.broadcast("node", self.node_json(nid))

    def _on_receive(self, packet, interface, **_):
        if interface is not self.iface:
            return  # straggler from a connection we've already dropped
        self.last_rx = time.monotonic()
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
        if runtime.BASE_ID and relay and hops and relay == runtime.BASE_BYTE:
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
            self.broadcast("node", self.node_json(runtime.BASE_ID))

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
        if dest != BROADCAST and not radioid.is_meshtastic(dest):
            # the library would take the id's last 8 hex digits as a node number: a DM to some other radio, or none
            raise ValueError("a Meshtastic radio can only message a Meshtastic radio (!xxxxxxxx)")
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
        if not radioid.is_meshtastic(target):
            # the library would turn the id's last 8 hex digits into a node number and send to a radio that isn't there
            raise ValueError("only a Meshtastic radio (!xxxxxxxx) can be traced from a Meshtastic radio")
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
        if not nid:   # a MeshCore channel message names its sender only in its text ("Name: message")
            return {"name": "(sender not named)", "short": "?", "hw": None, "role": None, "announced": False,
                    "isBase": False, "isLocal": False, "keyFlag": None, "v28": False}
        n = (self._nodes().get(nid) or {}).get("user") or {}
        k = self.known.get(nid) or {}
        name = n.get("longName") or k.get("long_name") or (runtime.BASE_NAME if nid == runtime.BASE_ID else nid)
        announced = bool(n.get("longName") or k)
        # The library omits role when it's the default CLIENT, so CLIENT is only implied for nodes that
        # actually announced themselves; nodes we've never heard a NodeInfo from have an unknown role.
        role = n.get("role") or k.get("role") or ("CLIENT" if announced else None)
        return {"name": name, "short": n.get("shortName") or k.get("short_name") or short_label(nid, name),
                "hw": n.get("hwModel") or k.get("hw_model"), "role": role, "announced": announced,
                "isBase": nid == runtime.BASE_ID, "isLocal": nid == self.local_id, "keyFlag": key_flag(nid),
                "v28": likely_28(nid)}

    def name(self, nid):
        n = self._nodes().get(nid)
        return ((n or {}).get("user", {}).get("longName") or (self.known.get(nid) or {}).get("long_name")
                or (runtime.BASE_NAME if nid == runtime.BASE_ID else nid))

    def node_json(self, nid):
        n = self._nodes().get(nid)
        if not n:
            if runtime.BASE_ID and nid == runtime.BASE_ID:
                # not in the radio's node DB yet: placeholder so the base card is always there
                n = {"num": int(runtime.BASE_ID[1:], 16), "user": {"longName": runtime.BASE_NAME, "shortName": runtime.BASE_NAME}}
            elif nid in self.pos or nid in self.rx or nid in self.rx_remote:
                n = {"num": radioid.node_num(nid), "user": {}}   # a MeshCore radio has no node number
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
            "id": nid, "num": n.get("num"), "protocol": radioid.protocol_of(nid),
            "longName": u.get("longName") or nid, "shortName": u.get("shortName") or short_label(nid, u.get("longName")),
            "hwModel": u.get("hwModel"), "role": u.get("role", None if radioid.is_meshcore(nid) else "CLIENT"),
            "lat": p.get("latitude"), "lon": p.get("longitude"), "alt": p.get("altitude"),
            "precisionBits": p.get("precisionBits"), "positionTime": p.get("time"),
            # nodes without a set clock report 1970-era times; treat those as unknown
            "lastHeard": max((n.get("lastHeard") or 0) if (n.get("lastHeard") or 0) > 1.6e9 else 0, rx.get("ts") or 0,
                             (self.base_relay_ts or 0) if nid == runtime.BASE_ID else 0, (rr or {}).get("ts") or 0) or None,
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
            "isLocal": nid == self.local_id, "isBase": nid == runtime.BASE_ID,
            "relayOnly": nid == runtime.BASE_ID and rx.get("ts") is None and n.get("lastHeard") is None,
            "v28": likely_28(nid),
            "keyFlag": dict(kf, withNames=[self.name(o) for o in kf["with"]]) if (kf := key_flag(nid)) else None,
        }

    def state(self):
        nodes = []
        for nid in set(self._nodes()) | set(self.pos) | set(self.rx) | set(self.rx_remote) | ({runtime.BASE_ID} if runtime.BASE_ID else set()):
            j = self.node_json(nid) if nid else None
            if j:
                nodes.append(j)
        return {"status": self.status(), "nodes": nodes, "now": time.time()}
