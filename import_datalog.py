"""Import a Meshtastic Android app data log (Settings > Export all packets; "Meshtastic_datalog_*.csv") as
one listening station's packets, or its node database (Export node database; "Meshtastic_nodedb_*.json").

The log is what the phone received from its radio while connected: one row per packet, with the sender,
the last hop's SNR, hop start/limit and relay byte, and the port (the text, for messages). No packet ids,
RSSI, channel or recipient, so imported rows are lower fidelity than a station's own log:
  - packets: one row per packet heard from another node, station = that radio, pkt_id NULL,
    raw = {"import": "android-datalog", ...}. The combined (all stations) view leaves them out (without
    a packet id they can't be de-duplicated against the other stations); pick the station to see them.
  - links: a 0-hop reception is a real RF link, as live logging records it.
  - messages: text messages, the radio's own (outgoing = 1) and others'.
Rows from the radio itself that aren't text (its telemetry to the phone, admin, routing) are skipped.
Positions are skipped too: the log's coordinates come from the phone's node list, rounded to the
channel's precision. Re-importing (or an overlapping export) is safe: every row has a stable import key
(src_rowid), and existing keys are ignored.

A node database export becomes one nodedb_snapshots row (reshaped like the library's node dict that live
snapshots store) plus a node_info row per node the radio has a real identity for (hwModel present; the app
invents "Meshtastic 1234" names for the rest), stamped with when the radio last heard that node.

    python import_datalog.py <file.csv | file.json> [--station !1234abcd] [--dry-run]
"""
import argparse
import collections
import csv
import hashlib
import json
import sys
import time
from datetime import datetime
from pathlib import Path

sys.argv, _args = sys.argv[:1], sys.argv[1:]  # server.py parses argv on import
import server  # noqa: E402
from config import CFG  # noqa: E402

SOURCE = "android-datalog"


def key(*parts):
    """Stable 60-bit import key for a row (src_rowid): the same packet in another export gets the same key."""
    return int(hashlib.sha1("|".join(parts).encode()).hexdigest()[:15], 16)


def load(path, station=None):
    rows = list(csv.DictReader(open(path, encoding="utf-8-sig")))
    if not rows or "hop start" not in rows[0]:
        raise ValueError("not a Meshtastic datalog export (no 'hop start' column)")
    if station is None:  # the radio itself: the sender of the local-only rows (hop start 0, at 0 m)
        own = collections.Counter(r["from"] for r in rows if r["distance(m)"] == "0")
        if not own:
            raise ValueError("can't tell which radio this log is from; pass --station")
        station = f"!{int(own.most_common(1)[0][0]):08x}"
    return station, rows


def convert(station, rows, file_name):
    """CSV rows -> {table: [row dicts]} ready for INSERT OR IGNORE."""
    out = collections.defaultdict(list)
    for r in rows:
        ts = datetime.strptime(f'{r["date"]} {r["time"]}', "%Y-%m-%d %H:%M:%S").timestamp()  # local time
        frm = f"!{int(r['from']):08x}"
        payload = r["payload"]
        port = payload[1:-1] if payload.startswith("<") and payload.endswith(">") else "TEXT_MESSAGE_APP"
        text = payload if port == "TEXT_MESSAGE_APP" else None
        hs, hl = int(r["hop start"] or 0), int(r["hop limit"] or 0)
        hops = hs - hl if hs > 0 else None
        snr = float(r["rx snr"]) if r["rx snr"] else None
        relay = int(r["relay node"], 16) if r["relay node"] else None
        k = key(r["date"], r["time"], r["from"], payload, r["hop start"], r["hop limit"], r["rx snr"], r["relay node"])
        common = dict(station=station, src_rowid=k)
        if frm == station:
            if text is not None:
                out["messages"].append(dict(ts=ts, from_id=frm, to_id=None, channel=None, text=text, outgoing=1, **common))
            continue
        raw = {"import": SOURCE, "file": file_name, "from": frm, "name": r["sender name"], "port": port,
               "snr": snr, "hopStart": hs, "hopLimit": hl, "relay": relay}
        out["packets"].append(dict(ts=ts, from_id=frm, to_id=None, portnum=port, channel=None, snr=snr, rssi=None,
                                   hops=hops, via_mqtt=0, summary=(text or "")[:120] or None, relay=relay,
                                   pkt_id=None, pki=0, raw=json.dumps(raw), **common))
        if hops == 0:
            out["links"].append(dict(ts=ts, a=frm, b=station, snr=snr, source="direct", **common))
        if text is not None:
            out["messages"].append(dict(ts=ts, from_id=frm, to_id=None, channel=None, text=text, outgoing=0, **common))
    return out


def convert_nodedb(path, file_name):
    """Node database export -> (station, {table: [row dicts]})."""
    d = json.load(open(path, encoding="utf-8-sig"))
    if not isinstance(d, dict) or "nodes" not in d or "myNodeNum" not in d:
        raise ValueError("not a Meshtastic node database export (no nodes / myNodeNum)")
    station = f"!{int(d['myNodeNum']):08x}"
    exported = datetime.fromisoformat(d["exportedAt"].replace("Z", "+00:00")).timestamp()
    user_keys = ("id", "longName", "shortName", "hwModel", "role", "publicKey", "isUnmessagable", "isLicensed")
    snap, out = {}, collections.defaultdict(list)
    for n in d["nodes"]:
        nid = n.get("id") or f"!{int(n['num']):08x}"
        user = {k: n[k] for k in user_keys if k in n}
        snap[nid] = {"num": n["num"], "user": user, **{k: v for k, v in n.items() if k not in user_keys and k != "num"}}
        if n.get("hwModel") and nid != station:
            out["node_info"].append(dict(
                ts=n.get("lastHeard") or exported, node=nid, long_name=n.get("longName"), short_name=n.get("shortName"),
                hw_model=n.get("hwModel"), role=n.get("role", "CLIENT"), public_key=n.get("publicKey"),
                is_licensed=int(bool(n.get("isLicensed"))), data=json.dumps({**user, "import": SOURCE, "file": file_name}),
                station=station, src_rowid=key("nodeinfo", nid, *(str(n.get(k)) for k in user_keys[1:6]))))
    out["nodedb_snapshots"].append(dict(ts=exported, data=json.dumps(snap), station=station,
                                        src_rowid=key("nodedb", station, d["exportedAt"])))
    return station, out, d


def store_rows(store, tables):
    stored = {}
    with store.lock:
        for table, rows in tables.items():
            cols = list(rows[0])
            cur = store.db.executemany(f"INSERT OR IGNORE INTO {table} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                                       [[r[c] for c in cols] for r in rows])
            stored[table] = cur.rowcount
        store.db.commit()
    return stored


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("csv")
    ap.add_argument("--station", help="the radio the log is from (default: worked out from the log)")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(_args)
    if a.csv.lower().endswith(".json"):
        station, tables, d = convert_nodedb(a.csv, Path(a.csv).name)
        n = len(d["nodes"])
        print(f"{Path(a.csv).name}: radio {station}, node database of {n} nodes, exported {d['exportedAt']}")
    else:
        station, rows = load(a.csv, a.station)
        tables = convert(station, rows, Path(a.csv).name)
        n = len(rows)
        span = f'{rows[0]["date"]} {rows[0]["time"]} to {rows[-1]["date"]} {rows[-1]["time"]}'
        print(f"{Path(a.csv).name}: radio {station}, {n} rows, {span}")
    print("  to import: " + ", ".join(f"{t} {len(r)}" for t, r in tables.items()))
    if a.dry_run:
        return
    store = server.Store(Path(CFG["storage"]["data_dir"]) / "mesh.db")
    stored = store_rows(store, tables)
    print("  stored (new): " + ", ".join(f"{t} {n}" for t, n in stored.items()))
    store.execute("INSERT INTO events (ts, kind, detail, station) VALUES (?, 'datalog_import', ?, ?)", time.time(),
                  json.dumps({"file": Path(a.csv).name, "rows": n, "stored": stored}), station)


if __name__ == "__main__":
    main()
