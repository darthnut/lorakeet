"""Link history: how each radio link's signal has changed over time, a before/after comparison around a date, and
the moments a link's signal stepped up or down (with what was logged nearby).

A link is an unordered pair of radios. Its observations, each a time and an SNR:
  measured  the links table: a station hearing a radio directly (0 hops), and every traceroute hop.
  relayed   a reception that came through a relay: the last hop, relayer -> station, when the 1-byte relay id
            resolves to one radio (topology.relay_resolver). Inferred, and labelled so.
Only a direct reception measures a link; a radio heard only through relays shows its relay's link instead.

Per link and bucket (an hour for short ranges, a day otherwise): median SNR, the middle half (p25-p75) and the
count. Buckets with no logging are gaps, not zeros (analytics coverage). SNR saturates for strong links (a radio
in the same house reads about +6 to +10 dB whatever the antenna), so a better antenna shows on the distant links.

Steps (find_steps): binary segmentation over the observations, splitting at local midnights and at logged station
events; a split counts when the medians of up to STEP_WINDOW_S either side differ by STEP_DB or more, each side has
STEP_MIN_OBS readings over at least two days, and the two sides' middle halves don't overlap. Events within
EVENT_NEAR_S of a step are attached to it: radio settings saved, the station's Wi-Fi network changing (it moved).
Worked out on request from the log; nothing is stored.
"""
import bisect
import json
import statistics
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta

import radioid
import weather
from analytics import RANGES, _connect, _coverage, scope_local, station_ids
from topology import relay_resolver

STEP_DB = 5.0
STEP_MIN_OBS = 8
STEP_WINDOW_S = 7 * 86400
STEP_MIN_DAYS = 2
MAX_STEPS = 3
EVENT_NEAR_S = 12 * 3600
MIN_OBS = 3          # a link needs this many readings in range to be listed
MAX_LINKS = 300
DAILY_MIN_PER_HOUR = 3   # a link's daily pattern needs this many readings in an hour of the day ...
DAILY_MIN_HOURS = 12     # ... in this many of the 24 hours
DAILY_MIN_DAY = 6        # (days with fewer readings don't count towards it)
DAILY_MIN_SWING = 1.5    # a smaller swing than this (dB) is no pattern worth adjusting for


def _q(db, sql, *a):
    return db.execute(sql, a).fetchall()


def observations(db, since, until, local):
    """(a, b) sorted pair -> {"obs": [(ts, snr)], "sources": Counter, "stations": set}."""
    out = defaultdict(lambda: {"obs": [], "sources": Counter(), "stations": set()})

    def add(a, b, ts, snr, source, station):
        if not a or not b or a == b or snr is None:
            return
        k = tuple(sorted((a, b)))
        out[k]["obs"].append((ts, snr))
        out[k]["sources"][source] += 1
        if station:
            out[k]["stations"].add(station)

    for r in _q(db, "SELECT ts, a, b, snr, source, COALESCE(station, ?) FROM links WHERE ts >= ? AND ts < ? "
                    "AND source IN ('direct', 'traceroute')", local, since, until):
        add(r[1], r[2], r[0], r[3], r[4], r[5])
    # a relay byte only stands for a radio that had been heard by then: resolved against today's radios, an old
    # reception's byte matched a radio first flashed days later
    first = first_heard(db)
    resolvers = {}
    for ts, frm, relay, snr, at in _q(db, "SELECT ts, from_id, relay, snr, COALESCE(station, ?) FROM rx_hops "
                                          "WHERE ts >= ? AND ts < ? AND hops >= 1 AND relay IS NOT NULL AND snr IS NOT NULL",
                                      local, since, until):
        if not at or frm == at:
            continue
        if at not in resolvers:
            resolvers[at] = relay_resolver(db, at)
        relayer, _how = resolvers[at](relay)
        if relayer and relayer != at and first.get(relayer, 0) <= ts + 60:
            add(relayer, at, ts, snr, "relay", at)
    for v in out.values():
        v["obs"].sort()
    return out


def first_heard(db):
    """radio id -> when it was first heard (a packet from it, a link to it, or its identity), from the whole log."""
    first = {}
    for nid, t in _q(db, "SELECT from_id, MIN(ts) FROM main.packets WHERE from_id IS NOT NULL GROUP BY 1 "
                         "UNION ALL SELECT a, MIN(ts) FROM main.links GROUP BY 1 UNION ALL SELECT b, MIN(ts) FROM main.links GROUP BY 1 "
                         "UNION ALL SELECT node, MIN(ts) FROM main.node_info GROUP BY 1"):
        if nid and t is not None and t < first.get(nid, float("inf")):
            first[nid] = t
    return first


def _stats(vals):
    vals = sorted(vals)
    if not vals:
        return None
    q = statistics.quantiles(vals, n=4) if len(vals) >= 4 else [vals[0], statistics.median(vals), vals[-1]]
    return {"n": len(vals), "med": round(statistics.median(vals), 2), "p25": round(q[0], 2), "p75": round(q[2], 2)}


def _iqr(vals):
    """Spread as a standard deviation estimate (IQR / 1.35), at least 1 dB: SNR is reported in quarter-dB steps."""
    if len(vals) < 4:
        return max(1.0, statistics.pstdev(vals) if len(vals) > 1 else 1.0)
    q = statistics.quantiles(vals, n=4)
    return max(1.0, (q[2] - q[0]) / 1.35)


def _starts(since, until, bucket):
    t = datetime.fromtimestamp(since).replace(minute=0, second=0, microsecond=0)
    step = timedelta(hours=1)
    if bucket == "day":
        t, step = t.replace(hour=0), timedelta(days=1)
    out = []
    while t.timestamp() <= until:
        out.append(t)
        t += step
    return out


def _midnights(t0, t1):
    d = datetime.fromtimestamp(t0).replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    out = []
    while d.timestamp() < t1:
        out.append(d.timestamp())
        d += timedelta(days=1)
    return out


def station_events(db, since, until):
    """Things logged at a station that can change its links: radio settings saved, its Wi-Fi network changing (a
    station on a new network has usually been moved). [{ts, station, kind, text}]"""
    out, last_net = [], {}
    # main.events: every station's, not just the one in view (a link's far end is often another station)
    for ts, kind, detail, station in _q(db, "SELECT ts, kind, detail, station FROM main.events WHERE kind IN "
                                            "('radio_settings', 'network') AND ts < ? ORDER BY ts", until):
        try:
            d = json.loads(detail or "{}")
        except ValueError:
            d = {}
        if kind == "network":
            # only a switch between two named networks: "no network" in between is a dropout, not a move
            net = d.get("network")
            if not net or net == "no network":
                continue
            prev = last_net.get(station)
            last_net[station] = net
            if prev is None or net == prev or ts < since:
                continue
            out.append({"ts": ts, "station": station, "kind": "network",
                        "text": f"Wi-Fi changed from {prev} to {net} (the station probably moved)"})
        elif ts >= since:
            keys = d.get("changes") or d.get("advanced") or {}
            what = ", ".join(sorted(keys))[:120] if isinstance(keys, dict) else ""
            out.append({"ts": ts, "station": station, "kind": "radio_settings",
                        "text": "Radio settings saved" + (f": {what}" if what else "")})
    return out


def daily_pattern(obs):
    """A link's usual shape over the day: {"offsets": 24 dB offsets from its daily median (index = local hour), "swing",
    "weakest", "strongest"}, or None. Each reading is taken relative to its own day's median first, so a step (an
    antenna, a move) doesn't leak into the hours it happened in; then the median per hour of the day, smoothed over
    neighbouring hours. Temperature and humidity move LoRa signal through the day; this is how much."""
    by_day = defaultdict(list)
    for t, s in obs:
        by_day[datetime.fromtimestamp(t).date()].append((t, s))
    by_hour = defaultdict(list)
    for rows in by_day.values():
        if len(rows) < DAILY_MIN_DAY:
            continue
        m = statistics.median(s for _, s in rows)
        for t, s in rows:
            by_hour[datetime.fromtimestamp(t).hour].append(s - m)
    raw = {h: statistics.median(v) for h, v in by_hour.items() if len(v) >= DAILY_MIN_PER_HOUR}
    if len(raw) < DAILY_MIN_HOURS:
        return None
    offs = []
    for h in range(24):
        near = [raw[(h + d) % 24] for d in (-1, 0, 1) if (h + d) % 24 in raw]
        offs.append(round(statistics.mean(near), 2) if near else 0.0)
    centre = statistics.median(offs)
    offs = [round(o - centre, 2) for o in offs]
    swing = max(offs) - min(offs)
    if swing < DAILY_MIN_SWING:
        return None
    return {"offsets": offs, "swing": round(swing, 1), "weakest": min(range(24), key=lambda h: offs[h]),
            "strongest": max(range(24), key=lambda h: offs[h])}


def deseasoned(obs, pattern):
    """Readings with the link's daily pattern taken out (unchanged without one)."""
    if not pattern:
        return obs
    o = pattern["offsets"]
    return [(t, s - o[datetime.fromtimestamp(t).hour]) for t, s in obs]


def find_steps(obs, cuts=(), events=()):
    """Where a link's signal stepped. obs: sorted [(ts, snr)]. cuts: extra candidate split times (event times).
    Returns [{ts, before, after, delta, nBefore, nAfter, events}] in time order."""
    if len(obs) < 2 * STEP_MIN_OBS:
        return []
    ts = [o[0] for o in obs]
    snr = [o[1] for o in obs]
    day = [datetime.fromtimestamp(t).date() for t in ts]

    def side(lo, hi):   # readings with lo <= ts < hi
        i, j = bisect.bisect_left(ts, lo), bisect.bisect_left(ts, hi)
        return snr[i:j], len(set(day[i:j]))

    segments, steps = [(ts[0], ts[-1] + 1)], []
    while len(steps) < MAX_STEPS:
        best = None
        for lo, hi in segments:
            for cut in sorted(set(_midnights(lo, hi)) | {c for c in cuts if lo < c < hi}):
                b, nb = side(max(lo, cut - STEP_WINDOW_S), cut)
                a, na = side(cut, min(hi, cut + STEP_WINDOW_S))
                if len(b) < STEP_MIN_OBS or len(a) < STEP_MIN_OBS or nb < STEP_MIN_DAYS or na < STEP_MIN_DAYS:
                    continue
                sb, sa = _stats(b), _stats(a)
                delta = sa["med"] - sb["med"]
                apart = sa["p25"] > sb["p75"] if delta > 0 else sa["p75"] < sb["p25"]
                # how cleanly the cut separates the two sides: the step against the readings' spread (a robust
                # t-statistic). The biggest raw step isn't it: a cut a few days late, whose "before" mixes both
                # sides, can show a bigger median gap than the real moment.
                spread = ((_iqr(b) ** 2) / len(b) + (_iqr(a) ** 2) / len(a)) ** 0.5 or 1e-9
                score = abs(delta) / spread
                if abs(delta) >= STEP_DB and apart and (best is None or score > best["score"]):
                    best = {"ts": cut, "seg": (lo, hi), "before": sb, "after": sa, "delta": round(delta, 2), "score": score}
        if not best:
            break
        lo, hi = best.pop("seg")
        best.pop("score")
        segments.remove((lo, hi))
        segments += [(lo, best["ts"]), (best["ts"], hi)]
        steps.append(best)
    for s in steps:
        s["events"] = [e for e in events if abs(e["ts"] - s["ts"]) <= EVENT_NEAR_S]
        s["nBefore"], s["nAfter"] = s["before"]["n"], s["after"]["n"]
    return sorted(steps, key=lambda s: s["ts"])


def history(db_path, range_key, station, describe):
    if range_key not in RANGES:
        raise ValueError(f"range must be one of {', '.join(RANGES)}")
    db = _connect(db_path, station)
    try:
        local = scope_local(station) or ""
        until = time.time()
        first = db.execute("SELECT MIN(ts) FROM packets").fetchone()[0] or until
        since = max(first, until - RANGES[range_key]) if RANGES[range_key] else first
        bucket = "hour" if until - since <= 2 * 86400 else "day"
        fmt = "%Y-%m-%d %H" if bucket == "hour" else "%Y-%m-%d"
        covered = {h if bucket == "hour" else h[:10] for h in _coverage(db, since, local)}
        stations = set(station_ids(db, local))
        events = station_events(db, since, until)
        links, wx = [], {}
        for (a, b), v in observations(db, since, until, local).items():
            obs = v["obs"]
            if len(obs) < MIN_OBS:
                continue
            by_bucket = defaultdict(list)
            for t, s in obs:
                by_bucket[datetime.fromtimestamp(t).strftime(fmt)].append(s)
            series = []
            for start in _starts(since, until, bucket):
                key = start.strftime(fmt)
                st = _stats(by_bucket.get(key, []))
                series.append({"t": start.timestamp(), **(st or {}), "covered": key in covered})
            mine = [e for e in events if e["station"] in (a, b)]
            pattern = daily_pattern(obs)
            # steps on readings with the daily pattern taken out: an afternoon dip every day isn't a change
            steps = find_steps(deseasoned(obs, pattern), [e["ts"] for e in mine], mine)
            days = sorted({datetime.fromtimestamp(t).date() for t, _ in obs})
            cov_days = len({k[:10] for k in covered}) or 1
            status = None
            if bucket == "day" and obs[0][0] > since + 2 * 86400 and len(days) >= 3:
                status = "new"
            elif bucket == "day" and len(days) >= 3 and until - obs[-1][0] > 2 * 86400:
                status = "quiet"
            st_end = next((x for x in (b, a) if x in stations), None)
            radio = a if st_end == b else b if st_end == a else a
            other = st_end or (b if radio == a else a)
            if st_end and st_end not in wx:
                wx[st_end] = weather.at_station(db, db_path, st_end, since, until)
            links.append({
                "a": radio, "b": other, "aName": describe(radio)["name"], "bName": describe(other)["name"],
                "station": st_end, "sources": dict(v["sources"]), "measured": bool(v["sources"]["direct"] or v["sources"]["traceroute"]),
                **_stats([s for _, s in obs]), "first": obs[0][0], "last": obs[-1][0], "perDay": round(len(obs) / cov_days, 1),
                "series": series, "steps": steps, "status": status, "daily": pattern,
                "weather": weather.fit(obs, wx[st_end]) if st_end and wx[st_end] else None,
            })
        links.sort(key=lambda x: (-max((abs(s["delta"]) for s in x["steps"]), default=0), x["status"] is None, -x["n"]))
        return {"range": range_key, "since": since, "until": until, "bucket": bucket, "links": links[:MAX_LINKS],
                "events": events, "stations": sorted(stations), "thresholds": {"stepDb": STEP_DB, "minObs": STEP_MIN_OBS}}
    finally:
        db.close()


def compare(db_path, at, days, station, describe, phantom=lambda nid: False):
    """Every link's signal and traffic in the `days` before `at` against the `days` after (to now at most)."""
    at, days = float(at), max(1, min(int(days), 60))
    now = time.time()
    if not (0 < at < now):
        raise ValueError("at must be a time in the past")
    db = _connect(db_path, station)
    try:
        local = scope_local(station) or ""
        win = {"before": (at - days * 86400, at), "after": (at, min(now, at + days * 86400))}
        hours = _coverage(db, win["before"][0], local)

        def covered_h(lo, hi):
            return sum(1 for h in hours if lo <= datetime.strptime(h, "%Y-%m-%d %H").timestamp() < hi) or None
        side = {k: observations(db, lo, hi, local) for k, (lo, hi) in win.items()}
        cov = {k: covered_h(*w) for k, w in win.items()}
        stations = set(station_ids(db, local))
        rows = []
        for key in set(side["before"]) | set(side["after"]):
            b = [s for _, s in side["before"].get(key, {"obs": []})["obs"]]
            a = [s for _, s in side["after"].get(key, {"obs": []})["obs"]]
            if len(a) + len(b) < MIN_OBS:
                continue
            sb, sa = _stats(b), _stats(a)
            st_end = next((x for x in key if x in stations), None)
            radio = next((x for x in key if x != st_end), key[0])
            other = st_end or next(x for x in key if x != radio)
            srcs = side["before"].get(key, {"sources": Counter()})["sources"] + side["after"].get(key, {"sources": Counter()})["sources"]
            rows.append({
                "a": radio, "b": other, "aName": describe(radio)["name"], "bName": describe(other)["name"],
                "measured": bool(srcs["direct"] or srcs["traceroute"]), "before": sb, "after": sa,
                "delta": round(sa["med"] - sb["med"], 2) if sa and sb else None,
                "perDayBefore": round(len(b) / (cov["before"] / 24), 1) if cov["before"] else None,
                "perDayAfter": round(len(a) / (cov["after"] / 24), 1) if cov["after"] else None,
                "status": "gained" if not b else "lost" if not a else None,
            })
        # links heard on both sides first, biggest change first; then links that appeared, then ones that went
        rows.sort(key=lambda r: ({None: 0, "gained": 1, "lost": 2}[r["status"]], -abs(r["delta"] or 0),
                                 -((r["before"] or {}).get("n", 0) + (r["after"] or {}).get("n", 0))))

        def heard(lo, hi):
            return {r[0] for r in _q(db, "SELECT DISTINCT from_id FROM packets WHERE ts >= ? AND ts < ? AND from_id IS NOT NULL", lo, hi)
                    if r[0] not in stations and not phantom(r[0]) and radioid.is_radio_id(r[0])}
        hb, ha = heard(*win["before"]), heard(*win["after"])
        names = lambda ids: sorted(({"id": i, "name": describe(i)["name"]} for i in ids), key=lambda d: d["name"].lower())  # noqa: E731
        return {"at": at, "days": days, "windows": {k: {"since": w[0], "until": w[1], "coveredHours": cov[k]} for k, w in win.items()},
                "links": rows[:MAX_LINKS], "radios": {"onlyBefore": names(hb - ha), "onlyAfter": names(ha - hb), "both": len(ha & hb)},
                "events": [e for e in station_events(db, win["before"][0], win["after"][1])]}
    finally:
        db.close()
