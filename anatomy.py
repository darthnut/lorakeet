"""Packet anatomy: one logged packet rebuilt layer by layer, as bytes, with every field labelled.

What our radio hands the dashboard is already decoded (and decrypted). This module rebuilds the layers
underneath from what was logged, and says for each value where it came from:

  received   bytes exactly as the radio reported them (the app payload; ciphertext we couldn't read)
  logged     a value the radio reported (API packet or its own debug-log RX line), placed in the
             firmware's byte layout
  rebuilt    bytes re-derived from logged values: the Data envelope re-serialized, the body re-encrypted
             with the channel key. Checked against the radio's own logged length where it logged one.

Layouts are from firmware 2.7.26: PacketHeader (src/mesh/RadioInterface.h, 16 bytes, little-endian),
CryptoEngine (AES-CTR, nonce = packet id as u64 LE + sender u32 LE + 4 zero bytes), Channels::getKey
(1-byte PSK = well-known default, short keys zero-padded), RadioInterface (preamble 16 symbols,
frequency slot = djb2(channel name) % number of slots). Keys are used in memory only, never returned.
"""
import base64
import math
import struct

from google.protobuf import descriptor as _d
from google.protobuf import json_format
from meshtastic.protobuf import mesh_pb2, portnums_pb2, storeforward_pb2, telemetry_pb2

# ---------------------------------------------------------------- radio (LoRa) layer

# preset -> (bandwidth kHz, spreading factor, coding rate denominator), firmware RadioInterface.cpp
PRESETS = {
    "SHORT_TURBO": (500, 7, 5), "SHORT_FAST": (250, 7, 5), "SHORT_SLOW": (250, 8, 5),
    "MEDIUM_FAST": (250, 9, 5), "MEDIUM_SLOW": (250, 10, 5), "LONG_FAST": (250, 11, 5),
    "LONG_TURBO": (500, 11, 5), "LONG_MODERATE": (125, 11, 8), "LONG_SLOW": (125, 12, 8),
    "VERY_LONG_SLOW": (62.5, 12, 8),
}
# region -> (start MHz, end MHz), firmware src/mesh/RadioInterface.cpp regions[] (spacing 0 for these)
REGIONS = {"US": (902.0, 928.0), "EU_868": (869.4, 869.65), "ANZ": (915.0, 928.0), "EU_433": (433.0, 434.0),
           "NZ_865": (864.0, 868.0), "IN": (865.0, 867.0), "KR": (920.0, 923.0), "TW": (920.0, 925.0)}
PREAMBLE_SYMBOLS = 16


def _djb2(s):
    h = 5381
    for c in s.encode():
        h = ((h << 5) + h + c) & 0xFFFFFFFF
    return h


def radio_layer(lora, onair_len):
    """Frequency, modulation and time on air for a frame of onair_len bytes (header + body)."""
    lora = lora or {}
    preset = lora.get("preset") or "LONG_FAST"
    if lora.get("use_preset", True):
        bw, sf, cr = PRESETS.get(preset, PRESETS["LONG_FAST"])
    else:
        bw, sf, cr = lora.get("bandwidth") or 250, lora.get("spread_factor") or 11, lora.get("coding_rate") or 5
    out = {"preset": preset if lora.get("use_preset", True) else "custom", "bwKHz": bw, "sf": sf, "cr": f"4/{cr}",
           "region": lora.get("region"), "presetAssumed": not lora}
    # frequency slot
    region = REGIONS.get(lora.get("region") or "")
    if lora.get("override_frequency"):
        out["freqMHz"] = lora["override_frequency"] + (lora.get("frequency_offset") or 0)
        out["slotHow"] = "override frequency set on the radio"
    elif region:
        slots = int((region[1] - region[0]) / (bw / 1000))
        name = lora.get("channel_name") or "LongFast"
        if lora.get("channel_num"):
            slot, how = lora["channel_num"] - 1, f"slot {lora['channel_num']} set on the radio"
        else:
            slot, how = _djb2(name) % slots, f'hash of channel name "{name}" mod {slots} slots'
        out.update(freqMHz=round(region[0] + bw / 2000 + slot * bw / 1000 + (lora.get("frequency_offset") or 0), 4),
                   slot=slot + 1, slots=slots, slotHow=how)
    # time on air (Semtech AN1200.13), explicit header, CRC on
    tsym = (2 ** sf) / (bw * 1000) * 1000  # ms
    de = 1 if tsym > 16 else 0  # low data rate optimisation, as RadioLib enables it
    payload_symbols = 8 + max(math.ceil((8 * onair_len - 4 * sf + 28 + 16) / (4 * (sf - 2 * de))) * cr, 0)
    out.update(symbolMs=round(tsym, 3), preambleSymbols=PREAMBLE_SYMBOLS, payloadSymbols=payload_symbols,
               preambleMs=round((PREAMBLE_SYMBOLS + 4.25) * tsym, 1), payloadMs=round(payload_symbols * tsym, 1),
               airtimeMs=round((PREAMBLE_SYMBOLS + 4.25 + payload_symbols) * tsym, 1),
               bitsPerSymbol=sf, onAirBytes=onair_len, syncWord="0x2B")
    return out


# ---------------------------------------------------------------- protobuf walker

PORT_TYPES = {
    "POSITION_APP": mesh_pb2.Position, "NODEINFO_APP": mesh_pb2.User, "ROUTING_APP": mesh_pb2.Routing,
    "TELEMETRY_APP": telemetry_pb2.Telemetry, "TRACEROUTE_APP": mesh_pb2.RouteDiscovery,
    "NEIGHBORINFO_APP": mesh_pb2.NeighborInfo, "WAYPOINT_APP": mesh_pb2.Waypoint,
    "STORE_FORWARD_APP": storeforward_pb2.StoreAndForward,
}
NODE_FIELDS = {"dest", "source", "route", "route_back", "node_id", "last_sent_by_id", "from", "to"}
TIME_FIELDS = {"time", "timestamp", "expire", "rx_time", "last_heard"}
WIRE = {0: "varint", 1: "64-bit", 2: "length-delimited", 5: "32-bit"}


def _varint(b, i):
    v = shift = 0
    while True:
        if i >= len(b):
            raise ValueError("truncated varint")
        v |= (b[i] & 0x7F) << shift
        shift += 7
        i += 1
        if not b[i - 1] & 0x80:
            return v, i


def _signed(fd, v):
    """A raw varint as the field's type reads it: zigzag for sint, two's complement for int32/int64
    (a negative int32 is sign-extended to 10 bytes on the wire)."""
    if fd and fd.type in (_d.FieldDescriptor.TYPE_SINT32, _d.FieldDescriptor.TYPE_SINT64):
        return (v >> 1) ^ -(v & 1)
    if fd and fd.type in (_d.FieldDescriptor.TYPE_INT32, _d.FieldDescriptor.TYPE_INT64) and v >= 1 << 63:
        return v - (1 << 64)
    return v


def _repeated(fd):
    r = getattr(fd, "is_repeated", None)  # protobuf 5+; older versions only have .label
    return r if r is not None else fd.label == _d.FieldDescriptor.LABEL_REPEATED


def _show(fd, v):
    """Human value for a scalar, with the obvious unit conversions noted."""
    name = fd.name if fd else ""
    if fd and fd.type == _d.FieldDescriptor.TYPE_ENUM:
        ev = fd.enum_type.values_by_number.get(v)
        return f"{ev.name} ({v})" if ev else str(v)
    if fd and fd.type == _d.FieldDescriptor.TYPE_BOOL:
        return "true" if v else "false"
    if name.endswith(("latitude_i", "longitude_i")):
        return f"{v} → {v / 1e7:.7f}°"
    if name in NODE_FIELDS and isinstance(v, int):
        return f"!{v:08x}"
    if name in TIME_FIELDS and isinstance(v, int) and v > 1_000_000_000:
        import time as _t
        return f"{v} → {_t.strftime('%Y-%m-%d %H:%M:%S', _t.localtime(v))}"
    if isinstance(v, float):
        return f"{v:.6g}"
    return str(v)


def walk(buf, desc, base=0, depth=0, prefix="", jpath=""):
    """Every field in a protobuf message: byte offset, length, tag, wire type, name, value. Nested messages
    are listed before their fields (one level deeper), so the deepest field wins when mapping a byte.
    jpath: where this message sits in the logged JSON; each field gets its JSON path(s) (array indexes
    left out) so the page can link a field to its line in the JSON."""
    out, i = [], 0
    while i < len(buf):
        start = i
        try:
            key, i = _varint(buf, i)
            fno, wt = key >> 3, key & 7
            fd = desc.fields_by_number.get(fno) if desc else None
            name = prefix + (fd.name if fd else f"field {fno}")
            tag_len = i - start
            entry = {"start": base + start, "tagLen": tag_len, "name": name, "field": fno, "wire": WIRE.get(wt, wt), "depth": depth}
            if fd and jpath:
                entry["json"] = [f"{jpath}.{fd.json_name}"]
                if fd.name.endswith(("latitude_i", "longitude_i")):  # the library adds the converted value too
                    entry["json"].append(f"{jpath}.{fd.json_name[:-1]}")
            if wt == 0:
                v, i = _varint(buf, i)
                entry["value"] = _show(fd, _signed(fd, v))
            elif wt in (1, 5):
                n = 8 if wt == 1 else 4
                raw = buf[i:i + n]
                if len(raw) < n:
                    raise ValueError("truncated fixed field")
                i += n
                t = fd.type if fd else None
                if t in (_d.FieldDescriptor.TYPE_FLOAT, _d.FieldDescriptor.TYPE_DOUBLE):
                    v = struct.unpack("<f" if n == 4 else "<d", raw)[0]
                elif t in (_d.FieldDescriptor.TYPE_SFIXED32, _d.FieldDescriptor.TYPE_SFIXED64):
                    v = struct.unpack("<i" if n == 4 else "<q", raw)[0]
                else:
                    v = struct.unpack("<I" if n == 4 else "<Q", raw)[0]
                entry["value"] = _show(fd, v)
            elif wt == 2:
                ln, i = _varint(buf, i)
                entry["tagLen"] = i - start
                data = buf[i:i + ln]
                if len(data) < ln:
                    raise ValueError("truncated field")
                if fd and fd.type == _d.FieldDescriptor.TYPE_MESSAGE:
                    entry["value"] = f"{fd.message_type.name} message, {ln} bytes"
                    entry["len"] = i + ln - start
                    out.append(entry)
                    out.extend(walk(data, fd.message_type, base + i, depth + 1, "", f"{jpath}.{fd.json_name}" if jpath else ""))
                    i += ln
                    continue
                if fd and fd.type == _d.FieldDescriptor.TYPE_STRING:
                    entry["value"] = repr(data.decode("utf-8", "replace"))
                elif fd and _repeated(fd) and fd.type != _d.FieldDescriptor.TYPE_BYTES:
                    vals = []  # packed repeated scalars
                    if fd.type in (_d.FieldDescriptor.TYPE_FIXED32, _d.FieldDescriptor.TYPE_SFIXED32, _d.FieldDescriptor.TYPE_FLOAT):
                        vals = [struct.unpack("<I", data[k:k + 4])[0] for k in range(0, len(data) - 3, 4)]
                    else:
                        k = 0
                        while k < len(data):
                            x, k = _varint(data, k)
                            vals.append(_signed(fd, x))
                    entry["value"] = ", ".join(_show(fd, x) for x in vals) or "(empty)"
                elif fd is None and data and all(32 <= c < 127 for c in data):
                    entry["value"] = repr(data.decode())
                else:
                    entry["value"] = f"{ln} bytes" + (f": {data.hex(' ')}" if 0 < ln <= 32 else "")
                i += ln
            else:
                raise ValueError(f"wire type {wt}")
            entry["len"] = i - start
            out.append(entry)
        except (ValueError, IndexError, struct.error) as e:
            out.append({"start": base + start, "len": len(buf) - start, "name": "unparsed", "value": str(e), "depth": depth})
            break
    return out


# ---------------------------------------------------------------- the packet

def _aes_key(psk):
    if not psk:
        return None
    if len(psk) < 16:
        return psk + bytes(16 - len(psk))
    if 16 < len(psk) < 32:
        return psk + bytes(32 - len(psk))
    return psk[:32]


def _ctr(key, nonce, data):
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    e = Cipher(algorithms.AES(key), modes.CTR(nonce)).encryptor()
    return e.update(data) + e.finalize()


def _traceroute_on_air(payload, is_reply):
    """A traceroute addressed to us, as it was on air: our radio appends its own reception SNR to the list for
    the direction the packet travelled (snr_back for a reply, snr_towards for a request) before handing it
    over (firmware TraceRouteModule). Returns (on-air payload bytes, field name, removed value) or None."""
    rd = mesh_pb2.RouteDiscovery()
    rd.ParseFromString(payload)
    field = "snr_back" if is_reply else "snr_towards"
    lst = getattr(rd, field)
    if not lst:
        return None
    removed = lst[-1]
    del lst[-1]
    return rd.SerializeToString(), field, removed


def build(row, rx, channels, lora, keys, local=None):
    """row: packets row (raw JSON parsed into row['raw']); rx: the matching rx_hops reception or None;
    channels: settings['radio_channels'] list; lora: settings['radio_lora']; keys: {hash: aes key};
    local: our radio's node id (traceroutes addressed to it were edited by it on arrival)."""
    p = row["raw"]
    d = p.get("decoded")
    to, frm, pid = p.get("to", 0), p.get("from", 0), p.get("id", 0)
    pki = bool(p.get("pkiEncrypted")) or bool(row.get("pki"))
    checks, notes = [], []

    def src(k_rx, v_api):
        """Prefer the radio's own debug-log value for this copy; fall back to the API packet."""
        if rx is not None and rx.get(k_rx) is not None:
            return rx[k_rx], "logged", "radio debug log (RX line)"
        return v_api, "logged" if v_api is not None else "unknown", "packet from the radio's API" if v_api is not None else "not logged"

    hop_limit, *hl = src("hop_limit", p.get("hopLimit"))
    hop_start, *hs = src("hop_start", p.get("hopStart"))
    want_ack, *wa = src("want_ack", 1 if p.get("wantAck") else 0)
    relay, *rl = src("relay", p.get("relayNode"))
    next_hop, *nh = src("next_hop", p.get("nextHop"))
    via_mqtt = 1 if p.get("viaMqtt") else 0
    # channel byte: a hash on air. Decoded packets report a channel *index*; map it via our radio's channels.
    if pki:
        ch_hash, chs = 0, ("logged", "PKI direct messages always carry channel byte 0")
    elif rx is not None and rx.get("channel") is not None:
        ch_hash, chs = rx["channel"], ("logged", "radio debug log (RX line)")
    elif d is None:
        ch_hash, chs = p.get("channel"), ("logged", "packet from the radio's API (undecoded packets report the hash)")
    else:
        idx = p.get("channel", 0)
        ch = next((c for c in channels if c["index"] == idx), None)
        ch_hash, chs = (ch["hash"], ("rebuilt", f"hash of our channel {idx} ({ch['name']})")) if ch else (None, ("unknown", "channel not known"))
    channel = next((c for c in channels if c["hash"] == ch_hash), None) if ch_hash is not None and not pki else None

    # ---- layer 4/5: the decrypted Data envelope and the app payload
    data_bytes = payload = traceroute_fix = None
    data_fields = payload_fields = []
    port = d.get("portnum") if d else None
    if d is not None:
        names = {f.json_name for f in mesh_pb2.Data.DESCRIPTOR.fields} | {f.name for f in mesh_pb2.Data.DESCRIPTOR.fields}
        msg = json_format.ParseDict({k: v for k, v in d.items() if k in names}, mesh_pb2.Data(), ignore_unknown_fields=True)
        app_key = next((k for k in d if k not in names and k != "raw"), None)  # e.g. "position", "telemetry"
        payload = base64.b64decode(d.get("payload") or "")
        # A traceroute to us carries one SNR entry our radio added on arrival: rebuild the on-air envelope
        # without it, and keep that only if it comes to exactly the length the radio logged on air.
        if port == "TRACEROUTE_APP" and local and to == int(local[1:], 16) and not pki:
            try:
                fix = _traceroute_on_air(payload, bool(d.get("requestId")))
            except Exception:  # noqa: BLE001  (unparseable payload: leave it as delivered)
                fix = None
            logged = rx.get("length") if rx else None
            if fix:
                trial = mesh_pb2.Data()
                trial.CopyFrom(msg)
                trial.payload = fix[0]
                if logged is None or logged == 16 + len(trial.SerializeToString()):
                    msg = trial
                    snr = fix[2] / 4
                    traceroute_fix = {"field": fix[1], "value": fix[2], "db": snr, "verified": logged is not None}
        data_bytes = msg.SerializeToString()
        data_fields = walk(data_bytes, mesh_pb2.Data.DESCRIPTOR, jpath="decoded")
        ptype = PORT_TYPES.get(port)
        if port in ("TEXT_MESSAGE_APP", "TEXT_MESSAGE_COMPRESSED_APP", "RANGE_TEST_APP", "DETECTION_SENSOR_APP"):
            payload_fields = [{"start": 0, "len": len(payload), "name": "text (UTF-8)", "value": repr(payload.decode("utf-8", "replace")), "depth": 0, "json": ["decoded.text"]}]
        else:
            payload_fields = walk(payload, ptype.DESCRIPTOR if ptype else None, jpath=f"decoded.{app_key}" if app_key else "")
            if not ptype:
                notes.append(f"No message definition for {port} in library 2.7.11: fields are shown by number.")

    # ---- layer 2/3: the encrypted body
    body, body_prov, crypto = None, None, None
    if d is None and p.get("encrypted"):
        body, body_prov = base64.b64decode(p["encrypted"]), ("received", "ciphertext exactly as received: our radio has no key for this channel")
    elif pki:
        body_prov = ("unknown", "PKI-encrypted (AES-CCM with a key shared only by sender and recipient): the ciphertext can't be rebuilt")
        crypto = {"scheme": "PKI: X25519 key agreement + AES-256-CCM", "rebuilt": False,
                  "note": "Encrypted to the recipient's public key. The radio decrypted it because it was addressed to us; "
                          "the on-air body is the ciphertext plus an 8-byte authentication tag and a 4-byte extra nonce."}
    elif data_bytes is not None:
        key = keys.get(ch_hash)
        nonce = struct.pack("<QI", pid, frm) + bytes(4)
        crypto = {"scheme": "AES-%d-CTR" % (len(key) * 8 if key else 128), "nonce": nonce.hex(),
                  "nonceFields": [{"start": 0, "len": 8, "name": "packet id (64-bit, little-endian)", "json": ["id"], "value": f"0x{pid:08x}"},
                                  {"start": 8, "len": 4, "name": "sender", "json": ["from", "fromId"], "value": f"!{frm:08x}"},
                                  {"start": 12, "len": 4, "name": "block counter", "value": "0, counts up per 16-byte block"}],
                  "key": (f"{channel['name']} key (" + ("the public default key" if channel.get("publicKey") else "from our radio, not shown") + ")") if channel else "unknown"}
        if key:
            body = _ctr(key, nonce, data_bytes)
            body_prov = ("rebuilt", "re-encrypted from the rebuilt envelope with the channel key: a reconstruction, not a capture")
            crypto["rebuilt"] = True
        else:
            body_prov = ("unknown", "the channel key isn't available (radio not connected?), so the ciphertext can't be rebuilt")
            crypto["rebuilt"] = False

    # The radio's RX line logs the whole frame length (16-byte header + body). Measured over 441 packets on
    # 2026-10-06: 436 rebuilt to exactly that size; 4 traceroute replies don't (our radio appends its own SNR to
    # snr_back before handing them over); one NodeInfo carried 66 extra envelope bytes the radio dropped
    # (its own log: on air len=172, payloadlen=84), probably a field our firmware/library doesn't know.
    logged_len = rx.get("length") if rx else None
    body_len = len(body) if body is not None else (len(data_bytes) + 12 if pki and data_bytes is not None else None)
    if logged_len is not None and body_len is not None:
        ok = logged_len == 16 + body_len
        why = ""
        if ok and traceroute_fix:
            why = (f" Rebuilt without the {traceroute_fix['field']} entry our radio added on arrival"
                   f" ({traceroute_fix['value']} = {traceroute_fix['db']:g} dB, its own reception SNR).")
        elif not ok and port == "TRACEROUTE_APP":
            why = (" Expected for a traceroute: our radio appends its own reception SNR before handing it to the"
                   " dashboard, and removing it didn't reproduce the logged length either.")
        elif not ok:
            why = f" The rebuild is {abs(logged_len - 16 - body_len)} bytes {'shorter' if logged_len > 16 + body_len else 'longer'} than what was on air, so the radio passed on a different payload than it received."
        parts = f"16-byte header + {len(data_bytes)}-byte envelope + 12 bytes PKI overhead" if pki else f"16-byte header + {body_len}-byte body"
        checks.append({"ok": ok, "text": f"{parts} = {16 + body_len} bytes; the radio logged {logged_len} on air for this reception.{why}"})
    if channel and d is not None and not pki and rx is not None and rx.get("channel") is not None:
        idx_ch = next((c for c in channels if c["index"] == p.get("channel", 0)), None)
        if idx_ch:
            checks.append({"ok": idx_ch["hash"] == rx["channel"], "text": f"Channel hash computed from our channel settings 0x{idx_ch['hash']:02x}; logged on air 0x{rx['channel']:02x}"})

    # ---- layer 2: the frame (16-byte header + body)
    def b(v):
        return 0 if v is None else int(v) & 0xFF
    flags = (b(hop_limit) & 7) | (8 if want_ack else 0) | (16 if via_mqtt else 0) | ((b(hop_start) & 7) << 5)
    header = struct.pack("<III", to & 0xFFFFFFFF, frm & 0xFFFFFFFF, pid & 0xFFFFFFFF) + bytes([flags, b(ch_hash), b(next_hop), b(relay)])
    unknown_bytes = [13] * (ch_hash is None) + [14] * (next_hop is None) + [15] * (relay is None)
    frame_fields = [
        {"start": 0, "len": 4, "name": "destination", "json": ["to", "toId"], "value": "broadcast (!ffffffff)" if to == 0xFFFFFFFF else f"!{to:08x}", "prov": "logged", "src": "packet from the radio's API"},
        {"start": 4, "len": 4, "name": "sender", "json": ["from", "fromId"], "value": f"!{frm:08x}", "prov": "logged", "src": "packet from the radio's API"},
        {"start": 8, "len": 4, "name": "packet id", "json": ["id"], "value": f"0x{pid:08x}", "prov": "logged", "src": "packet from the radio's API"},
        {"start": 12, "len": 1, "name": "flags", "json": ["hopStart", "viaMqtt", "wantAck", "hopLimit"], "value": f"0b{flags:08b}", "prov": hl[0], "src": hl[1], "bits": [
            {"bits": "7–5", "name": "hop start", "value": hop_start, "src": hs[1]},
            {"bits": "4", "name": "via MQTT", "value": via_mqtt, "src": "packet from the radio's API"},
            {"bits": "3", "name": "want ACK", "value": want_ack, "src": wa[1]},
            {"bits": "2–0", "name": "hop limit", "value": hop_limit, "src": hl[1]}]},
        {"start": 13, "len": 1, "name": "channel hash", "json": ["channel", "pkiEncrypted"], "value": "unknown" if ch_hash is None else f"0x{ch_hash:02x}" + (f" ({channel['name']})" if channel else " (PKI)" if pki else " (not one of our channels)"), "prov": chs[0], "src": chs[1]},
        {"start": 14, "len": 1, "name": "next hop", "json": ["nextHop"], "value": "unknown" if next_hop is None else f"0x{next_hop:02x}" + (" (none: flood routing)" if not next_hop else ""), "prov": nh[0], "src": nh[1]},
        {"start": 15, "len": 1, "name": "relay node", "json": ["relayNode"], "value": "unknown" if relay is None else f"0x{relay:02x}" + (" (low byte of the last relayer's id)" if relay else ""), "prov": rl[0], "src": rl[1]},
    ]
    if body is not None:
        frame_fields.append({"start": 16, "len": len(body), "name": "encrypted body", "json": ["encrypted"] if d is None else [], "value": f"{len(body)} bytes", "prov": body_prov[0], "src": body_prov[1]})
    frame = header + (body or b"")

    onair = 16 + (body_len if body_len is not None else (logged_len or 0))
    return {
        "rowid": row["rowid"], "ts": row["ts"], "portnum": port or "ENCRYPTED", "pki": pki,
        "rssi": p.get("rxRssi"), "snr": p.get("rxSnr"), "transport": p.get("transportMechanism"),
        "radio": radio_layer(lora, onair),
        "frame": {"hex": frame.hex(), "fields": frame_fields, "unknownBytes": unknown_bytes,
                  "bodyMissing": body is None, "bodyLen": body_len, "bodyProv": body_prov},
        "crypto": crypto,
        "data": {"hex": data_bytes.hex(), "fields": data_fields} if data_bytes is not None else None,
        "payload": {"hex": payload.hex(), "fields": payload_fields, "type": (PORT_TYPES[port].DESCRIPTOR.name if port in PORT_TYPES else None)} if payload is not None else None,
        "checks": checks, "notes": notes, "reception": dict(rx) if rx else None, "tracerouteFix": traceroute_fix,
        "json": p,
    }


def port_name(n):
    try:
        return portnums_pb2.PortNum.Name(n)
    except ValueError:
        return str(n)
