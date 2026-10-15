"""The MeshCore link (docs/MESHCORE.md, step 4): a MeshCore companion radio on USB, through the `meshcore` library.

The library is asynchronous and Lorakeet uses threads, so it runs on its own event loop in one thread; RadioLink's
loop calls connect() / drop_link() / tick() from its thread and they hand the work to that loop. Listening only:
what goes to the radio (start, its own info, its counters, fetching messages it holds) travels over USB, never the
air.

What's logged, from the library's events:
  RX_LOG_DATA       every reception -> mc_rx (the whole packet as hex, parsed by meshcore_packet), and the first
                    hearing of each packet (by its packet hash) -> packets, so the pages and the feed see it
  an ADVERT in it   -> node_info when a radio's name, type or key is new or changed, positions when it has one
  CONTACT_MSG_RECV  a direct message to this radio -> messages
  CHANNEL_MSG_RECV  a channel message this radio could read -> messages
  STATS_*, BATTERY  the radio's own counters -> telemetry_full
The handlers are plain methods taking the event's payload, so tests drive them with frames played through the
library's reader (attach()) without a radio.

It's a mesh.Mesh with the radio connection swapped: the pages, alerts and station reports call the Meshtastic link's
methods, and Mesh already loads radios' names, positions and last receptions from the database (node_info,
positions, packets), which this link fills and keeps current as it logs. What only Meshtastic can do (sending,
traceroutes, the Radio page's settings) says it isn't available on a MeshCore station.
"""
import asyncio
import collections
import json
import logging
import threading
import time

import meshcore_packet
import radioid
from mesh import Mesh

log = logging.getLogger("meshdash")

STATS_EVERY_S = 300      # ask the radio for its counters this often (they double as the watchdog's heartbeat)
SEEN_WINDOW_S = 1800     # a packet hash heard again within this is a repeat of the same transmission
CONNECT_TIMEOUT_S = 30
RESET_AFTER_FAILS = 3    # an ESP32 radio silent this many tries in a row is restarted (stuck in its USB bootloader)
RESET_EVERY_S = 600      # ...at most this often
TEXT_MAX = 1000


class MeshCoreLink(Mesh):
    PROTOCOL = "meshcore"
    WATCHDOG_S = 3 * STATS_EVERY_S   # silent for three rounds of counters: wedged

    def __init__(self, store, port_hint, debug_log=None):
        super().__init__(store, port_hint, debug_log)
        self.mc = None
        self.self_info = None
        self.loop = None
        self._seen = collections.OrderedDict()   # packet hash -> when first heard (first hearings go to packets)
        self._identity = {}                       # radio id -> (name, type, key): node_info only on a change
        self._last_stats = 0
        self._no_answer = 0                       # connects in a row with no answer from the radio
        self._last_reset = 0
        self._early = []                          # events that arrived before the radio said who it is (SELF_INFO)

    # -- the event loop thread
    def _ensure_loop(self):
        if self.loop is None:
            self.loop = asyncio.new_event_loop()
            threading.Thread(target=self.loop.run_forever, daemon=True, name="meshcore-loop").start()

    def _run(self, coro, timeout=CONNECT_TIMEOUT_S):
        self._ensure_loop()
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    # -- RadioLink hooks
    def connect(self):
        try:
            from meshcore import MeshCore
        except (ImportError, SyntaxError) as e:   # SyntaxError: a release made for a newer Python (2.3.16 on 3.11)
            err = ("the meshcore package isn't installed (pip install \"meshcore>=2.3.14,!=2.3.16\"): needed for a "
                   "MeshCore radio" if isinstance(e, ImportError) else
                   f"this meshcore package doesn't run on this Python ({e.msg}): pip install \"meshcore>=2.3.14,!=2.3.16\"")
            if err != self.last_connect_error:
                log.error(err)
            self.last_connect_error = err
            return
        port = self._pick_port()
        if not port:
            return
        try:
            mc = self._run(MeshCore.create_serial(port, 115200))
            if mc is None:
                raise RuntimeError("no MeshCore radio answered")
            self.port = port   # before the radio says who it is (SELF_INFO), so that's logged with its port
            self.attach(mc)
            self._run(mc.commands.send_appstart())        # its own info (SELF_INFO), which says who it is
            self._run(mc.start_auto_message_fetching())  # messages it holds for us, fetched as they arrive
        except Exception as e:  # noqa: BLE001 - port busy, wrong firmware, unplugged: retry next loop
            log.warning("MeshCore connect to %s failed: %s", port, e)
            if str(e) != self.last_connect_error:
                self.event("connect_failed", port=port, error=str(e))
            self.last_connect_error = str(e)
            self.mc = None
            self._no_answer = self._no_answer + 1 if "answered" in str(e) else 0
            if self._no_answer >= RESET_AFTER_FAILS:
                self._reset_radio(port)
            return
        self._no_answer = 0
        with self.lock:
            self.mc, self.port = mc, port
            self.connected, self.connected_at, self.last_rx = True, time.time(), time.monotonic()
        self.last_connect_error = None
        self.event("connected", port=port, local=self.local_id, protocol=self.PROTOCOL, self_info=self.self_info)
        self.broadcast("status", self.status())

    def _reset_radio(self, port):
        """Restart an ESP32 radio that has stopped answering, as esptool's hard reset does (the USB serial's RTS line
        drives its reset). Found on a station: the library's retry toggles the control lines, which can leave an
        ESP32-S3 waiting in its USB bootloader, silent until someone resets it. Only for Espressif boards, at most
        every RESET_EVERY_S, and logged. Nothing is flashed or changed."""
        from mesh import ESPRESSIF_VID, list_ports
        if time.time() - self._last_reset < RESET_EVERY_S:
            return
        dev = next((p for p in list_ports.comports() if p.device == port), None)
        if dev is None or dev.vid != ESPRESSIF_VID:
            return
        self._last_reset, self._no_answer = time.time(), 0
        try:
            import serial
            with serial.Serial(port) as ser:
                ser.dtr = False   # GPIO0 high: boot the firmware, not the bootloader
                ser.rts = True    # EN low: hold it in reset...
                time.sleep(0.2)
                ser.rts = False   # ...and let it go
            log.warning("MeshCore radio on %s wasn't answering: restarted it", port)
            self.event("radio_reset", port=port, reason="not answering")
        except Exception as e:  # noqa: BLE001
            log.warning("couldn't restart the radio on %s: %s", port, e)

    def _nodes(self):
        """The 'node database' the pages read: a MeshCore companion has none we use, so just this radio, so a
        station lists itself (named, and placed where its settings say, if they do)."""
        info = self.self_info or {}
        if not self.local_id:
            return {}
        me = {"num": None, "user": {"longName": info.get("name") or self.local_id, "shortName": None, "role": "self"}}
        if info.get("adv_lat") or info.get("adv_lon"):
            me["position"] = {"latitude": info.get("adv_lat"), "longitude": info.get("adv_lon")}
        return {self.local_id: me}

    def drop_link(self):
        mc, self.mc = self.mc, None
        with self.lock:
            self.connected = False
        if mc is not None:
            try:
                self._run(mc.disconnect(), timeout=10)
            except Exception:  # noqa: BLE001
                pass
            self.event("disconnected", port=self.port)

    def tick(self):
        if self.connected and self.mc is not None and time.time() - self._last_stats > STATS_EVERY_S:
            self._last_stats = time.time()
            for ask in ("get_bat", "get_stats_core", "get_stats_radio", "get_stats_packets"):
                try:
                    self._run(getattr(self.mc.commands, ask)(), timeout=15)
                except Exception as e:  # noqa: BLE001 - older firmware lacks some counters
                    log.debug("MeshCore %s: %s", ask, e)

    def send_text(self, text, to=None, channel=0):
        raise RuntimeError("sending isn't available on a MeshCore station yet")

    def traceroute(self, target, origin="manual"):
        raise RuntimeError("traceroutes aren't available on a MeshCore station yet")

    def status(self):
        return {**super().status(), "protocol": self.PROTOCOL,
                "radio": {k: (self.self_info or {}).get(k) for k in ("name", "radio_freq", "radio_bw", "radio_sf")}}

    def _pick_port(self):
        from mesh import resolve_port
        if not self.port_hint:
            err = "a MeshCore radio needs [radio] port set in lorakeet.toml"
            if err != self.last_connect_error:
                log.error(err)
            self.last_connect_error = err
            return None
        return resolve_port(self.port_hint)

    # -- events
    def attach(self, source):
        """Subscribe the handlers to a MeshCore object or a bare EventDispatcher (tests)."""
        from meshcore import EventType
        handlers = {EventType.RX_LOG_DATA: self.on_rx, EventType.SELF_INFO: self.on_self_info,
                    EventType.CONTACT_MSG_RECV: self.on_direct_message, EventType.CHANNEL_MSG_RECV: self.on_channel_message}
        for kind in ("STATS_CORE", "STATS_RADIO", "STATS_PACKETS", "BATTERY"):
            if hasattr(EventType, kind):
                handlers[getattr(EventType, kind)] = self.on_counters
        for kind, fn in handlers.items():
            source.subscribe(kind, lambda e, fn=fn: self._safely(fn, e))

    def _safely(self, fn, e):
        self.last_rx = time.monotonic()
        if not self.local_id and fn != self.on_self_info:
            # not yet known which station this is: stored now, its rows would carry another radio's station (the
            # main radio's, in a [[radios]] setup). Held until SELF_INFO, a moment after connecting.
            if len(self._early) < 1000:
                self._early.append((fn, e))
            return
        try:
            fn(e.payload if isinstance(e.payload, dict) else {}, getattr(e, "type", None))
        except Exception:  # noqa: BLE001 - one bad frame must never stop logging
            log.exception("MeshCore event %s", getattr(e, "type", "?"))

    def on_self_info(self, p, _kind=None):
        key = p.get("public_key")
        if not key:
            return
        self.self_info = {k: v for k, v in p.items() if k != "public_key"}
        sid = radioid.from_meshcore_key(key)
        if sid != self.local_id:
            self.local_id = sid
            self.store.claim_station(sid)
            log.info("MeshCore radio %s (%s) on %s", sid, p.get("name"), self.port)
            # identities this station already logged: a restart doesn't log every radio's identity again
            for r in self.store.query("SELECT node, long_name, role, public_key FROM node_info WHERE rowid IN "
                                      "(SELECT MAX(rowid) FROM node_info WHERE station = ? GROUP BY node)", sid):
                self._identity.setdefault(r["node"], (r["long_name"], r["role"], r["public_key"]))
        self._identify(sid, p.get("name"), "self", key, time.time())
        early, self._early = self._early, []
        for fn, e in early:
            self._safely(fn, e)

    def on_rx(self, p, _kind=None):
        """A reception: the library's payload carries the packet as hex ("payload"), with snr and rssi."""
        raw = p.get("payload")
        if raw:
            self.log_reception(raw, p.get("snr"), p.get("rssi"), time.time())

    def log_reception(self, raw, snr, rssi, ts):
        pkt = meshcore_packet.parse(raw)
        if pkt is None:   # malformed: kept, with what's known
            self.store.insert("mc_rx", ts=ts, snr=snr, rssi=rssi, length=len(raw) // 2, frame=raw)
            return
        advert = meshcore_packet.parse_advert(pkt["payload"]) if pkt["payloadType"] == meshcore_packet.ADVERT else None
        sender = radioid.from_meshcore_key(advert["publicKey"]) if advert else None
        t1, t2 = pkt["transport"] or (None, None)
        self.store.insert("mc_rx", ts=ts, snr=snr, rssi=rssi, length=len(bytes.fromhex(raw)), route=pkt["route"],
                          payload_type=pkt["payloadType"], payload_name=pkt["payloadName"], version=pkt["version"],
                          transport1=t1, transport2=t2, hop_bytes=pkt["hopBytes"], hops=len(pkt["hops"]),
                          path=",".join(pkt["hops"]), packet_hash=pkt["hash"], from_id=sender, frame=raw)
        if sender:
            r = self.rx.setdefault(sender, {"count": 0})
            r.update(ts=ts, snr=snr, rssi=rssi, hops=len(pkt["hops"]), count=r.get("count", 0) + 1)
        if self._first_hearing(pkt["hash"], ts):
            summary = self._summary(pkt, advert)
            self.store.insert("packets", ts=ts, from_id=sender, to_id=None, portnum=f"MC_{pkt['payloadName']}",
                              channel=None, snr=snr, rssi=rssi, hops=len(pkt["hops"]), summary=summary,
                              # 52 bits of the packet hash: never negative, and exact in a browser (2^53); rows
                              # logged before 2026-10-15 used 60 bits
                              pkt_id=int(pkt["hash"][:13], 16),
                              raw=json.dumps({"meshcore": {k: v for k, v in pkt.items() if k != "payload"},
                                              "advert": advert, "frame": raw}))
            self.broadcast("packet", {"ts": ts, "from_id": sender, "to_id": None, "portnum": f"MC_{pkt['payloadName']}",
                                      "snr": snr, "rssi": rssi, "hops": len(pkt["hops"]), "summary": summary,
                                      "pkt_id": int(pkt["hash"][:13], 16), "name": self.name(sender) if sender else None,
                                      "station": self.local_id})
        if advert:
            self._identify(sender, advert["name"], advert["type"], advert["publicKey"], ts, advert)

    def _first_hearing(self, h, ts):
        while self._seen and ts - next(iter(self._seen.values())) > SEEN_WINDOW_S:
            self._seen.popitem(last=False)
        if h in self._seen:
            return False
        self._seen[h] = ts
        return True

    @staticmethod
    def _summary(pkt, advert):
        path = f" via {'>'.join(pkt['hops'])}" if pkt["hops"] else ""
        if advert:
            who = advert["name"] or "(no name)"
            return f"advert: {who} ({advert['type']}){path}"
        return f"{pkt['payloadName'].lower()} ({pkt['route']}){path}"

    def _identify(self, node, name, kind, key, ts, advert=None):
        sig = (name, kind, key)
        if self._identity.get(node) != sig:
            self._identity[node] = sig
            self.known[node] = {"node": node, "long_name": name, "short_name": None, "hw_model": None, "role": kind}
            self.store.insert("node_info", ts=ts, node=node, long_name=name, short_name=None, hw_model=None,
                              role=kind, public_key=key,
                              data=json.dumps({"clock": meshcore_packet.clock_status(advert["timestamp"], ts),
                                               "advertTime": advert["timestamp"]}) if advert else None)
        if advert and advert["lat"] is not None:
            self.pos[node] = {"node": node, "ts": ts, "lat": advert["lat"], "lon": advert["lon"], "alt": None,
                              "precision_bits": 32}
            self.store.insert("positions", ts=ts, node=node, lat=advert["lat"], lon=advert["lon"], alt=None,
                              precision_bits=32, source="advert")

    def on_direct_message(self, p, _kind=None):
        prefix = p.get("pubkey_prefix") or ""
        sender = f"mc:{prefix}" if radioid.is_meshcore(f"mc:{prefix}") else None
        self.store.insert("messages", ts=time.time(), from_id=sender, to_id=self.local_id, channel=None,
                          text=str(p.get("text") or "")[:TEXT_MAX])

    def on_channel_message(self, p, _kind=None):
        self.store.insert("messages", ts=time.time(), from_id=None, to_id="^all", channel=p.get("channel_idx"),
                          text=str(p.get("text") or "")[:TEXT_MAX])   # "Name: text", as MeshCore sends it

    def on_counters(self, p, kind=None):
        if self.local_id:
            name = getattr(kind, "value", str(kind))
            self.store.insert("telemetry_full", ts=time.time(), node=self.local_id, kind=f"meshcore_{name}",
                              data=json.dumps(p, default=str))
