"""Radios whose public key can't be trusted, for badges across the dashboard.

  compromised = the key is on the Meshtastic firmware's list of known low-entropy keys (weak_keys.py): anyone
                can read direct messages to the radio and impersonate it.
  shared      = several radios announce the same key (a cloned device or copied config): each can read the
                others' direct messages and impersonate them.

A radio's key is a fact about the radio, not about who heard it, so flags use every station's identity log.
A radio renumbered by firmware 2.8 (nodeids.py: its old and new numbers share the key) is one radio, not a
shared key. Nothing is stored: flags are worked out from `node_info` when asked (cached briefly), so a radio
that regenerates its key loses its badge on its own.
"""
import base64
import hashlib
import sqlite3
import threading
import time
from collections import defaultdict

import nodeids
import weak_keys

CACHE_S = 120

_lock = threading.Lock()
_cache = {}  # db path -> (computed_at, flags)


def key_bytes(b64):
    try:
        return base64.b64decode(b64)
    except Exception:  # noqa: BLE001
        return None


def is_weak(b64):
    kb = key_bytes(b64)
    return bool(kb) and len(kb) == 32 and hashlib.sha256(kb).hexdigest() in weak_keys.LOW_ENTROPY_SHA256


def renumbered(nodes, aliases):
    """One key under several numbers because a radio was renumbered by firmware 2.8 (nodeids.py), not cloned."""
    return len(nodes) > 1 and sum(n not in aliases for n in nodes) == 1


def classify(latest, aliases):
    """latest: node -> its newest public key (base64); aliases: old -> new numbers (nodeids.py).
    Returns node -> {kind, weak, with}. An old number of a renumbered radio isn't a second radio."""
    by_key = defaultdict(list)
    for nid, key in latest.items():
        by_key[key].append(nid)
    out = {}
    for key, nodes in by_key.items():
        weak = is_weak(key)
        radios = [n for n in nodes if n not in aliases]
        shared = len(radios) > 1
        if not (weak or shared):
            continue
        for nid in nodes:
            out[nid] = {"kind": "compromised" if weak else "shared", "weak": weak,
                        "with": sorted(n for n in radios if n != nid) if shared else []}
    return out


def compute(db_path):
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        latest = {}
        for nid, key in db.execute("SELECT node, public_key FROM node_info WHERE public_key IS NOT NULL "
                                   "AND public_key != '' ORDER BY ts"):
            latest[nid] = key
    finally:
        db.close()
    return classify(latest, nodeids.info(db_path)["aliases"])


def flags(db_path):
    """node -> flag for every flagged radio (cached CACHE_S; {} if the database can't be read yet)."""
    key = str(db_path)
    with _lock:
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < CACHE_S:
            return hit[1]
    try:
        f = compute(db_path)
    except sqlite3.Error:
        f = {}
    with _lock:
        _cache[key] = (time.time(), f)
    return f


def text(flag, name_of=lambda n: n):
    """One-line explanation for a badge's tooltip / a note."""
    if not flag:
        return ""
    others = ", ".join(name_of(n) for n in flag["with"])
    if flag["kind"] == "compromised":
        s = ("Compromised key: its public key is on Meshtastic's list of known weak keys, so anyone can read "
             "direct messages to it and pretend to be it. Regenerating the key on the device fixes it.")
        return s + (f" Also shared with: {others}." if others else "")
    return (f"Shared key: {len(flag['with']) + 1} radios announce the same public key ({others} and this one), so "
            "they can read each other's direct messages and pretend to be each other.")
