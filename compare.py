"""Station comparison: what two listening stations heard of the same mesh, side by side.

Only MINUTES both stations were logging count (a station that started later, or restarted, must not
look deaf; hours were too coarse: a station down for half an hour still "covered" it), and
packets from either station's own radio are left out (that's our own traffic, not the mesh). A packet is
identified by (sender, packet id); a station "heard" it if any copy reached it. Per station, the best
reception of each packet (highest SNR) stands for it, so a weak duplicate doesn't drag it down.

Paired figures (signal difference, arrival gap, same/different relay) use only packets heard by both.
The arrival gap mixes in the two computers' clock difference (both NTP-synced, so usually well under a
second) and the 15-60 s it can take a collector to log a packet is NOT involved: ts is when each radio
logged the reception.
"""
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timedelta

from analytics import STATION_RE, _bucket_starts, _connect, _window, port_group
from topology import relay_resolver


def compute(db_path, range_key, a, b, describe):
    for s in (a, b):
        if not (isinstance(s, str) and STATION_RE.match(s)):
            raise ValueError(f"not a station id: {s!r}")
    if a == b:
        raise ValueError("pick two different stations")
    db = _connect(db_path)
    try:
        return _compute(db, range_key, a, b, describe)
    finally:
        db.close()


def _minutes(db, station, since):
    """Minutes (epoch // 60) this station was demonstrably logging: within a minute of one of its radio's
    once-a-minute reports, or a minute it logged a reception in."""
    out = set()
    for (ts,) in db.execute("SELECT ts FROM telemetry_full WHERE station = ? AND node = ? AND kind = 'deviceMetrics' AND ts >= ?",
                            (station, station, since)):
        m = int(ts // 60)
        out.update((m - 1, m, m + 1))
    out.update(int(r[0] // 60) for r in db.execute("SELECT ts FROM rx_hops WHERE station = ? AND ts >= ?", (station, since)))
    return out


def _compute(db, range_key, a, b, describe):
    since, until, _first, bucket, fmt = _window(db, range_key)
    both_min = _minutes(db, a, since) & _minutes(db, b, since)
    both_hours = {datetime.fromtimestamp(m * 60).strftime("%Y-%m-%d %H") for m in both_min}
    ports = {(r[0], r[1]): r[2] for r in db.execute(
        "SELECT from_id, pkt_id, portnum FROM packets WHERE ts >= ? AND pkt_id IS NOT NULL", (since - 600,))}

    # best reception of each packet at each station, within hours both were logging
    best = {a: {}, b: {}}
    first = {a: {}, b: {}}
    for r in db.execute("SELECT station, from_id, pkt_id, ts, snr, rssi, hops, relay FROM rx_hops "
                        "WHERE station IN (?, ?) AND ts >= ? AND pkt_id IS NOT NULL AND from_id NOT IN (?, ?)",
                        (a, b, since, a, b)):
        if int(r["ts"] // 60) not in both_min:
            continue
        k = (r["from_id"], r["pkt_id"])
        cur = best[r["station"]].get(k)
        if cur is None or (r["snr"] is not None and (cur["snr"] is None or r["snr"] > cur["snr"])):
            best[r["station"]][k] = dict(r)
        f = first[r["station"]].get(k)
        if f is None or r["ts"] < f["ts"]:
            first[r["station"]][k] = dict(r)
    ka, kb = set(best[a]), set(best[b])
    both, only_a, only_b = ka & kb, ka - kb, kb - ka

    # per node
    nodes = defaultdict(lambda: {"a": [], "b": [], "both": 0, "diff": [], "firstA": 0})
    for k in ka | kb:
        n = nodes[k[0]]
        if k in best[a]:
            n["a"].append(best[a][k])
        if k in best[b]:
            n["b"].append(best[b][k])
        if k in both:
            n["both"] += 1
            sa, sb = best[a][k]["snr"], best[b][k]["snr"]
            if sa is not None and sb is not None:
                n["diff"].append(sb - sa)
            n["firstA"] += first[a][k]["ts"] <= first[b][k]["ts"]
    med = lambda xs: statistics.median(xs) if xs else None  # noqa: E731

    def side(rows):
        if not rows:
            return None
        return {"packets": len(rows), "snr": med([r["snr"] for r in rows if r["snr"] is not None]),
                "rssi": med([r["rssi"] for r in rows if r["rssi"] is not None]),
                "hops": med([r["hops"] for r in rows if r["hops"] is not None]),
                "direct": sum(1 for r in rows if r["hops"] == 0)}
    node_rows = []
    for nid, n in nodes.items():
        node_rows.append({"id": nid, **describe(nid), "a": side(n["a"]), "b": side(n["b"]), "both": n["both"],
                          "snrDiff": med(n["diff"]), "firstAShare": n["firstA"] / n["both"] if n["both"] else None})
    node_rows.sort(key=lambda r: -((r["a"] or {}).get("packets", 0) + (r["b"] or {}).get("packets", 0)))

    # paired: relays and arrival gap
    res = {a: relay_resolver(db, a), b: relay_resolver(db, b)}

    def relay_name(station, r):
        if r["hops"] == 0:
            return "direct"
        if r["relay"] is None:
            return "unknown"
        node, _ = res[station](r["relay"])
        return node or f"0x{r['relay']:02x}"
    same_relay, diff_pairs = 0, Counter()
    gaps = []
    for k in both:
        ra, rb = relay_name(a, first[a][k]), relay_name(b, first[b][k])
        if ra == rb:
            same_relay += 1
        else:
            diff_pairs[(ra, rb)] += 1
        gaps.append(first[b][k]["ts"] - first[a][k]["ts"])
    top_relays = {s: Counter(relay_name(s, r) for r in first[s].values()).most_common(6) for s in (a, b)}

    # over time: packets heard by A only / both / B only, per bucket
    series = []
    tsof = {k: min(first[s][k]["ts"] for s in (a, b) if k in first[s]) for k in ka | kb}
    per = defaultdict(Counter)
    for k, ts in tsof.items():
        per[datetime.fromtimestamp(ts).strftime(fmt)]["both" if k in both else "a" if k in ka else "b"] += 1
    for start in _bucket_starts(since, until, bucket):
        hours = [start + timedelta(hours=h) for h in range(1 if bucket == "hour" else 24)]
        covered = any(h.strftime("%Y-%m-%d %H") in both_hours for h in hours)
        c = per.get(start.strftime(fmt), Counter())
        series.append({"t": start.timestamp(), "covered": covered, "a": c["a"] if covered else None,
                       "both": c["both"] if covered else None, "b": c["b"] if covered else None})

    groups = Counter()
    for k in ka | kb:
        groups[(port_group(ports.get(k)) if ports.get(k) else "Not decoded", "both" if k in both else "a" if k in ka else "b")] += 1
    return {
        "range": range_key, "since": since, "until": until, "bucket": bucket, "a": a, "b": b,
        "aName": describe(a)["name"], "bName": describe(b)["name"],
        "minutesBoth": len(both_min), "firstBoth": min(both_min) * 60 if both_min else None,
        "packets": {"both": len(both), "a": len(only_a), "b": len(only_b)},
        "nodes": {"both": sum(1 for r in node_rows if r["a"] and r["b"]), "a": sum(1 for r in node_rows if r["a"] and not r["b"]),
                  "b": sum(1 for r in node_rows if r["b"] and not r["a"])},
        "snrDiff": med([best[b][k]["snr"] - best[a][k]["snr"] for k in both
                        if best[a][k]["snr"] is not None and best[b][k]["snr"] is not None]),
        "arrivalGap": med(gaps), "relaysSame": same_relay, "relaysDifferent": sum(diff_pairs.values()),
        "relayPairs": [{"a": x, "b": y, "n": n} for (x, y), n in diff_pairs.most_common(6)],
        "topRelays": {"a": [{"relay": r, "n": n} for r, n in top_relays[a]], "b": [{"relay": r, "n": n} for r, n in top_relays[b]]},
        "series": series, "nodesTable": node_rows,
        "byType": [{"group": g, "side": s, "n": n} for (g, s), n in sorted(groups.items())],
    }


# ======================================================================= every station at once

def matrix(db_path, range_key, describe, stations=None):
    """Node x station view for any number of stations. The key figure is CAPTURE: of a node's packets that
    any station heard during minutes when S AND at least one other station were logging, the share S heard.
    Only co-logged minutes count, so a station is neither penalised for time it was offline nor credited for
    time it was the only one listening (alone, every station "hears everything"). It's fair whatever the
    number of stations. "Only heard by S" likewise needs the others to have been listening and missed it."""
    db = _connect(db_path)
    try:
        all_st = [r[0] for r in db.execute("SELECT DISTINCT station FROM packets WHERE station IS NOT NULL ORDER BY station")]
        sts = [s for s in (stations or all_st) if s in all_st]
        return _matrix(db, range_key, sts, describe)
    finally:
        db.close()


def _matrix(db, range_key, sts, describe):
    since, until, *_ = _window(db, range_key)
    logged = {s: _minutes(db, s, since) for s in sts}
    # minutes S was logging while at least one other station was too: the only minutes S can be compared in
    minutes = {s: logged[s] & set().union(*(logged[o] for o in sts if o != s)) for s in sts}
    marks = ",".join("?" * len(sts))
    heard = defaultdict(dict)    # (sender, pkt id) -> station -> best reception there
    first_ts = {}
    for r in db.execute(f"SELECT station, from_id, pkt_id, ts, snr, rssi, hops FROM rx_hops WHERE station IN ({marks}) "
                        f"AND ts >= ? AND pkt_id IS NOT NULL AND from_id NOT IN ({marks})", (*sts, since, *sts)):
        k = (r["from_id"], r["pkt_id"])
        cur = heard[k].get(r["station"])
        if cur is None or (r["snr"] is not None and (cur["snr"] is None or r["snr"] > cur["snr"])):
            heard[k][r["station"]] = dict(r)
        first_ts[k] = min(first_ts.get(k, r["ts"]), r["ts"])
    med = lambda xs: statistics.median(xs) if xs else None  # noqa: E731

    cells = defaultdict(lambda: defaultdict(lambda: {"eligible": 0, "heard": 0, "snr": [], "rssi": [], "hops": []}))
    for k, by in heard.items():
        m = int(first_ts[k] // 60)
        for s in sts:
            if m not in minutes[s]:
                continue  # S wasn't logging: neither a hit nor a miss
            c = cells[k[0]][s]
            c["eligible"] += 1
            if s in by:
                c["heard"] += 1
                for f in ("snr", "rssi", "hops"):
                    if by[s][f] is not None:
                        c[f].append(by[s][f])
    nodes = []
    for nid, per in cells.items():
        row = {"id": nid, **describe(nid), "stations": {}}
        for s in sts:
            c = per.get(s)
            if not c or not c["eligible"]:
                row["stations"][s] = None
                continue
            row["stations"][s] = {"eligible": c["eligible"], "heard": c["heard"], "capture": c["heard"] / c["eligible"],
                                  "snr": med(c["snr"]), "rssi": med(c["rssi"]), "hops": med(c["hops"])}
        hearing = [s for s, v in row["stations"].items() if v and v["heard"]]
        caps = [v["capture"] for v in row["stations"].values() if v]
        # compared at all? (some station was co-logging when this node transmitted)
        compared = [s for s, v in row["stations"].items() if v]
        row.update(heardBy=hearing, bestCapture=max(caps) if caps else None, compared=bool(compared),
                   packets=sum(1 for k in heard if k[0] == nid),
                   # only one station heard it while the others were listening too, and they missed every packet
                   single=len(hearing) == 1 and all(row["stations"][o] and not row["stations"][o]["heard"]
                                                    for o in sts if o != hearing[0]),
                   weak=bool(caps) and max(caps) < 0.5)
        nodes.append(row)
    nodes = [n for n in nodes if n["compared"]]  # nodes that never transmitted while 2+ stations listened can't be compared
    nodes.sort(key=lambda r: (-len(r["heardBy"]), -r["packets"]))
    summary = []
    for s in sts:
        mine = [n for n in nodes if s in n["heardBy"]]
        caps = [n["stations"][s]["capture"] for n in nodes if n["stations"].get(s) and n["stations"][s]["eligible"] >= 3]
        summary.append({"id": s, "name": describe(s)["name"], "minutes": len(minutes[s]), "loggedMinutes": len(logged[s]),
                        "packets": sum(1 for k, by in heard.items() if s in by and int(first_ts[k] // 60) in minutes[s]),
                        "nodes": len(mine),
                        "unique": sum(1 for n in mine if n["single"] and n["heardBy"] == [s]), "medianCapture": med(caps)})
    co = set().union(*minutes.values()) if minutes else set()
    shared = [by for k, by in heard.items() if int(first_ts[k] // 60) in co]
    return {"range": range_key, "since": since, "until": until, "stations": summary, "nodes": nodes,
            "coMinutes": len(co), "packets": len(shared), "together": sum(1 for by in shared if len(by) == len(sts))}
