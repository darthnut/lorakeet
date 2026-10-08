"""Packet browser: filtered, paged search over `packets` (first copies, decoded) or `rx_hops` (every
over-the-air reception) or `tx_log` (our transmissions), with CSV export of the full filtered set."""
import csv
import io
import time
from datetime import datetime

from analytics import ALL_STATIONS, _connect

SOURCES = {
    "packets": ["ts", "from_id", "to_id", "portnum", "channel", "hops", "snr", "rssi", "relay", "via_mqtt", "pki",
                "pkt_id", "summary"],
    "rx_hops": ["ts", "from_id", "to_id", "pkt_id", "channel", "directed", "hops", "hop_start", "hop_limit", "relay",
                "next_hop", "want_ack", "length", "encrypted", "transport", "snr", "rssi"],
    "tx_log": ["ts", "from_id", "to_id", "pkt_id", "channel", "directed", "hops", "hop_start", "hop_limit", "relay",
               "next_hop", "want_ack", "length", "encrypted", "transport", "priority"],
}
MAX_CSV_ROWS = 500_000

# How a packets-table row reached the dashboard, read from fields the radio reported in `raw`
# (computed at query time; nothing stored). LoRa arrivals carry transportMechanism + rxSnr; the radio's
# own internal reports (e.g. a "max retransmit" NAK to itself) carry neither.
ARRIVAL_SQL = """CASE
  WHEN raw IS NULL THEN 'unknown'
  WHEN json_extract(raw, '$.transportMechanism') LIKE 'TRANSPORT_LORA%' THEN 'lora'
  WHEN json_extract(raw, '$.transportMechanism') = 'TRANSPORT_MQTT' THEN 'mqtt'
  WHEN json_extract(raw, '$.transportMechanism') = 'TRANSPORT_MULTICAST_UDP' THEN 'udp'
  WHEN json_extract(raw, '$.transportMechanism') = 'TRANSPORT_API' THEN 'api'
  WHEN json_extract(raw, '$.rxSnr') IS NULL AND json_extract(raw, '$.rxRssi') IS NULL THEN 'local'
  ELSE 'unknown' END"""


def _where(source, f):
    cols = SOURCES[source]
    sql, args = ["1=1"], []
    if f.get("since"):
        sql.append("ts >= ?"); args.append(float(f["since"]))
    if f.get("until"):
        sql.append("ts < ?"); args.append(float(f["until"]))
    for key in ("from_id", "to_id"):
        if f.get(key):
            sql.append(f"{key} = ?"); args.append(f[key])
    if f.get("node"):  # either end
        sql.append("(from_id = ? OR to_id = ?)"); args += [f["node"], f["node"]]
    if f.get("portnum") and "portnum" in cols:
        sql.append("portnum = ?"); args.append(f["portnum"])
    if f.get("channel") not in (None, ""):
        sql.append("channel = ?"); args.append(int(str(f["channel"]), 0))
    for key, op in (("hopsMin", ">="), ("hopsMax", "<=")):
        if f.get(key) not in (None, ""):
            sql.append(f"hops {op} ?"); args.append(int(f[key]))
    if f.get("snrMin") not in (None, "") and "snr" in cols:
        sql.append("snr >= ?"); args.append(float(f["snrMin"]))
    if f.get("kind") == "broadcast":
        sql.append("to_id IN ('^all','!ffffffff')")
    elif f.get("kind") == "directed":
        sql.append("to_id NOT IN ('^all','!ffffffff')")
    if f.get("q") and "summary" in cols:
        sql.append("summary LIKE ?"); args.append(f"%{f['q']}%")
    if f.get("arrival") and source == "packets":
        if f["arrival"] == "air":
            sql.append(f"({ARRIVAL_SQL}) IN ('lora', 'mqtt', 'udp')")
        else:
            sql.append(f"({ARRIVAL_SQL}) = ?"); args.append(f["arrival"])
    return " AND ".join(sql), args


def search(db_path, source, f, limit=200, offset=0, station=None):
    """station '*': every station combined; rows then say which station logged them."""
    if source not in SOURCES:
        raise ValueError(f"source must be one of {', '.join(SOURCES)}")
    cols = SOURCES[source] + (["station"] if station == ALL_STATIONS else [])  # combined: which station logged it
    where, args = _where(source, f)
    db = _connect(db_path, station)
    try:
        t = time.time()
        total = db.execute(f"SELECT COUNT(*) FROM {source} WHERE {where}", args).fetchone()[0]
        extra = ", rowid" + (f", raw IS NOT NULL AS has_raw, {ARRIVAL_SQL} AS arrival" if source == "packets" else "")
        rows = [dict(r) for r in db.execute(
            f"SELECT {', '.join(cols)}{extra} FROM {source} WHERE {where} ORDER BY ts DESC LIMIT ? OFFSET ?",
            [*args, min(int(limit), 1000), max(0, int(offset))]).fetchall()]
        facets = {}
        if source == "packets":
            facets["portnum"] = [r[0] for r in db.execute(
                "SELECT portnum FROM packets GROUP BY 1 ORDER BY COUNT(*) DESC").fetchall()]
        facets["channel"] = [r[0] for r in db.execute(
            f"SELECT channel FROM {source} WHERE channel IS NOT NULL GROUP BY 1 ORDER BY COUNT(*) DESC").fetchall()]
        return {"source": source, "columns": cols, "total": total, "rows": rows, "facets": facets,
                "ms": round((time.time() - t) * 1000)}
    finally:
        db.close()


def search_csv(db_path, source, f, station=None):
    if source not in SOURCES:
        raise ValueError(f"source must be one of {', '.join(SOURCES)}")
    cols = SOURCES[source] + (["station"] if station == ALL_STATIONS else []) + (["raw"] if source == "packets" else [])
    where, args = _where(source, f)
    sel = ", ".join(cols) + (f", {ARRIVAL_SQL} AS arrival" if source == "packets" else "")
    db = _connect(db_path, station)
    try:
        rows = db.execute(f"SELECT {sel} FROM {source} WHERE {where} ORDER BY ts LIMIT ?",
                          [*args, MAX_CSV_ROWS]).fetchall()
    finally:
        db.close()
    buf = io.StringIO()
    w = csv.writer(buf)
    extra = ["arrival"] if source == "packets" else []
    w.writerow(["time_local"] + cols + extra)
    for r in rows:
        w.writerow([datetime.fromtimestamp(r["ts"]).isoformat(sep=" ", timespec="seconds")] + [r[c] for c in cols + extra])
    return buf.getvalue()
