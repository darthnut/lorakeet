"""Node numbers across the firmware 2.8 upgrade.

Firmware 2.8 gives a radio a new node number on first boot: crc32 of its public key (2.7 and earlier derive
it from the MAC). The radio keeps its key, so the old and new numbers announce the same key. From that:

  likely 2.8  a radio whose number IS crc32(its key): 2.8 numbering. A 2.7 radio matching by chance is a
              1-in-4-billion event. Only knowable once we've heard its key (a NodeInfo).
  aliases     old number -> new number, so a radio's history stays one radio across its upgrade: the same
              key, the new number = crc32(key), and the old number never heard after the new one first was
              (both on the air at once = two radios with a copied key, which stays a "shared key" warning).

Nothing is stored: the log keeps every row under the number it arrived with, and analytics._connect maps
old numbers to new ones in its views. Worked out from every station's identity log, cached briefly.
"""
import base64
import sqlite3
import threading
import time
import zlib
from collections import defaultdict

CACHE_S = 120
OVERLAP_S = 3600  # an old number heard up to an hour after the new one's first packet (late relays) still counts

_lock = threading.Lock()
_cache = {}  # db path -> (computed_at, info)


def crc_id(b64):
    """The node number firmware 2.8 derives from a public key, as '!xxxxxxxx', or None."""
    try:
        kb = base64.b64decode(b64)
    except Exception:  # noqa: BLE001
        return None
    return f"!{zlib.crc32(kb):08x}" if len(kb) == 32 else None


def classify(latest, first_heard, last_heard):
    """latest: node -> newest public key; first/last_heard: node -> packet times.
    Returns {"aliases": {old: new}, "v28": likely-2.8 numbers, "keyed": every number whose key we know}."""
    v28 = {n for n, k in latest.items() if crc_id(k) == n}
    by_key = defaultdict(list)
    for n, k in latest.items():
        by_key[k].append(n)
    aliases = {}
    for key, nodes in by_key.items():
        new = crc_id(key)
        if len(nodes) < 2 or new not in nodes:
            continue
        start = first_heard.get(new)
        for old in nodes:
            if old == new:
                continue
            ended = last_heard.get(old)
            if ended is None or start is None or ended <= start + OVERLAP_S:
                aliases[old] = new
    return {"aliases": aliases, "v28": v28, "keyed": set(latest)}


def compute(db_path):
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        latest = {}
        for n, k in db.execute("SELECT node, public_key FROM node_info WHERE public_key IS NOT NULL "
                               "AND public_key != '' ORDER BY ts"):
            latest[n] = k
        first, last = {}, {}
        for n, a, b in db.execute("SELECT from_id, MIN(ts), MAX(ts) FROM packets GROUP BY from_id"):
            first[n], last[n] = a, b
    finally:
        db.close()
    return classify(latest, first, last)


def info(db_path):
    """{"aliases": {old: new}, "v28": {...}} for a database (cached CACHE_S; empty if unreadable)."""
    key = str(db_path)
    with _lock:
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < CACHE_S:
            return hit[1]
    try:
        out = compute(db_path)
    except sqlite3.Error:
        out = {"aliases": {}, "v28": set(), "keyed": set()}
    with _lock:
        _cache[key] = (time.time(), out)
    return out
