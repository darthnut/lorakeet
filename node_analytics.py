"""Per-node analytics for the Analytics page's node view, plus CSV export.

Uses the same window, bucketing and coverage rules as analytics.py, so "gap, not zero" holds here too.
"""
import csv
import io
import json
from collections import Counter, defaultdict
from datetime import datetime, timedelta

from analytics import (OTHER, PORT_GROUPS, _bucket_starts, _connect, alias_map, _coverage, _covered, _window, measured_neighbors,
                       port_group, resolve_relay, scope_local)


def compute_node(db_path, nid, range_key, local_id, describe):
    db = _connect(db_path, local_id)
    local_id = scope_local(local_id)
    try:
        return _compute_node(db, alias_map(db).get(nid, nid), range_key, local_id or "", describe)  # a pre-2.8 number: the radio's page
    finally:
        db.close()


def _relay_candidates(db, byte, local):
    hexb = f"{byte:02x}"
    ids = {r[0] for r in db.execute(
        "SELECT from_id FROM packets UNION SELECT node FROM node_info UNION SELECT a FROM links UNION SELECT b FROM links")}
    ids |= set(alias_map(db))  # pre-2.8 numbers: relay bytes logged before an upgrade end in them
    return sorted(i for i in ids if i and i.endswith(hexb) and i != local)


def _longest_silence(stamps, until, covered_hours):
    """Longest gap between packets during which the dashboard was logging every hour.

    Gaps that overlap an outage are skipped: we can't tell silence from not listening.
    """
    longest = None
    edges = stamps + [until]
    for a, b in zip(edges, edges[1:]):
        if b - a < 3600:
            continue
        h = datetime.fromtimestamp(a).replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        end = datetime.fromtimestamp(b).replace(minute=0, second=0, microsecond=0)
        ok = True
        while h < end:
            if h.strftime("%Y-%m-%d %H") not in covered_hours:
                ok = False
                break
            h += timedelta(hours=1)
        if ok and (longest is None or b - a > longest["seconds"]):
            longest = {"from": a, "to": b, "seconds": b - a, "ongoing": b == until}
    return longest


def _compute_node(db, nid, range_key, local, describe):
    since, until, _first_ever, bucket, fmt = _window(db, range_key)
    q = lambda sql, *a: db.execute(sql, a).fetchall()  # noqa: E731
    covered_hours = _coverage(db, since, local)
    covered_n = len(covered_hours)
    N = "FROM packets WHERE from_id = ? AND ts >= ?"

    ever = q("SELECT MIN(ts), MAX(ts), COUNT(*) FROM packets WHERE from_id = ?", nid)[0]
    # A pure relay may never send a packet of its own we log; links and identity still describe it.
    if not ever[2] and not q("SELECT 1 FROM node_info WHERE node = ? LIMIT 1", nid) \
            and not q("SELECT 1 FROM links WHERE a = ? OR b = ? LIMIT 1", nid, nid):
        raise ValueError(f"no data for node {nid}")

    # ---- activity + per-bucket signal and telemetry
    counts = defaultdict(Counter)
    for r in q(f"SELECT strftime('{fmt}', ts, 'unixepoch', 'localtime') AS b, portnum, COUNT(*) AS n {N} GROUP BY 1, 2",
               nid, since):
        counts[r["b"]][port_group(r["portnum"])] += r["n"]
    sig = {r["b"]: r for r in q(
        f"SELECT strftime('{fmt}', ts, 'unixepoch', 'localtime') AS b, AVG(snr) AS snr, AVG(rssi) AS rssi "
        f"{N} AND hops = 0 GROUP BY 1", nid, since)}
    tel = {r["b"]: r for r in q(
        f"SELECT strftime('{fmt}', ts, 'unixepoch', 'localtime') AS b, AVG(battery) AS battery, AVG(voltage) AS voltage, "
        "AVG(ch_util) AS ch, AVG(air_util) AS air, AVG(temperature) AS temp "
        "FROM telemetry WHERE node = ? AND ts >= ? GROUP BY 1", nid, since)}
    groups = [g for g, _ in PORT_GROUPS] + [OTHER]
    series = []
    for start in _bucket_starts(since, until, bucket):
        key = start.strftime(fmt)
        covered = _covered(start, bucket, covered_hours)
        c, s, t = counts.get(key, Counter()), sig.get(key), tel.get(key)
        series.append({
            "t": start.timestamp(), "covered": covered,
            "total": sum(c.values()) if covered else None,
            "byGroup": {g: c.get(g, 0) for g in groups} if covered else None,
            "snr": s["snr"] if s else None, "rssi": s["rssi"] if s else None,
            "battery": t["battery"] if t else None, "voltage": t["voltage"] if t else None,
            "chUtil": t["ch"] if t else None, "airUtil": t["air"] if t else None, "temp": t["temp"] if t else None,
        })

    # ---- daily rhythm: packets per covered hour, by local hour of day
    hour_cov = Counter(int(h[-2:]) for h in covered_hours)
    by_hour = {r[0]: r[1] for r in q(
        f"SELECT CAST(strftime('%H', ts, 'unixepoch', 'localtime') AS INT), COUNT(*) {N} GROUP BY 1", nid, since)}
    rhythm = [{"hour": h, "packets": by_hour.get(h, 0), "hours": hour_cov.get(h, 0),
               "perHour": by_hour.get(h, 0) / hour_cov[h] if hour_cov.get(h) else None} for h in range(24)]

    # ---- presence, rate, silence
    stamps = [r[0] for r in q(f"SELECT ts {N} ORDER BY ts", nid, since)]
    heard_hours = {datetime.fromtimestamp(t).strftime("%Y-%m-%d %H") for t in stamps}

    # ---- reach
    hops = [{"hops": r[0], "count": r[1]} for r in q(f"SELECT hops, COUNT(*) {N} GROUP BY 1 ORDER BY 1", nid, since)]
    relays = []
    for r in q(f"SELECT relay, COUNT(*) {N} AND hops > 0 AND relay IS NOT NULL GROUP BY 1 ORDER BY 2 DESC", nid, since):
        cands = _relay_candidates(db, r[0], local)
        chosen, how = resolve_relay(cands, measured_neighbors(db, local), alias_map(db))
        relays.append({"byte": f"{r[0]:02x}", "count": r[1], "resolvedBy": how,
                       "candidates": [{"id": c, **describe(c)} for c in ([chosen] if chosen else cands)]})
    ports = [{"port": r[0], "group": port_group(r[0]), "count": r[1]}
             for r in q(f"SELECT portnum, COUNT(*) {N} GROUP BY 1 ORDER BY 2 DESC", nid, since)]
    neighbors = []
    for r in q("""SELECT CASE WHEN a = ? THEN b ELSE a END AS other, source, COUNT(*) AS n, AVG(snr) AS snr, MAX(ts) AS last
                  FROM links WHERE (a = ? OR b = ?) AND ts >= ? GROUP BY 1, 2 ORDER BY n DESC""", nid, nid, nid, since):
        neighbors.append({"id": r["other"], **describe(r["other"]), "source": r["source"], "count": r["n"],
                          "snr": r["snr"], "last": r["last"]})

    # ---- movement, identity, telemetry snapshots, messages, traceroutes
    positions = [dict(r) for r in q("SELECT ts, lat, lon, alt, precision_bits FROM positions WHERE node = ? AND ts >= ? "
                                    "ORDER BY ts", nid, since)]
    identity = [dict(r) for r in q("SELECT ts, long_name, short_name, hw_model, role, public_key FROM node_info "
                                   "WHERE node = ? ORDER BY ts", nid)]
    latest_tel = {}
    for r in q("SELECT kind, data, MAX(ts) AS ts FROM telemetry_full WHERE node = ? GROUP BY kind", nid):
        try:
            latest_tel[r["kind"]] = {"ts": r["ts"], "data": json.loads(r["data"])}
        except ValueError:
            pass
    messages = [dict(r) for r in q("""SELECT ts, from_id, to_id, text, outgoing, encrypted FROM messages
                                     WHERE (from_id = ? OR to_id = ?) AND ts >= ? ORDER BY ts DESC LIMIT 100""",
                                   nid, nid, since)]
    for m in messages:
        m["name"] = describe(m["from_id"])["name"]
        m["direct"] = m["to_id"] not in ("^all", "!ffffffff")
    traces = [dict(r) for r in q("SELECT ts, status, forward, back, done_ts FROM traceroutes WHERE target = ? "
                                 "ORDER BY ts DESC LIMIT 10", nid)]
    for t in traces:
        for k in ("forward", "back"):
            path = json.loads(t[k]) if t[k] else None
            t[k] = [{**h, "name": describe(h["id"])["name"] if h.get("id") else "unknown relay"}
                    for h in path] if path else None

    # Packets this node relayed to us. Packets name the relayer by one byte only, so this is exact only
    # when no other known node shares that last byte; report the ambiguity rather than guess.
    byte = int(nid[-2:], 16) if len(nid) == 9 else None
    relayed = q("SELECT COUNT(*) FROM packets WHERE relay = ? AND hops > 0 AND from_id != ? AND ts >= ?",
                byte, nid, since)[0][0] if byte is not None else 0
    # "relayed to us" is exact when this node is the only one with the byte, or the only one with a measured
    # link to our radio among those sharing it (a relay must be in our radio's range)
    all_with_byte = _relay_candidates(db, byte, local) if byte is not None else []
    chosen, _how = resolve_relay(all_with_byte, measured_neighbors(db, local), alias_map(db)) if all_with_byte else (None, None)
    sharing = [] if chosen == nid else [c for c in all_with_byte if c != nid]

    return {
        "id": nid, **describe(nid),
        "keyFlagWith": [{"id": o, "name": describe(o)["name"]} for o in (describe(nid).get("keyFlag") or {}).get("with", [])],
        "range": range_key, "since": since, "until": until, "bucket": bucket,
        "groups": groups, "series": series, "rhythm": rhythm,
        "stats": {
            "packets": len(stamps), "firstEver": ever[0], "lastEver": ever[1], "packetsEver": ever[2],
            "coveredHours": covered_n, "hoursHeard": len(heard_hours),
            "presence": len(heard_hours) / covered_n if covered_n else None,
            "perDay": len(stamps) / (covered_n / 24) if covered_n else None,
            "longestSilence": _longest_silence(stamps, until, covered_hours),
            "direct": sum(h["count"] for h in hops if h["hops"] == 0),
            "relayedToUs": relayed, "relayByteShared": [{"id": c, **describe(c)} for c in sharing],
        },
        "hops": hops, "relays": relays, "ports": ports, "neighbors": neighbors, "positions": positions,
        "identity": identity, "latestTelemetry": latest_tel, "messages": messages, "traceroutes": traces,
    }


def node_csv(db_path, nid, range_key, station=None):
    """The node's packets in range as CSV; raw_json carries anything that isn't a column."""
    db = _connect(db_path, station)
    try:
        nid = alias_map(db).get(nid, nid)
        since = _window(db, range_key)[0]
        rows = db.execute("""SELECT ts, from_id, to_id, portnum, channel, hops, snr, rssi, relay, via_mqtt, pki,
                                    pkt_id, summary, raw FROM packets WHERE from_id = ? AND ts >= ? ORDER BY ts""",
                          (nid, since)).fetchall()
    finally:
        db.close()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["time_local", "unix_ts", "from", "to", "portnum", "channel", "hops", "snr", "rssi",
                "relay_byte", "via_mqtt", "pki_encrypted", "packet_id", "summary", "raw_json"])
    for r in rows:
        relay = f"{r['relay']:02x}" if r["relay"] is not None else ""
        w.writerow([datetime.fromtimestamp(r["ts"]).isoformat(sep=" ", timespec="seconds"), f"{r['ts']:.3f}",
                    r["from_id"], r["to_id"], r["portnum"], r["channel"], r["hops"], r["snr"], r["rssi"],
                    relay, r["via_mqtt"], r["pki"], r["pkt_id"], r["summary"], r["raw"]])
    return buf.getvalue()
