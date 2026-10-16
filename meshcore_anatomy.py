"""Packet anatomy for MeshCore (docs/MESHCORE.md, step 7): one logged packet laid out byte by byte.

Unlike Meshtastic's anatomy (anatomy.py), nothing here is rebuilt: a MeshCore station stores the packet's exact bytes
as its radio received them (packets.raw "frame", mc_rx.frame), so every field is read straight from those bytes.
Layers: the radio (settings the station's radio reported), the frame (header, transport codes, path), the payload by
type, channel-message decryption where the key is public, the packet hash, and every hearing of the packet.

Decryption: a channel message (GRP_TXT) is AES-128-ECB under the channel's 16-byte secret, with a 2-byte MAC (the
start of HMAC-SHA256 over the ciphertext, keyed with the secret) and a 1-byte channel hash (the first byte of the
secret's SHA-256), as in the firmware and the meshcore library. Only keys anyone can know are tried: the Public
channel's published one, and hashtag channels' (the SHA-256 of the name, first 16 bytes) for a name the viewer types.
The MAC proves a key right; nothing is shown decrypted without it.
"""
import hashlib
import hmac
import time

import meshcore_packet
from anatomy import radio_layer

PUBLIC_SECRET = bytes.fromhex("8b3387e9c5cdea6ac9e5edbaa115cd72")   # the Public channel's published key
PREAMBLE_SYMBOLS = 16      # the firmware's begin(..., preamble 16) (CustomSX1262 and friends)
SYNC_WORD = "0x12"         # RadioLib's private sync word, which MeshCore uses (Meshtastic: 0x2B)
TXT_TYPES = {0: "plain text", 1: "CLI command", 2: "signed plain text"}
# payloads that start with destination and source hashes, a MAC and ciphertext (firmware Mesh.cpp)
ADDRESSED = {0: "request", 1: "response", 2: "direct message", 8: "returned path"}


def _when(ts):
    """A radio's own timestamp, readable (it's the sender's clock: see the notes on unset clocks)."""
    return f"{ts} ({time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(ts))})" if ts else str(ts)


def channel_hash(secret):
    return hashlib.sha256(secret).digest()[0]


def mac_ok(secret, mac, ciphertext):
    return hmac.new(secret, ciphertext, hashlib.sha256).digest()[:2] == mac


def hashtag_secret(name):
    """A hashtag channel's key: the first 16 bytes of the SHA-256 of its name, "#" included."""
    name = name.strip()
    return hashlib.sha256(("#" + name.lstrip("#")).encode()).digest()[:16]


def seal_channel_message(secret, ts, text, attempt=0):
    """A channel message's payload as a MeshCore radio sends it (the inverse of what's read below): channel hash, MAC,
    AES-128-ECB of timestamp, flags and "name: text", zero-padded to a whole block. For made-up traffic (the demo)."""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    plain = int(ts).to_bytes(4, "little") + bytes([attempt & 3]) + text.encode()
    plain += bytes(-len(plain) % 16)
    e = Cipher(algorithms.AES(secret), modes.ECB()).encryptor()
    ct = e.update(plain) + e.finalize()
    return bytes([channel_hash(secret)]) + hmac.new(secret, ct, hashlib.sha256).digest()[:2] + ct


def _decrypt(secret, ciphertext):
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    d = Cipher(algorithms.AES(secret), modes.ECB()).decryptor()
    return d.update(ciphertext[:len(ciphertext) - len(ciphertext) % 16]) + d.finalize()


def _f(start, length, name, value, json=(), src="received bytes", note=None, **kw):
    """A field; `note` explains it (shown on hover, like the Meshtastic anatomy's sources)."""
    return {"start": start, "len": length, "name": name, "value": value, "prov": "received",
            "src": f"{src}: {note}" if note else src, "json": list(json), **kw}


def _radio(settings, frame_len):
    """The radio layer from the station radio's own report (SELF_INFO: radio_freq MHz, radio_bw kHz, radio_sf,
    radio_cr), with MeshCore's preamble and sync word."""
    s = settings or {}
    bw, sf, cr = s.get("radio_bw"), s.get("radio_sf"), s.get("radio_cr")
    known = all(isinstance(x, (int, float)) and x for x in (bw, sf, cr))
    out = radio_layer({"use_preset": False, "bandwidth": bw if known else 62.5, "spread_factor": sf if known else 7,
                       "coding_rate": cr if known else 5}, frame_len)
    # radio_layer is Meshtastic's (preamble 16 there too); restate what differs
    out.update(preset="MeshCore", presetAssumed=not known, syncWord=SYNC_WORD, region=None, slot=None, slots=None,
               freqMHz=s.get("radio_freq"), preambleSymbols=PREAMBLE_SYMBOLS,
               slotHow="the frequency the station's radio reported" if s.get("radio_freq") else None)
    return out


def _who(prefix, nodes):
    """Names for a path's key prefix: the MeshCore radios whose id starts with it (ids are the key's first 6 bytes)."""
    hits = sorted(n for n in nodes if n.startswith("mc:" + prefix))
    return hits


def build(row, receptions, radio_settings, nodes, describe, hashtag=None):
    """row: the packets row (raw parsed); receptions: mc_rx rows with this packet's hash; radio_settings: the logging
    radio's SELF_INFO; nodes: known MeshCore ids; describe(id) -> {name}; hashtag: a channel name to try."""
    raw = row["raw"]
    frame = bytes.fromhex(raw.get("frame") or "")
    if not frame:
        raise ValueError("this packet's bytes weren't logged")
    checks, notes = [], []
    fields = []
    header = frame[0]
    route, ptype, version = header & 0x03, (header >> 2) & 0x0F, header >> 6
    pname = meshcore_packet.PAYLOAD_TYPES.get(ptype, f"TYPE{ptype}")
    fields.append(_f(0, 1, "header", f"0x{header:02x}", ["meshcore.route", "meshcore.routeType", "meshcore.payloadType",
                                                         "meshcore.payloadName", "meshcore.version"], bits=[
        {"bits": "7–6", "name": "payload version", "value": version},
        {"bits": "5–2", "name": "payload type", "value": f"{ptype} ({pname})"},
        {"bits": "1–0", "name": "route", "value": f"{route} ({meshcore_packet.ROUTES[route]})"}]))
    at = 1
    if route in (0, 3):
        t1, t2 = int.from_bytes(frame[1:3], "little"), int.from_bytes(frame[3:5], "little")
        fields.append(_f(1, 2, "transport code 1", t1, ["meshcore.transport"],
                         note="a region-limited flood: repeaters outside the region don't pass it on"))
        fields.append(_f(3, 2, "transport code 2", t2, ["meshcore.transport"]))
        at = 5
    plb = frame[at]
    layout = meshcore_packet.path_layout(plb)
    if layout is None:
        raise ValueError("the path-length byte is one the firmware rejects")
    hops, per_hop = layout
    fields.append(_f(at, 1, "path length", f"{hops} × {per_hop} byte{'s' if per_hop > 1 else ''}",
                     ["meshcore.hops", "meshcore.hopBytes"], bits=[
                         {"bits": "7–6", "name": "bytes per hop − 1", "value": per_hop - 1},
                         {"bits": "5–0", "name": "hops", "value": hops}]))
    at += 1
    for i in range(hops):
        chunk = frame[at:at + per_hop]
        if ptype == meshcore_packet.TRACE:
            snr = int.from_bytes(chunk[:1], "little", signed=True) / 4
            fields.append(_f(at, per_hop, f"hop {i + 1} SNR", f"{snr:g} dB", ["meshcore.hops"],
                             note="a trace records each hop's reception SNR × 4 here, not a key prefix"))
        else:
            who = _who(chunk.hex(), nodes)
            name = describe(who[0])["name"] if len(who) == 1 else f"{len(who)} radios share it" if who else "not a radio we know"
            fields.append(_f(at, per_hop, f"repeater {i + 1}", f"{chunk.hex()} ({name})", ["meshcore.hops"]))
        at += per_hop
    payload = frame[at:]
    fields.append(_f(at, len(payload), "payload", f"{len(payload)} bytes ({pname})", ["frame"]))

    # the payload by type
    pf, crypto = [], None
    if ptype == meshcore_packet.ADVERT and len(payload) >= 101:
        pf += [_f(0, 32, "public key", payload[:32].hex()[:16] + "…", ["advert.publicKey"],
                  note="its first 6 bytes are the radio's id: mc:" + payload[:6].hex()),
               _f(32, 4, "timestamp", _when(int.from_bytes(payload[32:36], "little")), ["advert.timestamp"],
                  note="by the radio's own clock"),
               _f(36, 64, "signature", payload[36:44].hex() + "…", [],
                  note="Ed25519 over key, timestamp and app data (not checked: Lorakeet logs what it heard)")]
        flags = payload[100]
        pf.append(_f(100, 1, "flags", f"0b{flags:08b}", ["advert.type"], bits=[
            {"bits": "7", "name": "name follows", "value": int(bool(flags & 0x80))},
            {"bits": "6–5", "name": "feature fields", "value": int(bool(flags & 0x40)) + int(bool(flags & 0x20))},
            {"bits": "4", "name": "location follows", "value": int(bool(flags & 0x10))},
            {"bits": "3–0", "name": "radio type", "value": meshcore_packet.ADV_TYPES.get(flags & 0x0F, flags & 0x0F)}]))
        p = 101
        if flags & 0x10 and len(payload) >= p + 8:
            lat = int.from_bytes(payload[p:p + 4], "little", signed=True) / 1e6
            lon = int.from_bytes(payload[p + 4:p + 8], "little", signed=True) / 1e6
            pf += [_f(p, 4, "latitude", f"{lat:.6f}", ["advert.lat"], note="int32 LE, millionths of a degree"),
                   _f(p + 4, 4, "longitude", f"{lon:.6f}", ["advert.lon"])]
            p += 8
        for mask, label in ((0x20, "feature 1"), (0x40, "feature 2")):
            if flags & mask and len(payload) >= p + 2:
                pf.append(_f(p, 2, label, payload[p:p + 2].hex()))
                p += 2
        if flags & 0x80 and len(payload) > p:
            pf.append(_f(p, len(payload) - p, "name", payload[p:].split(bytes(1), 1)[0].decode("utf-8", "replace"),
                         ["advert.name"]))
        clock = meshcore_packet.clock_status(int.from_bytes(payload[32:36], "little"), row["ts"])
        if clock == "unset":
            notes.append("The advert's timestamp says the radio's clock was never set: it counts up from a firmware seed.")
        elif clock == "wrong":
            notes.append("The advert's timestamp says the radio's clock is set, but wrong.")
    elif ptype in ADDRESSED and len(payload) >= 4:
        pf += [_f(0, 1, "destination hash", f"0x{payload[0]:02x}", note="first byte of the recipient's public key"),
               _f(1, 1, "source hash", f"0x{payload[1]:02x}", note="first byte of the sender's public key"),
               _f(2, 2, "MAC", payload[2:4].hex(), note="HMAC-SHA256 (first 2 bytes) with the pair's shared secret"),
               _f(4, len(payload) - 4, "ciphertext", f"{len(payload) - 4} bytes",
                  note="AES-128 with a key only the two radios share (from their key pair): can't be read here")]
        crypto = {"scheme": "AES-128-ECB, encrypt-then-MAC", "note": "A " + ADDRESSED[ptype] + " is encrypted with a "
                  "secret only the two radios share (worked out from their key pair), so it stays ciphertext here."}
    elif ptype == 3 and len(payload) >= 4:
        pf.append(_f(0, 4, "checksum", payload[:4].hex(), note="the acknowledged message's checksum (CRC of its contents)"))
    elif ptype == 7 and len(payload) >= 35:
        pf += [_f(0, 1, "destination hash", f"0x{payload[0]:02x}"),
               _f(1, 32, "sender public key", payload[1:9].hex() + "…",
                  note="sent in full: the recipient doesn't know the sender yet"),
               _f(33, 2, "MAC", payload[33:35].hex()), _f(35, len(payload) - 35, "ciphertext", f"{len(payload) - 35} bytes")]
    elif ptype == meshcore_packet.TRACE and len(payload) >= 9:
        pf += [_f(0, 4, "tag", payload[:4].hex(), note="matches the trace's reply to its request"),
               _f(4, 4, "auth code", payload[4:8].hex()),
               _f(8, 1, "flags", f"0b{payload[8]:08b}"),
               _f(9, len(payload) - 9, "route", " › ".join(payload[i:i + 1].hex() for i in range(9, len(payload))) or "—",
                  note="the repeaters' key prefixes the trace is sent along")]
    elif ptype in (5, 6) and len(payload) >= 3:
        ch, mac, ct = payload[0], payload[1:3], payload[3:]
        pf += [_f(0, 1, "channel hash", f"0x{ch:02x}", note="first byte of the SHA-256 of the channel's key"),
               _f(1, 2, "MAC", mac.hex(), note="HMAC-SHA256 of the ciphertext with the channel key, first 2 bytes"),
               _f(3, len(ct), "ciphertext", f"{len(ct)} bytes", note="AES-128-ECB with the channel key")]
        crypto = _channel(ch, mac, ct, ptype, hashtag)
        if crypto.get("hashtagTried") and not crypto.get("channel"):
            notes.append(f"#{crypto['hashtagTried']} isn't this message's channel: its key doesn't match the MAC.")
    else:
        pf.append(_f(0, len(payload), "contents", f"{len(payload)} bytes"))

    # the hash that recognises the packet however it travelled
    hash_in = bytes([ptype]) + (plb.to_bytes(2, "little") if ptype == meshcore_packet.TRACE else b"") + payload
    h = hashlib.sha256(hash_in).digest()[:8].hex()
    logged = (raw.get("meshcore") or {}).get("hash")
    if logged:
        checks.append({"ok": h == logged, "text": f"packet hash {h} {'matches' if h == logged else 'differs from'} "
                       "the one logged at reception"})
    if crypto and crypto.get("macOk") is not None:
        checks.append({"ok": crypto["macOk"], "text": f"MAC verified with the {crypto['channel']} key" if crypto["macOk"]
                       else "MAC doesn't match"})

    hearings = [{"ts": r["ts"], "station": r["station"], "snr": r["snr"], "rssi": r["rssi"], "route": r["route"],
                 "path": [{"prefix": x, "who": [{"id": i, "name": describe(i)["name"]} for i in _who(x, nodes)]}
                          for x in (r["path"] or "").split(",") if x] if ptype != meshcore_packet.TRACE else [],
                 "hops": r["hops"]} for r in receptions]
    return {
        "protocol": "meshcore", "rowid": row["rowid"], "ts": row["ts"], "portnum": row["portnum"],
        "snr": receptions[0]["snr"] if receptions else None, "rssi": receptions[0]["rssi"] if receptions else None,
        "radio": _radio(radio_settings, len(frame)),
        "frame": {"hex": frame.hex(), "fields": fields},
        "payload": {"hex": payload.hex(), "fields": pf, "type": pname},
        "crypto": crypto,
        "hash": {"inputHex": hash_in.hex(), "value": h, "trace": ptype == meshcore_packet.TRACE},
        "hearings": hearings, "checks": checks, "notes": notes, "json": raw,
    }


def _channel(ch, mac, ct, ptype, hashtag):
    """Try the keys anyone can know on a channel message: Public's, then a hashtag name the viewer gave."""
    tries = [("Public", PUBLIC_SECRET)]
    if hashtag:
        tries.append(("#" + hashtag.strip().lstrip("#"), hashtag_secret(hashtag)))
    out = {"scheme": "AES-128-ECB, encrypt-then-MAC (2-byte HMAC-SHA256)", "channel": None, "macOk": None,
           "hashtagTried": hashtag.strip().lstrip("#") if hashtag else None, "canTryHashtag": ptype == 5}
    for name, secret in tries:
        if channel_hash(secret) != ch or not mac_ok(secret, mac, ct):
            continue
        out.update(channel=name, macOk=True, key=f"{name} channel key ({'published' if name == 'Public' else 'from its name'})")
        if ptype != 5:
            out["note"] = "Group data: decrypted bytes aren't text, so they're not shown."
            return out
        pt = _decrypt(secret, ct)
        flags = pt[4] if len(pt) > 4 else 0
        text = pt[5:].split(bytes(1), 1)[0].decode("utf-8", "replace")
        out["plain"] = {"hex": pt.hex(), "fields": [
            _f(0, 4, "sender's timestamp", _when(int.from_bytes(pt[:4], "little")), src="decrypted", note="by the sender's clock"),
            _f(4, 1, "flags", f"0b{flags:08b}", src="decrypted", bits=[
                {"bits": "7–2", "name": "text type", "value": TXT_TYPES.get(flags >> 2, flags >> 2)},
                {"bits": "1–0", "name": "attempt", "value": flags & 3}]),
            _f(5, len(pt) - 5, "text", text, src="decrypted", note='"name: message", as MeshCore sends it; zero-padded '
               "to a whole AES block")]}
        return out
    out["note"] = ("Not the Public channel" + (f" or #{out['hashtagTried']}" if hashtag else "") +
                   ": a private channel's key isn't known here, so it stays ciphertext." +
                   (" A hashtag channel's key comes from its name: try one below." if ptype == 5 else ""))
    return out
