"""Station reach and gaps: which radios each fixed station hears directly, and where the radios nobody hears
directly are. For deciding where another station would add the most.

A fixed station's direct neighbours are the radios it heard at 0 hops (its first copy of a packet, as logged in
packets). Each is drawn at its latest position, the most precise one it sent in the last POS_DAYS: a position
rounded by its channel (precision_bits < 32) is the middle of a box, and its size is kept (precisionKm) so the map
can say so. Radios heard only through relays, by every station shown, are the gaps. They're grouped into squares of
CELL_KM (wider than a 13-bit box, ~3 km, the precision most public-channel positions are rounded to); a square with
one or more of them and no directly heard radio is where a new station could hear what ours can't.

A radio a station hears directly is in its reach, wherever it is. The far ones are worth knowing (a relay on a ridge),
and a short path that isn't heard is worth knowing too, so nothing is filtered by distance here: each radio carries
its distance to the nearest station shown (km), and the page picks how far out to look.

Moving stations (own GPS fixes spanning MOVED_KM or more in range) are left out: their direct neighbours are wherever
they drove (the Coverage map's drive view). Stations with no known location are listed but can't be drawn.
"""
import math
import statistics

import radioid
from analytics import ALL_STATIONS, STATION_LOCATIONS, _connect, _window

POS_DAYS = 30      # a radio's position: the most precise one it sent in this long before the range ends
CELL_KM = 3.0      # gap squares: wider than the ~3 km box of a 13-bit (public channel) position
MOVED_KM = 1.0     # own GPS fixes spanning this much in range: a moving station, not drawn here


def _km(a, b):
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * 6371 * math.asin(math.sqrt(h))


def _precision_km(bits):
    """Width of the box a rounded position stands for (0 for full precision)."""
    return 0.0 if not bits or bits >= 32 else round(23.3 * 2 ** (10 - bits), 2)


def compute(db_path, range_key, station, describe):
    db = _connect(db_path)  # unscoped: every query below names its station
    try:
        return _compute(db, range_key, station, describe)
    finally:
        db.close()


def _station_place(db, st, since, until):
    """(lat, lon, "configured" | "gps") for a fixed station, "moving" for one whose GPS track spans MOVED_KM, or None."""
    fixes = db.execute("SELECT lat, lon FROM positions WHERE node = ?1 AND station = ?1 AND source = 'own' "
                       "AND (precision_bits IS NULL OR precision_bits >= 32) AND ts BETWEEN ?2 AND ?3", (st, since, until)).fetchall()
    if fixes:
        lats, lons = [f[0] for f in fixes], [f[1] for f in fixes]
        if _km((min(lats), min(lons)), (max(lats), max(lons))) >= MOVED_KM:
            return "moving"
    loc = STATION_LOCATIONS.get(st)
    if loc:
        return (loc[0], loc[1], "configured")
    if fixes:
        return (statistics.median(f[0] for f in fixes), statistics.median(f[1] for f in fixes), "gps")
    return None


def _positions(db, until):
    """node -> (lat, lon, precision_bits, ts): its most precise position in the POS_DAYS before `until`, latest of those."""
    best = {}
    for node, lat, lon, bits, ts in db.execute(
            "SELECT node, lat, lon, precision_bits, ts FROM positions WHERE ts BETWEEN ? AND ? AND lat IS NOT NULL "
            "AND lon IS NOT NULL AND NOT (lat = 0 AND lon = 0) ORDER BY ts", (until - POS_DAYS * 86400, until)):
        b = 32 if bits is None else bits
        if node not in best or b >= best[node][2]:
            best[node] = (lat, lon, b, ts)
    return best


def _compute(db, range_key, station, describe):
    since, until, *_ = _window(db, range_key)
    ours = sorted(r[0] for r in db.execute("SELECT DISTINCT station FROM packets WHERE station IS NOT NULL"))
    wanted = [station] if station and station != ALL_STATIONS else ours
    stations, skipped = [], []
    for st in wanted:
        if not radioid.is_radio_id(st):
            continue
        place = _station_place(db, st, since, until)
        if place is None or place == "moving":
            skipped.append({"id": st, "name": describe(st)["name"], "why": "moving" if place else "no location"})
            continue
        stations.append({"id": st, "name": describe(st)["name"], "lat": place[0], "lon": place[1], "located": place[2]})
    shown = {s["id"] for s in stations}

    # every radio heard in range by the stations shown: per station, its fewest hops and its direct receptions
    heard = {}
    for frm, st, hops, snr, ts in db.execute(
            "SELECT from_id, station, hops, snr, ts FROM packets WHERE ts BETWEEN ? AND ? AND from_id IS NOT NULL",
            (since, until)):
        if st not in shown or frm in ours:
            continue   # a station we don't draw, or one of our own stations' radios
        h = heard.setdefault(frm, {}).setdefault(st, {"hops": None, "direct": 0, "snrs": [], "last": 0})
        if hops is not None and (h["hops"] is None or hops < h["hops"]):
            h["hops"] = hops
        if hops == 0:
            h["direct"] += 1
            if snr is not None:
                h["snrs"].append(snr)
        h["last"] = max(h["last"], ts)

    pos = _positions(db, until)
    radios, links = [], []
    for nid, by in heard.items():
        p = pos.get(nid)
        direct = sorted(s for s, h in by.items() if h["direct"])
        hops = [h["hops"] for h in by.values() if h["hops"] is not None]
        r = {"id": nid, "name": describe(nid)["name"], "direct": direct, "minHops": min(hops) if hops else None,
             "stations": len(by), "lat": p[0] if p else None, "lon": p[1] if p else None,
             "precisionKm": _precision_km(p[2]) if p else None, "posTs": p[3] if p else None, "km": None}
        if p and stations:
            r["km"] = round(min(_km((p[0], p[1]), (s["lat"], s["lon"])) for s in stations), 1)
        radios.append(r)
        for st in direct:
            h = by[st]
            s = next(x for x in stations if x["id"] == st)
            links.append({"station": st, "radio": nid, "receptions": h["direct"], "last": h["last"],
                          "bestSnr": max(h["snrs"]) if h["snrs"] else None,
                          "medSnr": round(statistics.median(h["snrs"]), 2) if h["snrs"] else None,
                          "km": round(_km((p[0], p[1]), (s["lat"], s["lon"])), 1) if p else None})

    for s in stations:
        mine = [lk for lk in links if lk["station"] == s["id"]]
        far = [lk["km"] for lk in mine if lk["km"] is not None]
        only = [r["id"] for r in radios if r["direct"] == [s["id"]]]
        s.update(direct=len(mine), placed=len(far), farthestKm=max(far) if far else None,
                 medianKm=round(statistics.median(far), 1) if far else None,
                 medSnr=round(statistics.median(lk["medSnr"] for lk in mine if lk["medSnr"] is not None), 1)
                 if any(lk["medSnr"] is not None for lk in mine) else None,
                 onlyHere=len(only))

    return {"range": range_key, "since": since, "until": until, "cellKm": CELL_KM, "stations": stations,
            "skipped": skipped, "radios": radios, "links": links, "cells": _cells(radios),
            "stats": {"radios": len(radios), "direct": sum(1 for r in radios if r["direct"]),
                      "relayedOnly": sum(1 for r in radios if not r["direct"]),
                      "placed": sum(1 for r in radios if r["lat"] is not None),
                      "gapsPlaced": sum(1 for r in radios if not r["direct"] and r["lat"] is not None)}}


def _cells(radios):
    """Squares of CELL_KM on a fixed worldwide grid (rows of latitude, columns sized at each row's middle, as drive.py),
    with the placed radios in each: how many are heard directly and how many only through relays."""
    dlat = CELL_KM / 111.32
    dlon_row = lambda i: CELL_KM / (111.32 * max(0.1, math.cos(math.radians((i + 0.5) * dlat))))  # noqa: E731
    cells = {}
    for r in radios:
        if r["lat"] is None:
            continue
        i = math.floor(r["lat"] / dlat)
        j = math.floor(r["lon"] / dlon_row(i))
        c = cells.setdefault((i, j), {"direct": [], "relayed": []})
        c["direct" if r["direct"] else "relayed"].append(r["id"])
    out = []
    for (i, j), c in cells.items():
        out.append({"lat": round((i + 0.5) * dlat, 6), "lon": round((j + 0.5) * dlon_row(i), 6),
                    "bounds": [[round(i * dlat, 6), round(j * dlon_row(i), 6)],
                               [round((i + 1) * dlat, 6), round((j + 1) * dlon_row(i), 6)]],
                    "direct": c["direct"], "relayed": c["relayed"], "gap": not c["direct"]})
    return out
