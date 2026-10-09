"""Scheduled test messages on a private channel, sent through a running Lorakeet server (so each one is
logged like any message sent from the dashboard). Numbered and timestamped, so a gap shows exactly which
message didn't arrive; optionally with the station's latest own GPS fix.

  python tools/beacon.py --name Desk --start 2026-10-07T22:30 --until 2026-10-08T20:00 [--every 30]
                         [--offset 0] [--channel Lorakeet] [--gps] [--url http://127.0.0.1:5190] [--dry-run]

Message n goes out at start + offset + (n-1) * every minutes, so a restart with the same --start keeps the
numbering. TRANSMITS: refuses a channel that uses a public key (LongFast and friends), stops at --until.
"""
import argparse
import json
import sqlite3
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def api(url, path, body=None):
    req = urllib.request.Request(url + path, data=None if body is None else json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"} if body is not None else {})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def private_channel(url, name):
    """The radio's slot for the named channel; refuses a channel anyone can read."""
    for c in api(url, "/api/channels")["channels"]:
        if (c.get("name") or "").lower() == name.lower():
            if c.get("publicKey") or not c.get("encrypted"):
                raise SystemExit(f"refusing: channel {name!r} uses a public key, so anyone could read it")
            return c["index"]
    raise SystemExit(f"this radio has no channel named {name!r}")


def own_fix(station):
    """The station radio's latest exact own GPS fix: (lat, lon, age in seconds) or None."""
    from config import CFG
    db = sqlite3.connect(f"file:{Path(CFG['storage']['data_dir']) / 'mesh.db'}?mode=ro", uri=True)
    try:
        r = db.execute("SELECT lat, lon, ts FROM positions WHERE node = ?1 AND station = ?1 AND source = 'own' "
                       "AND (precision_bits IS NULL OR precision_bits >= 32) ORDER BY ts DESC LIMIT 1", (station,)).fetchone()
    finally:
        db.close()
    return (r[0], r[1], time.time() - r[2]) if r else None


def text(n, name, station, gps):
    s = f"Lorakeet test #{n} · {datetime.now():%H:%M} · from {name}"
    if gps:
        fix = own_fix(station)
        if not fix:
            s += " · no GPS fix"
        else:
            lat, lon, age = fix
            s += f" · {lat:.5f},{lon:.5f}" + (f" ({age / 60:.0f} min old)" if age > 120 else "")
    return s


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--name", required=True, help="sender label in the message, e.g. Desk or Pi")
    ap.add_argument("--start", required=True, help="first slot, local time, e.g. 2026-10-07T22:30")
    ap.add_argument("--until", required=True, help="stop after this local time")
    ap.add_argument("--every", type=float, default=30, help="minutes between this sender's messages")
    ap.add_argument("--offset", type=float, default=0, help="minutes after --start for this sender's first slot")
    ap.add_argument("--channel", default="Lorakeet")
    ap.add_argument("--gps", action="store_true", help="include the station's latest own GPS fix")
    ap.add_argument("--url", default="http://127.0.0.1:5190")
    ap.add_argument("--dry-run", action="store_true", help="print the next message instead of sending")
    a = ap.parse_args()
    url = a.url.rstrip("/")
    start = datetime.fromisoformat(a.start).timestamp() + a.offset * 60
    until, every = datetime.fromisoformat(a.until).timestamp(), a.every * 60
    chan = private_channel(url, a.channel)
    station = api(url, "/api/state")["status"]["localId"] if a.gps else None
    if a.dry_run:
        print(f"channel {a.channel} = slot {chan}: {text(1, a.name, station, a.gps)}")
        return
    print(f"{a.name}: every {a.every:g} min on {a.channel} (slot {chan}) until {a.until}", flush=True)
    while True:
        n = max(1, int((time.time() - start) // every) + 2 if time.time() >= start else 1)
        due = start + (n - 1) * every
        if due > until:
            print("done", flush=True)
            return
        time.sleep(max(0, due - time.time()))
        msg = text(n, a.name, station, a.gps)
        try:
            api(url, "/api/send", {"text": msg, "channel": chan})
            print(f"{datetime.now():%H:%M:%S} sent: {msg}", flush=True)
        except Exception as e:  # noqa: BLE001 - radio briefly away (reconnect): skip this slot, keep the schedule
            print(f"{datetime.now():%H:%M:%S} #{n} not sent: {e}", flush=True)
        time.sleep(1)


if __name__ == "__main__":
    main()
