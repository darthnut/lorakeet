"""Packets that were probably damaged on the air, and the radios that exist only because of them.

A LoRa frame carries a 16-bit CRC, so about 1 damaged frame in 65,000 passes it. A relay that receives one
re-broadcasts the damage with a fresh CRC, so every station downstream hears the same bad copy. When the damage
lands in the header's sender field, a radio that doesn't exist shows up: one packet, never decryptable (the
sender is part of the decryption nonce), with an id a couple of bits away from a real, busy radio. Found in the
wild 2026-10-18: a busy radio's position packet, relayed with bits 0xC0 flipped in two header bytes.

Three levels, worked out on request from every station's log (cached CACHE_S), never stored: the database stays
a clean log of what was received.

  certain   the header can't be real: hop_limit above hop_start (a relay only ever lowers hop_limit).
  likely    a sender heard in at most MAX_PACKETS packets, never decrypted, with no identity, whose id is within
            NEAR_BITS bits of a busy radio. Header-only frames (16 bytes, no payload) are left alone: they're a
            different, real oddity, and nowhere near any busy radio's id.
  possible  a busy radio's packet on a public channel (key known to everyone) that won't decrypt, though that
            radio's packets normally do: its contents were probably damaged.

A sender flagged certain or likely is a phantom: radio counts, new-radio alerts and the network graph leave it out.
"""
import sqlite3
import threading
import time
from collections import Counter, defaultdict

from channels import DEFAULT_PSK, _xor

CACHE_S = 120
ACTIVE_MIN = 20      # decrypted packets: a radio busy enough to be the original of a damaged copy
MAX_PACKETS = 2      # a phantom is heard in at most this many packets
NEAR_BITS = 2        # ids this close to a busy radio's are probably it, damaged
HEADER_ONLY = 16     # a frame this long has no payload
LONGFAST = _xor(b"LongFast") ^ _xor(DEFAULT_PSK)   # 0x08, the default public channel's on-air hash

_lock = threading.Lock()
_cache = {}  # db path -> (computed_at, result)


def bits(a, b):
    """Differing bits between two Meshtastic ids ("!xxxxxxxx")."""
    return bin(int(a[1:], 16) ^ int(b[1:], 16)).count("1")


def _public_hashes(db):
    out = {LONGFAST}
    try:
        import json
        row = db.execute("SELECT value FROM settings WHERE key = 'radio_channels'").fetchone()
        for c in (json.loads(row[0]).get("channels") or []) if row else []:
            if c.get("publicKey") and c.get("hash") is not None:
                out.add(int(c["hash"]))
    except (sqlite3.Error, ValueError, TypeError):
        pass
    return out


def _mode(counter):
    return counter.most_common(1)[0][0] if counter else None


def analyse(db):
    """{"packets": {(from_id, pkt_id): flag}, "senders": {from_id: flag}} for a read connection to mesh.db."""
    q = lambda sql, *a: db.execute(sql, a).fetchall()  # noqa: E731
    rx = defaultdict(lambda: {"pkts": set(), "len": Counter(), "hs": Counter(), "relay": Counter()})
    impossible, plen = {}, {}
    for frm, pkt, hs, hl, length, relay in q("SELECT from_id, pkt_id, hop_start, hop_limit, length, relay FROM rx_hops "
                                             "WHERE from_id LIKE '!%'"):
        s = rx[frm]
        s["pkts"].add(pkt)
        if length is not None:
            s["len"][length] += 1
            plen[(frm, pkt)] = length
        if hs is not None:
            s["hs"][hs] += 1
        if relay is not None:
            s["relay"][relay] += 1
        if hs is not None and hl is not None and hl > hs:
            impossible[(frm, pkt)] = {"hopStart": hs, "hopLimit": hl}
    sent = defaultdict(set)
    decrypted = Counter()
    for frm, pkt, port in q("SELECT from_id, pkt_id, portnum FROM packets WHERE from_id LIKE '!%' AND pkt_id IS NOT NULL"):
        sent[frm].add(pkt)
        if port and port != "ENCRYPTED":
            decrypted[frm] += 1
    known = {r[0] for r in q("SELECT DISTINCT node FROM node_info")}
    active = {f for f, n in decrypted.items() if n >= ACTIVE_MIN}

    packets, senders = {}, {}
    for key, hdr in impossible.items():
        packets[key] = {"confidence": "certain", "why": (
            f"Its header says {hdr['hopLimit']} hops were left of the {hdr['hopStart']} it started with: a relay only "
            "ever lowers the hops left, so the header was damaged on the air."), **hdr}

    for frm in set(rx) | set(sent):
        if frm in active or frm in known or decrypted[frm]:
            continue
        pkts = rx[frm]["pkts"] | sent[frm]
        if not pkts or len(pkts) > MAX_PACKETS:
            continue
        lengths = rx[frm]["len"]
        if lengths and max(lengths) <= HEADER_ONLY:
            continue
        near = sorted((bits(frm, a), a) for a in active)
        near = [a for b, a in near if b <= NEAR_BITS][:1]
        sure = any((frm, p) in impossible for p in pkts)
        if not (near or sure):
            continue
        flag = {"confidence": "certain" if sure else "likely", "packets": sorted(p for p in pkts if p is not None)}
        if near:
            orig = near[0]
            flag["original"] = orig
            flag["bits"] = bits(frm, orig)
            flag["compare"] = _compare(frm, orig, rx, impossible)
        flag["why"] = _sender_why(flag, sure)
        senders[frm] = flag
        for p in flag["packets"]:
            packets[(frm, p)] = dict(packets.get((frm, p)) or {}, confidence=flag["confidence"], why=flag["why"],
                                     **{k: flag[k] for k in ("original", "bits", "compare") if k in flag})

    public = _public_hashes(db)
    marks = ",".join("?" * len(public))
    for frm, pkt in q(f"SELECT DISTINCT from_id, pkt_id FROM packets WHERE portnum = 'ENCRYPTED' "
                          f"AND channel IN ({marks}) AND from_id LIKE '!%'", *public):
        if frm not in active or (frm, pkt) in packets or plen.get((frm, pkt), HEADER_ONLY + 1) <= HEADER_ONLY:
            continue
        packets[(frm, pkt)] = {"confidence": "possible", "why": (
            "It won't decrypt with the public channel's key, though this radio's packets normally do: its contents were "
            "probably damaged on the air.")}
    return {"packets": packets, "senders": senders}


def _compare(frm, orig, rx, impossible):
    """Header fields of the damaged copy next to what the original radio usually sends."""
    out = [{"field": "sender", "got": frm, "usual": orig,
            "xor": f"{int(frm[1:], 16) ^ int(orig[1:], 16):08x}"}]
    s, o = rx[frm], rx[orig]
    hs_o = _mode(o["hs"])
    hs_s = next((h["hopStart"] for (f, _), h in impossible.items() if f == frm), _mode(s["hs"]))
    if hs_o is not None and hs_s is not None:
        out.append({"field": "hop start", "got": hs_s, "usual": hs_o,
                    "xor": f"{(hs_s << 5) ^ (hs_o << 5):02x}" if hs_s != hs_o else None})
    if s["len"] and o["len"]:
        out.append({"field": "frame length", "got": _mode(s["len"]), "usual": _mode(o["len"])})
    if s["relay"] and o["relay"]:
        out.append({"field": "last relay", "got": f"0x{_mode(s['relay']):02x}", "usual": f"0x{_mode(o['relay']):02x}"})
    return out


def _sender_why(flag, sure=False):
    if flag.get("original"):
        b = flag["bits"]
        s = (f"Heard in only {len(flag['packets'])} packet{'s' if len(flag['packets']) != 1 else ''}, never decryptable, and "
             f"its id is {b} bit{'s' if b != 1 else ''} away from {flag['original']}, a busy radio: probably a damaged "
             "copy of that radio's packet, not a radio of its own.")
        if sure:
            s += " Its header can't be real either: more hops left than it started with."
    else:
        s = "Heard only in a packet whose header can't be real: probably damaged on the air, not a radio of its own."
    return s


def compute(db_path):
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30)
    try:
        return analyse(db)
    finally:
        db.close()


def flags(db_path, max_age=CACHE_S):
    """The analysis, cached max_age seconds ({} parts if the database can't be read yet)."""
    key = str(db_path)
    with _lock:
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < max_age:
            return hit[1]
    try:
        f = compute(db_path)
    except sqlite3.Error:
        f = {"packets": {}, "senders": {}}
    with _lock:
        _cache[key] = (time.time(), f)
    return f


def phantom(flag):
    """A sender flag that means "not a radio of its own"."""
    return bool(flag) and flag.get("confidence") in ("certain", "likely")


def badge(flag):
    """What describe()/node_json carry for a flagged sender (small: it goes out with every radio)."""
    return {"confidence": flag["confidence"], "original": flag.get("original"), "why": flag["why"]} if flag else None
