"""Drive coverage: what a moving station heard, and where.

A mobile station logs its radio's own GPS fixes (positions with source = 'own', see analytics.station_track);
fixes rounded to a channel's precision (precision_bits < 32) are never used.
Each reception it logged is placed where the station was at that moment (analytics.station_position: the
latest fix at or before it, within 12 minutes; never interpolated), then the route is binned into square
cells. Per cell:

  - minutes spent there (track time, up to 12 minutes per fix: the rule that places receptions),
  - receptions and receptions per minute spent there,
  - distinct radios heard, and how many were heard directly (0 hops),
  - best and median SNR / RSSI of what was heard,
  - which relays carried the traffic (relay byte resolved as everywhere else; "unresolved" otherwise).

Each track fix also carries how many receptions landed within 60 s of it, so silent stretches (coverage
holes) show up along the route. Only the station's own receptions count; its own transmissions and packets
from other listening stations' radios are left out. Receptions come from rx_hops (every copy, with relay
and hops); a station without the firmware debug log falls back to its packets (first copies only).
"""
import bisect
import math
import statistics
from collections import Counter, defaultdict

from analytics import ALL_STATIONS, STATION_RE, _connect, _window
from topology import relay_resolver

MAX_FIX_AGE_S = 12 * 60   # a fix older than this doesn't place a reception: a parked station logs one every
                          # 10 min, which arrive a little over 10 min apart (polling), so 2 min of slack
TRACK_GAP_S = MAX_FIX_AGE_S  # longer between fixes = a break in the route; time in a cell is counted up to
                             # this per fix, the same rule that places receptions (a parked station logs a fix every 10 min)
MOVED_M = 200             # the combined view only shows stations whose track spans at least this (they moved)
NEAR_S = 60               # receptions within this of a fix count as "heard here" for the route colouring
BINS_M = (100, 250, 500, 1000)


def compute(db_path, range_key, station, describe, bin_m=250):
    db = _connect(db_path)  # unscoped: every query below names its station
    try:
        return _compute(db, range_key, station, describe, bin_m)
    finally:
        db.close()


def _compute(db, range_key, station, describe, bin_m):
    if bin_m not in BINS_M:
        raise ValueError(f"bin must be one of {', '.join(map(str, BINS_M))} (metres)")
    since, until, *_ = _window(db, range_key)
    # one station, or (combined view / none given) every station with its own GPS track in the window
    if station and station != ALL_STATIONS:
        candidates = [station]
    else:
        candidates = [r[0] for r in db.execute(
            "SELECT DISTINCT station FROM positions WHERE source = 'own' AND node = station AND (precision_bits IS NULL OR precision_bits >= 32) "
            "AND ts BETWEEN ? AND ?",
            (since, until)) if r[0]]
    explicit = bool(station and station != ALL_STATIONS)
    stations_out, tracks, recs = [], [], []
    others = {r[0] for r in db.execute("SELECT DISTINCT station FROM packets WHERE station IS NOT NULL")}
    for st in candidates:
        if not st or not STATION_RE.match(st):
            continue
        fixes = [tuple(r) for r in db.execute(
            "SELECT ts, lat, lon FROM positions WHERE node = ?1 AND station = ?1 AND source = 'own' "
            "AND (precision_bits IS NULL OR precision_bits >= 32) AND ts BETWEEN ?2 AND ?3 ORDER BY ts", (st, since - MAX_FIX_AGE_S, until))]
        if not fixes:
            continue
        if not explicit and _span_m(fixes) < MOVED_M:
            continue  # a station that stayed put isn't a drive
        resolve = relay_resolver(db, st)
        rows = db.execute(
            "SELECT ts, from_id, hops, relay, snr, rssi FROM rx_hops WHERE station = ? AND ts BETWEEN ? AND ? "
            "AND from_id IS NOT NULL ORDER BY ts", (st, since, until)).fetchall()
        source = "receptions"
        if not rows:  # no debug log on this station: decoded packets (first copies) instead
            rows = db.execute(
                "SELECT ts, from_id, hops, relay, snr, rssi FROM packets WHERE station = ? AND ts BETWEEN ? AND ? "
                "AND from_id IS NOT NULL ORDER BY ts", (st, since, until)).fetchall()
            source = "packets"
        fts = [f[0] for f in fixes]
        placed = []
        for ts, frm, hops, relay, snr, rssi in rows:
            if frm == st or frm in others:
                continue  # our own radio, or another of our stations
            i = bisect.bisect_right(fts, ts) - 1
            if i < 0 or ts - fts[i] > MAX_FIX_AGE_S:
                continue
            rel = None
            if hops and relay is not None:
                rid = resolve(relay)[0]
                rel = rid or f"0x{relay:02x}"
            placed.append({"ts": ts, "lat": fixes[i][1], "lon": fixes[i][2], "from": frm, "hops": hops,
                           "relay": rel, "snr": snr, "rssi": rssi, "station": st})
        recs += placed
        # the route, split where fixes are too far apart; each fix: receptions within NEAR_S
        rts = [p["ts"] for p in placed]
        segs, cur = [], []
        for k, (ts, lat, lon) in enumerate(fixes):
            if ts < since:
                continue
            if cur and ts - cur[-1][2] > TRACK_GAP_S:
                segs.append(cur); cur = []
            near = bisect.bisect_right(rts, ts + NEAR_S) - bisect.bisect_left(rts, ts - NEAR_S)
            cur.append([round(lat, 6), round(lon, 6), ts, near])
        if cur:
            segs.append(cur)
        tracks.append({"station": st, "segments": segs})
        stations_out.append({"id": st, "name": describe(st)["name"], "fixes": len(fixes), "receptions": len(placed),
                             "source": source, "first": fixes[0][0], "last": fixes[-1][0]})

    cells = _bin(tracks, recs, bin_m, describe)
    return {"range": range_key, "since": since, "until": until, "bin": bin_m, "bins": list(BINS_M),
            "stations": stations_out, "tracks": tracks, "cells": cells,
            "stats": {"receptions": len(recs), "cells": len(cells),
                      "radios": len({r["from"] for r in recs}),
                      "minutes": round(sum(c["minutes"] for c in cells), 1)}}


def _span_m(fixes):
    """Largest extent of a set of fixes, in metres (bounding-box diagonal)."""
    lats, lons = [f[1] for f in fixes], [f[2] for f in fixes]
    dy = (max(lats) - min(lats)) * 111_320
    dx = (max(lons) - min(lons)) * 111_320 * math.cos(math.radians(sum(lats) / len(lats)))
    return math.hypot(dx, dy)


def _bin(tracks, recs, bin_m, describe):
    """Square cells of bin_m metres (an equirectangular grid around the data's mean latitude)."""
    lats = [p[0] for t in tracks for s in t["segments"] for p in s] + [r["lat"] for r in recs]
    if not lats:
        return []
    lat0 = sum(lats) / len(lats)
    dlat = bin_m / 111_320
    dlon = bin_m / (111_320 * max(0.1, math.cos(math.radians(lat0))))
    key = lambda lat, lon: (math.floor(lat / dlat), math.floor(lon / dlon))  # noqa: E731
    cells = defaultdict(lambda: {"minutes": 0.0, "recs": [], "fixes": 0})
    for t in tracks:  # time spent in each cell: each fix-to-fix interval goes to the cell it started in
        for seg in t["segments"]:
            for a, b in zip(seg, seg[1:] + [None]):
                c = cells[key(a[0], a[1])]
                c["fixes"] += 1
                if b is not None:
                    c["minutes"] += min(b[2] - a[2], TRACK_GAP_S) / 60
    for r in recs:
        cells[key(r["lat"], r["lon"])]["recs"].append(r)
    out = []
    for (i, j), c in cells.items():
        rs = c["recs"]
        snrs = [r["snr"] for r in rs if r["snr"] is not None]
        rssis = [r["rssi"] for r in rs if r["rssi"] is not None]
        senders = Counter(r["from"] for r in rs)
        direct = {r["from"] for r in rs if r["hops"] == 0}
        relays = Counter(r["relay"] for r in rs if r["relay"])
        name = lambda nid: describe(nid)["name"] if nid.startswith("!") else f"unresolved {nid}"  # noqa: E731
        minutes = round(max(c["minutes"], 0.0), 2)
        out.append({
            "lat": round((i + 0.5) * dlat, 6), "lon": round((j + 0.5) * dlon, 6),
            "bounds": [[round(i * dlat, 6), round(j * dlon, 6)], [round((i + 1) * dlat, 6), round((j + 1) * dlon, 6)]],
            "minutes": minutes, "receptions": len(rs),
            "perMin": round(len(rs) / minutes, 2) if minutes >= 0.25 else None,
            "radios": len(senders), "direct": len(direct),
            "bestSnr": max(snrs) if snrs else None, "medSnr": round(statistics.median(snrs), 2) if snrs else None,
            "bestRssi": max(rssis) if rssis else None,
            "topRadios": [[n, name(n), k] for n, k in senders.most_common(8)],
            "directRadios": [[n, name(n)] for n in sorted(direct)][:12],
            "relays": [[n, name(n), k] for n, k in relays.most_common(5)],
            "first": min((r["ts"] for r in rs), default=None), "last": max((r["ts"] for r in rs), default=None),
        })
    return out
