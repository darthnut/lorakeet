"""Aggregations over mesh.db for the Analytics page.

Everything is computed in SQL over a read-only connection (WAL lets it read while the server
writes). Time is bucketed in *local* time, since "when is the mesh busy" is a local-clock question.

Coverage matters: the log only exists while the dashboard runs. A bucket counts as "covered" if our
radio's localStats report (~every 15 min) or any packet landed in it; uncovered buckets are returned as
gaps (null), never zeros, and rates are computed over covered hours only.
"""
import json
import math
import re
import sqlite3
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta

RANGES = {"24h": 86400, "7d": 7 * 86400, "30d": 30 * 86400, "90d": 90 * 86400, "all": None}
CONVERSATION_GAP_S = 45 * 60

# Port groups for the stacked traffic chart. Order is fixed (it's the colour order), never by rank.
PORT_GROUPS = [
    ("Text", {"TEXT_MESSAGE_APP", "TEXT_MESSAGE_COMPRESSED_APP"}),
    ("Position", {"POSITION_APP", "WAYPOINT_APP"}),
    ("Telemetry", {"TELEMETRY_APP"}),
    ("Node info", {"NODEINFO_APP"}),
    ("Routing", {"ROUTING_APP", "TRACEROUTE_APP", "NEIGHBORINFO_APP"}),
    ("Encrypted", {"ENCRYPTED"}),
]
OTHER = "Other"


def measured_neighbors(db, local):
    """Nodes with a MEASURED radio link to our radio (direct 0-hop reception, traceroute hop, neighbor report)."""
    if not local:
        return set()
    return {r[0] for r in db.execute(
        "SELECT a FROM links WHERE b = ? AND source IN ('direct','traceroute','neighborinfo') "
        "UNION SELECT b FROM links WHERE a = ? AND source IN ('direct','traceroute','neighborinfo')", (local, local))}


def resolve_relay(candidates, neighbors):
    """Which node a 1-byte relay ID refers to, or None.

    The relay that handed a copy to our radio must be within our radio's range. So when several known nodes
    share the byte, the one with a measured link to our radio is it - if exactly one has such a link.
    Returns (node or None, how): how is 'unique', 'by-link', 'ambiguous' or 'unknown'.
    """
    if len(candidates) == 1:
        return candidates[0], "unique"
    if not candidates:
        return None, "unknown"
    linked = [c for c in candidates if c in neighbors]
    if len(linked) == 1:
        return linked[0], "by-link"
    return None, "ambiguous"


def port_group(port):
    for name, ports in PORT_GROUPS:
        if port in ports:
            return name
    return OTHER


# Tables whose rows were logged by a listening station (server.py tags them; see STATION_TABLES there).
STATION_TABLES = ("packets", "rx_hops", "tx_log", "messages", "traceroutes", "events", "links", "telemetry",
                  "telemetry_full", "positions", "node_info", "nodedb_snapshots")
STATION_RE = re.compile(r"^![0-9a-f]{8}$")
ALL_STATIONS = "*"   # ?station=* : every station combined
HOME = {"id": None}  # this server's own radio; server.py sets it on connect
# Listening stations' configured antenna locations, {node id: (lat, lon, alt or None)}: from this server's
# [station] location and from collectors' station reports (sync). server.py keeps it current.
STATION_LOCATIONS = {}


def station_location(nid):
    return STATION_LOCATIONS.get(nid)


def dist_m(lat1, lon1, lat2, lon2):
    """Great-circle distance in metres."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 2 * 6371000 * math.asin(math.sqrt(a))


# A station's own GPS fixes are positions rows with node = station = that radio and source = 'own'
# (server.py Mesh._track_self). A mobile station has no fixed location; these say where it was.
TRACK_MAX_AGE_S = 10 * 60  # an older fix doesn't say where a moving station is now


def station_track(db, station, since, until):
    """A station's own GPS fixes in [since, until], oldest first: [(ts, lat, lon, alt), ...]."""
    return [tuple(r) for r in db.execute(
        "SELECT ts, lat, lon, alt FROM positions WHERE node = ?1 AND station = ?1 AND source = 'own' "
        "AND ts BETWEEN ?2 AND ?3 ORDER BY ts", (station, since, until))]


def station_position(db, station, ts, max_age_s=TRACK_MAX_AGE_S):
    """Where a listening station's antenna was at `ts`: (lat, lon, alt, "fixed" | "track"), or None.
    A configured location wins (a fixed station); otherwise its latest own GPS fix at or before ts, if
    recent enough. Not interpolated: between fixes it's the last place the station was known to be."""
    loc = STATION_LOCATIONS.get(station)
    if loc:
        return (*loc[:3], "fixed")
    r = db.execute("SELECT lat, lon, alt FROM positions WHERE node = ?1 AND station = ?1 AND source = 'own' "
                   "AND ts <= ?2 AND ts >= ?3 ORDER BY ts DESC LIMIT 1", (station, ts, ts - max_age_s)).fetchone()
    return (r[0], r[1], r[2], "track") if r else None

# "One station hearing another station's own radio": internal traffic between our stations, left out of
# the combined view (each station's own view still shows it: from there the other station is just a node).
_OTHER_STATION = "(from_id IN (SELECT id FROM _stations) AND from_id != station)"


# Packets imported from a phone's data log (import_datalog.py): no packet id, so they can't be matched with
# the other stations' copies. Each station's own view shows them; the combined view leaves them out.
_IMPORTED = "(pkt_id IS NULL AND raw LIKE '{\"import\"%')"


def _first(table, partition, where="1"):
    """Combined-view TEMP view keeping the first row (by ts) of each `partition` group."""
    return (f"CREATE TEMP VIEW {table} AS SELECT * FROM (SELECT rowid AS rowid, *, ROW_NUMBER() OVER "
            f"(PARTITION BY {partition} ORDER BY ts) AS _copy FROM main.{table} WHERE {where}) WHERE _copy = 1")


# The combined view: the same packet heard by several stations counts once (its first copy, by
# (sender, packet id)); every reception stays (each is a real reception, at its station); rows derived
# from packets (positions, telemetry, identities, neighbor links) are de-duplicated on their content
# within two minutes, since they carry no packet id. Approximation: a pair straddling a 2-minute bucket
# boundary counts twice.
_COMBINED = [
    _first("packets", "CASE WHEN pkt_id IS NULL THEN rowid ELSE from_id || ':' || pkt_id END",
           f"NOT {_OTHER_STATION} AND NOT {_IMPORTED}"),
    f"CREATE TEMP VIEW rx_hops AS SELECT rowid AS rowid, * FROM main.rx_hops WHERE NOT {_OTHER_STATION}",
    _first("messages", "CASE WHEN outgoing = 1 OR pkt_id IS NULL THEN rowid ELSE from_id || ':' || pkt_id END"),
    _first("links", "a, b, source, snr, CAST(ts / 120 AS INT)"),
    _first("telemetry", "node, battery, voltage, ch_util, air_util, uptime, temperature, CAST(ts / 120 AS INT)"),
    # a station's own GPS fixes (source 'own') are each kept: they're its track, not a reception to de-duplicate
    _first("positions", "source, CASE WHEN source = 'own' THEN rowid END, node, lat, lon, alt, CAST(ts / 120 AS INT)"),
    _first("telemetry_full", "node, kind, data, CAST(ts / 120 AS INT)"),
    _first("node_info", "node, long_name, short_name, hw_model, role, public_key"),
]


def scope_local(station):
    """The radio that "our radio" means for a scope: the station itself, or this server's radio when combined."""
    return HOME["id"] if station == ALL_STATIONS else station


def is_combined(db):
    return bool(db.execute("SELECT 1 FROM sqlite_temp_master WHERE name = '_stations'").fetchone())


def station_ids(db, local):
    """The listening stations in this view (combined: every one; otherwise just the one)."""
    if is_combined(db):
        return [r[0] for r in db.execute("SELECT id FROM _stations ORDER BY id")]
    return [local] if local else []


def _connect(path, station=None):
    """Read-only connection. With a station, every station-tagged table is shadowed by a TEMP view of that
    station's rows only: unqualified table names resolve to temp first, so every query in the analytics
    modules is scoped to one listening station without being rewritten. rowid passes through (the anatomy
    links use it) and the (station, ts) indexes keep the views fast."""
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
    db.row_factory = sqlite3.Row
    if station == ALL_STATIONS:
        db.execute("CREATE TEMP TABLE _stations AS SELECT DISTINCT station AS id FROM main.packets WHERE station IS NOT NULL")
        for sql in _COMBINED:
            db.execute(sql)
    elif station:
        if not STATION_RE.match(station):
            db.close()
            raise ValueError(f"not a station id: {station!r}")
        for t in STATION_TABLES:
            db.execute(f"CREATE TEMP VIEW {t} AS SELECT rowid AS rowid, * FROM main.{t} WHERE station = '{station}'")
    return db


def stations(path):
    """Every station that has logged anything: id, first/last packet, counts. For the station picker."""
    db = _connect(path)
    try:
        return [dict(r) for r in db.execute(
            "SELECT station AS id, MIN(ts) AS first, MAX(ts) AS last, COUNT(*) AS packets FROM packets "
            "WHERE station IS NOT NULL GROUP BY station ORDER BY first")]
    finally:
        db.close()


def _bucket_starts(since, until, bucket):
    """Every bucket start (local time) from since to until, so empty/uncovered ones still exist."""
    start = datetime.fromtimestamp(since).replace(minute=0, second=0, microsecond=0)
    step = timedelta(hours=1)
    if bucket == "day":
        start = start.replace(hour=0)
        step = timedelta(days=1)
    out, t = [], start
    end = datetime.fromtimestamp(until)
    while t <= end:
        out.append(t)
        t += step
    return out


def compute(db_path, range_key, local_id, describe):
    """describe(node_id) -> {"name", "short", "hw", "role", "isBase"}"""
    if range_key not in RANGES:
        raise ValueError(f"range must be one of {', '.join(RANGES)}")
    db = _connect(db_path, local_id)
    try:
        return _compute(db, range_key, scope_local(local_id) or "", describe)
    finally:
        db.close()


def _window(db, range_key):
    """(since, until, first_ever, bucket, strftime format) for a range key."""
    if range_key not in RANGES:
        raise ValueError(f"range must be one of {', '.join(RANGES)}")
    until = time.time()
    first_ever = db.execute("SELECT MIN(ts) FROM packets").fetchone()[0] or until
    since = max(first_ever, until - RANGES[range_key]) if RANGES[range_key] else first_ever
    bucket = "hour" if until - since <= 8 * 86400 else "day"
    return since, until, first_ever, bucket, ("%Y-%m-%d %H:00" if bucket == "hour" else "%Y-%m-%d")


def _coverage(db, since, local):
    """Local-time hours ('YYYY-MM-DD HH') in which the dashboard was demonstrably logging (combined view:
    in which any station was)."""
    if is_combined(db):
        hours = {r[0] for r in db.execute(
            "SELECT strftime('%Y-%m-%d %H', ts, 'unixepoch', 'localtime') FROM telemetry_full "
            "WHERE kind='localStats' AND node IN (SELECT id FROM _stations) AND ts >= ? GROUP BY 1", (since,))}
        hours |= {r[0] for r in db.execute(
            "SELECT strftime('%Y-%m-%d %H', ts, 'unixepoch', 'localtime') FROM packets "
            "WHERE ts >= ? AND from_id NOT IN (SELECT id FROM _stations) GROUP BY 1", (since,))}
        return hours
    hours = {r[0] for r in db.execute(
        "SELECT strftime('%Y-%m-%d %H', ts, 'unixepoch', 'localtime') FROM telemetry_full "
        "WHERE kind='localStats' AND node=? AND ts >= ? GROUP BY 1", (local, since))}
    hours |= {r[0] for r in db.execute(
        "SELECT strftime('%Y-%m-%d %H', ts, 'unixepoch', 'localtime') FROM packets "
        "WHERE ts >= ? AND from_id != ? GROUP BY 1", (since, local))}
    return hours


def _covered(start, bucket, covered_hours):
    if bucket == "hour":
        return start.strftime("%Y-%m-%d %H") in covered_hours
    day = start.strftime("%Y-%m-%d")
    return any(h.startswith(day) for h in covered_hours)


def _compute(db, range_key, local, describe):
    since, until, first_ever, bucket, fmt = _window(db, range_key)
    q = lambda sql, *a: db.execute(sql, a).fetchall()  # noqa: E731
    # packets "heard": everything except our own radio's output
    P = "FROM packets WHERE ts >= ? AND from_id != ?"

    # ---- coverage (hourly, always — the heatmap and presence need hour resolution)
    covered_hours = _coverage(db, since, local)
    covered_hours_n = len(covered_hours)

    # ---- traffic over time, by port group
    counts = defaultdict(Counter)
    for r in q(f"SELECT strftime('{fmt}', ts, 'unixepoch', 'localtime') AS b, portnum, COUNT(*) AS n {P} GROUP BY 1, 2",
               since, local):
        counts[r["b"]][port_group(r["portnum"])] += r["n"]
    # Channel utilization / TX airtime: our radio's once-a-minute deviceMetrics. localStats carries them only
    # every ~15 min and only for the previous minute (47 values on 2026-10-06, swinging 1-15 %, mean 2.6 %
    # against 0.68 % per-minute), so it's used only for nodes online, which deviceMetrics doesn't have.
    util = {r["b"]: r for r in q(
        f"SELECT strftime('{fmt}', ts, 'unixepoch', 'localtime') AS b, "
        "AVG(json_extract(data, '$.channelUtilization')) AS ch, AVG(json_extract(data, '$.airUtilTx')) AS air "
        "FROM telemetry_full WHERE kind='deviceMetrics' AND node=? AND ts >= ? GROUP BY 1", local, since)}
    online = {r["b"]: r["online"] for r in q(
        f"SELECT strftime('{fmt}', ts, 'unixepoch', 'localtime') AS b, AVG(json_extract(data, '$.numOnlineNodes')) AS online "
        "FROM telemetry_full WHERE kind='localStats' AND node=? AND ts >= ? GROUP BY 1", local, since)}
    groups = [g for g, _ in PORT_GROUPS] + [OTHER]
    series = []
    for start in _bucket_starts(since, until, bucket):
        key = start.strftime(fmt)
        covered = _covered(start, bucket, covered_hours)
        c = counts.get(key, Counter())
        u = util.get(key)
        series.append({
            "t": start.timestamp(), "label": key, "covered": covered,
            "total": sum(c.values()) if covered else None,
            "byGroup": {g: c.get(g, 0) for g in groups} if covered else None,
            "chUtil": u["ch"] if u else None, "airUtil": u["air"] if u else None,
            "online": online.get(key),
        })

    # ---- weekly rhythm: average packets per *covered* hour, by local weekday x hour
    slot_hours = Counter()
    for h in covered_hours:
        d = datetime.strptime(h, "%Y-%m-%d %H")
        slot_hours[(int(d.strftime("%w")), d.hour)] += 1
    heat = []
    for r in q(f"SELECT CAST(strftime('%w', ts, 'unixepoch', 'localtime') AS INT) AS dow, "
               f"CAST(strftime('%H', ts, 'unixepoch', 'localtime') AS INT) AS hr, COUNT(*) AS n {P} GROUP BY 1, 2",
               since, local):
        hours = slot_hours.get((r["dow"], r["hr"]), 0)
        heat.append({"dow": r["dow"], "hour": r["hr"], "packets": r["n"], "hours": hours,
                     "perHour": r["n"] / hours if hours else None})
    heat_cov = [{"dow": k[0], "hour": k[1], "hours": v} for k, v in slot_hours.items()]

    # ---- nodes
    first_seen_ever = {r[0]: r[1] for r in q("SELECT from_id, MIN(ts) FROM packets GROUP BY from_id")}
    groups_by_node = defaultdict(Counter)
    for r in q(f"SELECT from_id, portnum, COUNT(*) {P} GROUP BY 1, 2", since, local):
        groups_by_node[r[0]][port_group(r[1])] += r[2]
    nodes = []
    for r in q(f"""SELECT from_id, COUNT(*) AS n, MIN(ts) AS first, MAX(ts) AS last,
                 COUNT(DISTINCT strftime('%Y-%m-%d %H', ts, 'unixepoch', 'localtime')) AS hours,
                 AVG(CASE WHEN hops = 0 THEN snr END) AS snr0, AVG(CASE WHEN hops = 0 THEN rssi END) AS rssi0,
                 SUM(hops = 0) AS direct, MIN(hops) AS min_hops, AVG(hops) AS avg_hops, MAX(hops) AS max_hops,
                 SUM(via_mqtt) AS mqtt, GROUP_CONCAT(DISTINCT portnum) AS ports
                 {P} GROUP BY from_id""", since, local):
        d = describe(r["from_id"])
        nodes.append({
            "id": r["from_id"], **d, "packets": r["n"], "byGroup": dict(groups_by_node[r["from_id"]]),
            "first": r["first"], "last": r["last"], "firstEver": first_seen_ever.get(r["from_id"]),
            "hoursSeen": r["hours"], "presence": r["hours"] / covered_hours_n if covered_hours_n else None,
            "perDay": r["n"] / (covered_hours_n / 24) if covered_hours_n else None,
            "snrDirect": r["snr0"], "rssiDirect": r["rssi0"], "direct": r["direct"] or 0,
            "minHops": r["min_hops"], "avgHops": r["avg_hops"], "maxHops": r["max_hops"], "mqtt": r["mqtt"] or 0,
        })
    nodes.sort(key=lambda n: -n["packets"])

    # ---- distributions
    ports = [{"port": r[0], "group": port_group(r[0]), "count": r[1]}
             for r in q(f"SELECT portnum, COUNT(*) {P} GROUP BY 1 ORDER BY 2 DESC", since, local)]
    hops = [{"hops": r[0], "count": r[1]}
            for r in q(f"SELECT hops, COUNT(*) {P} GROUP BY 1 ORDER BY 1", since, local)]

    # relays: packets carry only the low byte of the relayer's node number; resolve when unambiguous
    # A relayer may never have sent a packet of its own that we logged; traceroutes and links still name it.
    known_ids = set(first_seen_ever) | {r[0] for r in q("SELECT DISTINCT node FROM node_info")}
    known_ids |= {r[0] for r in q("SELECT a FROM links UNION SELECT b FROM links")}
    neighbors = measured_neighbors(db, local)
    relays = []
    for r in q(f"SELECT relay, COUNT(*) {P} AND hops > 0 AND relay IS NOT NULL GROUP BY 1 ORDER BY 2 DESC",
               since, local):
        hexb = f"{r[0]:02x}"
        cands = sorted(i for i in known_ids if i.endswith(hexb) and i != local)
        chosen, how = resolve_relay(cands, neighbors)
        # list the resolved node first (alone) so the UI shows it; keep the full set for the tooltip
        relays.append({"byte": hexb, "count": r[1], "resolvedBy": how, "allCandidates": len(cands),
                       "candidates": [{"id": c, **describe(c)} for c in ([chosen] if chosen else cands)]})

    # ---- conversations
    msgs = q("""SELECT m.ts, m.from_id, m.to_id, m.text, m.outgoing, m.encrypted, m.pkt_id, m.status,
                       json_extract(p.raw, '$.decoded.replyId') AS reply_id,
                       json_extract(p.raw, '$.decoded.emoji') AS emoji,
                       p.hops AS hops, p.snr AS snr
                FROM messages m
                LEFT JOIN packets p ON p.pkt_id = m.pkt_id AND p.from_id = m.from_id
                WHERE m.ts >= ? ORDER BY m.ts""", since)
    by_pkt = {}
    threads = []
    open_thread = {}
    talkers = Counter()
    for m in msgs:
        broadcast = m["to_id"] in ("^all", "!ffffffff") or not m["to_id"]
        key = "^all" if broadcast else "|".join(sorted((m["from_id"], m["to_id"])))
        th = open_thread.get(key)
        if th is None or m["ts"] - th["end"] > CONVERSATION_GAP_S:
            th = {"key": key, "kind": "channel" if broadcast else "direct", "start": m["ts"], "end": m["ts"],
                  "messages": [], "people": Counter()}
            threads.append(th)
            open_thread[key] = th
        sender = m["from_id"]
        item = {"ts": m["ts"], "from": sender, "name": describe(sender)["name"], "text": m["text"],
                "outgoing": bool(m["outgoing"]), "encrypted": bool(m["encrypted"]), "status": m["status"],
                "hops": m["hops"], "snr": m["snr"], "emoji": bool(m["emoji"])}
        if m["reply_id"] and m["reply_id"] in by_pkt:
            ref = by_pkt[m["reply_id"]]
            item["replyTo"] = {"name": ref["name"], "text": ref["text"][:80]}
        if m["pkt_id"]:
            by_pkt[m["pkt_id"]] = item
        th["messages"].append(item)
        th["end"] = m["ts"]
        th["people"][sender] += 1
        if not m["encrypted"]:
            talkers[sender] += 1
    conversations = []
    for th in reversed(threads):  # newest first
        conversations.append({
            "kind": th["kind"], "start": th["start"], "end": th["end"], "count": len(th["messages"]),
            "people": [{"id": i, "name": describe(i)["name"], "count": c} for i, c in th["people"].most_common()],
            "messages": th["messages"][-300:],
        })

    # ---- headline numbers
    total = sum(n["packets"] for n in nodes)
    ch = [s["chUtil"] for s in series if s["chUtil"] is not None]
    new_nodes = [n for n in nodes if n["firstEver"] and n["firstEver"] >= since]
    kpis = {
        "packets": total, "nodes": len(nodes), "newNodes": len(new_nodes),
        "messages": sum(1 for m in msgs if not m["encrypted"]),
        "conversations": len(conversations),
        "chUtilAvg": sum(ch) / len(ch) if ch else None,
        "perHour": total / covered_hours_n if covered_hours_n else None,
        "coveredHours": covered_hours_n, "spanHours": (until - since) / 3600,
    }
    return {
        "range": range_key, "since": since, "until": until, "bucket": bucket, "firstEver": first_ever,
        "groups": groups, "kpis": kpis, "series": series, "heat": heat, "heatCoverage": heat_cov,
        "nodes": nodes, "ports": ports, "hops": hops, "relays": relays,
        "talkers": [{"id": i, "name": describe(i)["name"], "count": c} for i, c in talkers.most_common(15)],
        "newNodes": [{"id": n["id"], "name": n["name"], "firstEver": n["firstEver"]} for n in
                     sorted(new_nodes, key=lambda n: -n["firstEver"])],
        "conversations": conversations[:100],
        "privacy": _privacy(db, since, until, bucket, fmt, local, covered_hours),
    }


# Fallback when the radio's channel list hasn't been read yet: default LongFast (hash 0x08).
FALLBACK_CHANNELS = [{"index": 0, "role": "PRIMARY", "name": "LongFast", "hash": 0x08, "encrypted": True, "publicKey": True}]


def _readable_channels(db):
    """Channels our radio can decrypt, as recorded by the server from the radio's config on connect."""
    try:
        r = db.execute("SELECT value FROM settings WHERE key = 'radio_channels'").fetchone()
    except sqlite3.OperationalError:
        r = None
    if r:
        v = json.loads(r[0])
        return v["channels"], "radio", v.get("ts")
    return FALLBACK_CHANNELS, "fallback", None


def _privacy_category(channel, directed, readable):
    if channel in readable:
        return f"rd-{channel:02x}" + ("-d" if directed else "")
    if channel == 0 and directed:
        return "dm"  # end-to-end (PKI) encrypted direct messages carry channel 0
    return f"ch-{channel:02x}" if channel is not None else "unknown"


def _privacy(db, since, until, bucket, fmt, local, covered_hours):
    """Readable vs private traffic, in aggregate, from every over-the-air reception (rx_hops).

    Counts distinct packets (sender + packet id), not copies, so a busy relay doesn't inflate a channel.
    """
    q = lambda sql, *a: db.execute(sql, a).fetchall()  # noqa: E731
    chans, chan_source, chan_ts = _readable_channels(db)
    readable = {c["hash"]: c for c in chans}
    mining_since = q("SELECT MIN(ts) FROM rx_hops")[0][0]
    counts = defaultdict(Counter)
    for r in q(f"""SELECT strftime('{fmt}', ts, 'unixepoch', 'localtime') AS b, channel, directed,
                          COUNT(DISTINCT from_id || ':' || pkt_id) AS n
                   FROM rx_hops WHERE ts >= ? AND from_id != ? GROUP BY 1, 2, 3""", since, local):
        counts[r["b"]][_privacy_category(r["channel"], r["directed"], readable)] += r["n"]
    cats = Counter()
    for c in counts.values():
        cats.update(c)
    readable_keys = [k for c in chans for k in (f"rd-{c['hash']:02x}", f"rd-{c['hash']:02x}-d") if k in cats]
    private_channels = sorted(k for k in cats if k.startswith("ch-") or k == "unknown")
    groups = readable_keys + (["dm"] if "dm" in cats else []) + private_channels
    series = []
    for start in _bucket_starts(since, until, bucket):
        key = start.strftime(fmt)
        step = timedelta(hours=1) if bucket == "hour" else timedelta(days=1)
        # a bucket has data only if the dashboard was logging AND reception mining had started
        covered = _covered(start, bucket, covered_hours) and mining_since is not None \
            and (start + step).timestamp() > mining_since
        c = counts.get(key, Counter())
        series.append({"t": start.timestamp(), "covered": covered,
                       "total": sum(c.values()) if covered else None,
                       "byGroup": {g: c.get(g, 0) for g in groups} if covered else None})
    channels = []
    for r in q("""SELECT channel, COUNT(DISTINCT from_id || ':' || pkt_id) AS packets, COUNT(*) AS copies,
                         SUM(directed = 0) AS bcast, SUM(directed = 1) AS direct,
                         COUNT(DISTINCT from_id) AS senders, MIN(ts) AS first, MAX(ts) AS last
                  FROM rx_hops WHERE ts >= ? AND from_id != ? GROUP BY channel ORDER BY packets DESC""", since, local):
        info = readable.get(r["channel"])
        channels.append({**dict(r), "readable": info is not None, "name": info["name"] if info else None,
                         "publicKey": info["publicKey"] if info else None})
    total = sum(cats.values())
    unreadable = sum(v for k, v in cats.items() if not k.startswith("rd-"))
    return {"groups": groups, "series": series, "channels": channels, "miningSince": mining_since,
            "readable": chans, "readableSource": chan_source, "readableTs": chan_ts,
            "totals": dict(cats), "total": total, "unreadable": unreadable,
            "privateChannels": len([k for k in private_channels if k != "unknown"])}
