"""Airtime: how much channel time the transmissions our radio heard took up, and whose they were.

Airtime is CALCULATED, not measured: LoRa time on air follows exactly from the frame length and the modem
settings (anatomy.radio_layer, Semtech AN1200.13). The firmware's own "Packet RX: N ms" log line is the same
calculation (all 536 checked on 2026-10-06 agree to within 1 ms of rounding), so it isn't stored. What IS
measured is our radio's channel utilization (localStats, % of time the channel was busy), shown alongside.

Source: rx_hops, one row per over-the-air reception (duplicates included: every copy is a separate
transmission, by the sender or by a relay), plus tx_log for our own radio. Both log the whole frame length
(16-byte header included). Only covered hours since reception mining began count; the rest are gaps.
"""
import json
from collections import Counter, defaultdict
from datetime import timedelta

from analytics import (OTHER, PORT_GROUPS, _bucket_starts, _connect, _coverage, _covered, _readable_channels,
                       _window, is_combined, port_group, scope_local)
from anatomy import radio_layer
from topology import relay_resolver

NOT_DECODED = "Not decoded"  # a copy whose packet never reached the API (heard, but our radio passed on another copy or none)


def compute(db_path, range_key, local_id, describe):
    db = _connect(db_path, local_id)
    try:
        return _compute(db, range_key, scope_local(local_id) or "", describe)
    finally:
        db.close()


def _compute(db, range_key, local, describe):
    since, until, _first, bucket, fmt = _window(db, range_key)
    q = lambda sql, *a: db.execute(sql, a).fetchall()  # noqa: E731
    lora_row = q("SELECT value FROM settings WHERE key='radio_lora'")
    lora = {k: v for k, v in json.loads(lora_row[0][0]).items() if k != "ts"} if lora_row else {}
    ms_cache = {}

    def airtime(length):
        if length not in ms_cache:
            ms_cache[length] = radio_layer(lora, length)["airtimeMs"] / 1000
        return ms_cache[length]

    radio = radio_layer(lora, 0)
    mining_since = q("SELECT MIN(ts) FROM rx_hops")[0][0]
    covered_hours = _coverage(db, since, local)
    readable = {c["hash"] for c in _readable_channels(db)[0]}
    port = {(r[0], r[1]): r[2] for r in q(
        "SELECT from_id, pkt_id, portnum FROM packets WHERE ts >= ? AND pkt_id IS NOT NULL", since - 600)}
    resolvers = {}

    def resolve(relay, at):
        if at not in resolvers:
            resolvers[at] = relay_resolver(db, at)
        return resolvers[at](relay)
    # Combined view: the same transmission heard by several stations is ONE use of the channel. A copy is
    # identified by sender, packet id, hop count and relay byte (a relay's retransmission differs in those).
    combined = is_combined(db)
    rx_sql = ("SELECT MIN(ts) AS ts, from_id, relay, hops, MAX(length) AS length, pkt_id, channel, MIN(station) AS at "
              "FROM rx_hops WHERE ts >= ? AND from_id != COALESCE(station, ?) GROUP BY from_id, pkt_id, hops, relay"
              if combined else
              "SELECT ts, from_id, relay, hops, length, pkt_id, channel, COALESCE(station, ?) AS at FROM rx_hops "
              "WHERE ts >= ? AND from_id != ?")

    by_bucket = defaultdict(Counter)            # bucket -> group -> seconds
    tx_bucket = Counter()                       # bucket -> seconds our radio transmitted
    transmitters = defaultdict(lambda: [0.0, 0])  # radio on air -> [seconds, transmissions]
    originators = defaultdict(lambda: [0.0, 0, set()])  # sender -> [seconds, copies, packet ids]
    by_group = Counter()
    no_length = 0
    for r in (q(rx_sql, since, local) if combined else q(rx_sql, local, since, local)):
        if not r["length"]:
            no_length += 1
            continue
        s = airtime(r["length"])
        p = port.get((r["from_id"], r["pkt_id"]))
        g = port_group(p) if p else (NOT_DECODED if r["channel"] in readable else "Encrypted")
        b = _bkey(r["ts"], fmt)
        by_bucket[b][g] += s
        by_group[g] += s
        # who was physically transmitting: the sender itself (0 hops) or the relay that handed us this copy
        if r["hops"] == 0:
            who = r["from_id"]
        elif r["hops"] is None:
            who = "sender doesn't report hop count"  # can't tell whether the sender or a relay was on air
        else:
            node, _how = resolve(r["relay"], r["at"]) if r["relay"] is not None else (None, None)
            who = node or (f"relay 0x{r['relay']:02x}" if r["relay"] is not None else "relay not logged")
        transmitters[who][0] += s
        transmitters[who][1] += 1
        o = originators[r["from_id"]]
        o[0] += s
        o[1] += 1
        o[2].add(r["pkt_id"])
    ours = 0.0
    for r in q("SELECT ts, length FROM tx_log WHERE ts >= ?", since):
        if r["length"]:
            s = airtime(r["length"])
            tx_bucket[_bkey(r["ts"], fmt)] += s
            ours += s
    # Measured: our radio's channel utilization (% of the last minute the channel was busy, its own TX and
    # undecodable receptions included). Taken from its once-a-minute deviceMetrics; the localStats reports
    # carry it only every ~15 min (47 values on 2026-10-06, ranging 1-15 %), too sparse to average.
    util = {r[0]: (r[1], r[2]) for r in q(
        f"SELECT strftime('{fmt}', ts, 'unixepoch', 'localtime'), AVG(json_extract(data, '$.channelUtilization')), COUNT(*) "
        "FROM telemetry_full WHERE kind='deviceMetrics' AND node=? AND ts >= ? "
        "AND json_extract(data, '$.channelUtilization') IS NOT NULL GROUP BY 1", local, since)}

    groups = [g for g, _ in PORT_GROUPS] + [OTHER, NOT_DECODED]
    groups = [g for g in groups if by_group.get(g)]
    series, covered_s = [], 0.0
    step = timedelta(hours=1) if bucket == "hour" else timedelta(days=1)
    for start in _bucket_starts(since, until, bucket):
        key = start.strftime(fmt)
        # seconds of this bucket that were logged AND after reception mining began: the denominator
        hours = [start + timedelta(hours=h) for h in range(1 if bucket == "hour" else 24)]
        logged = sum(1 for h in hours if h.strftime("%Y-%m-%d %H") in covered_hours
                     and mining_since is not None and (h + timedelta(hours=1)).timestamp() > mining_since
                     and h.timestamp() < until) * 3600.0
        covered = logged > 0 and _covered(start, bucket, covered_hours)
        covered_s += logged if covered else 0
        c = by_bucket.get(key, Counter())
        series.append({
            "t": start.timestamp(), "covered": covered,
            "pct": {g: 100 * c.get(g, 0) / logged for g in groups} if covered else None,
            "total": 100 * sum(c.values()) / logged if covered else None,
            "ours": 100 * tx_bucket.get(key, 0) / logged if covered else None,
            "chUtil": util[key][0] if key in util else None, "chUtilN": util[key][1] if key in util else 0,
        })

    heard = sum(by_group.values())
    tx_rows = sorted(transmitters.items(), key=lambda kv: -kv[1][0])
    or_rows = sorted(originators.items(), key=lambda kv: -kv[1][0])

    def ident(i):
        return {"id": i, **describe(i)} if i.startswith("!") else {"id": None, "name": i}
    ch = [v[0] for v in util.values() if v[0] is not None]
    return {
        "range": range_key, "since": since, "until": until, "bucket": bucket, "miningSince": mining_since,
        "combined": combined, "measuredAt": local,
        "radio": {k: radio[k] for k in ("preset", "sf", "bwKHz", "cr", "symbolMs")},
        "groups": groups, "series": series,
        "totals": {"heardS": heard, "oursS": ours, "loggedS": covered_s,
                   "heardPct": 100 * heard / covered_s if covered_s else None,
                   "oursPct": 100 * ours / covered_s if covered_s else None,
                   "chUtilAvg": sum(ch) / len(ch) if ch else None,
                   "badPackets": _bad_packets(q, local, since),
                   "transmissions": sum(v[1] for v in transmitters.values()), "noLength": no_length},
        "byGroup": [{"group": g, "s": by_group[g]} for g in groups],
        "transmitters": [{**ident(k), "s": v[0], "n": v[1], "share": v[0] / heard if heard else 0} for k, v in tx_rows[:25]],
        "originators": [{**ident(k), "s": v[0], "copies": v[1], "packets": len(v[2]),
                         "share": v[0] / heard if heard else 0} for k, v in or_rows[:25]],
    }


def _bkey(ts, fmt):
    from datetime import datetime
    return datetime.fromtimestamp(ts).strftime(fmt)


def _bad_packets(q, local, since):
    """Receptions our radio rejected (failed CRC etc.): summed increases of localStats numPacketsRxBad,
    skipping counter resets (reboots). None when the radio never reported the counter."""
    total, prev, seen = 0, None, False
    for (v,) in q("SELECT json_extract(data, '$.numPacketsRxBad') FROM telemetry_full WHERE kind='localStats' "
                  "AND node=? AND ts >= ? ORDER BY ts", local, since):
        if v is None:
            continue
        seen = True
        if prev is not None and v >= prev:
            total += v - prev
        prev = v
    return total if seen else None
