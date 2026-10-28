"""Hourly weather where each listening station is, from Open-Meteo ([weather] enabled, off by default).

LoRa signal moves with the weather: a warm afternoon, humidity, rain on an antenna. To tell that apart from a real
change (a moved radio, a failing antenna), Lorakeet keeps the weather at each station and checks which links follow it.

Privacy: only a grid square's centre is ever sent. Stations are grouped into GRID_DEG squares (0.1 degree, about
11 x 8 km at mid latitudes) and every station in a square shares one request. Nothing about the mesh, the radios or
an antenna's exact position leaves this computer. A moving station uses the square of its latest own GPS fix.

Stored in <data>/weather.db, not mesh.db: it's model data, not something a radio received, and the model revises
recent hours, so rows are replaced (one per square and hour) where mesh.db rows never are.
Weather data by Open-Meteo.com (CC BY 4.0); its free API is for non-commercial use, which this is.
"""
import json
import logging
import math
import sqlite3
import statistics
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

import analytics

log = logging.getLogger("meshdash")

API = "https://api.open-meteo.com/v1/forecast"
ATTRIBUTION = "Weather data by Open-Meteo.com"
GRID_DEG = 0.1
REFRESH_S = 3600            # each square, once an hour
RECENT_DAYS = 3             # a refresh re-reads the last few days: the model firms them up
MAX_PAST_DAYS = 92          # as far back as the forecast API goes
TICK_S = 60
VARS = {                    # Open-Meteo name -> column
    "temperature_2m": "temp_c", "relative_humidity_2m": "rh", "dew_point_2m": "dew_c", "precipitation": "precip_mm",
    "surface_pressure": "pressure_hpa", "wind_speed_10m": "wind_kmh", "cloud_cover": "cloud_pct",
}
COLS = list(VARS.values())
FIT_MIN_HOURS = 48          # a link needs this many hours with both a reading and weather to be judged
FIT_MIN_SPREAD = {"temp_c": 5.0, "rh": 20.0}   # and the weather must have actually varied (10th to 90th percentile)
FOLLOWS_R = 0.4             # |correlation| at which a link "follows" the weather

SCHEMA = """
CREATE TABLE IF NOT EXISTS squares (cell TEXT PRIMARY KEY, lat REAL, lon REAL, elevation REAL, first_ts REAL,
    last_fetch REAL, last_error TEXT, last_error_ts REAL);
CREATE TABLE IF NOT EXISTS hourly (cell TEXT NOT NULL, ts INTEGER NOT NULL, temp_c REAL, rh REAL, dew_c REAL,
    precip_mm REAL, pressure_hpa REAL, wind_kmh REAL, cloud_pct REAL, fetched REAL, PRIMARY KEY (cell, ts));
"""


def cell_of(lat, lon):
    """The grid square a place is in, named by its centre ("45.7,-121.5")."""
    return f"{round(lat / GRID_DEG) * GRID_DEG:.1f},{round(lon / GRID_DEG) * GRID_DEG:.1f}"


def centre(cell):
    lat, lon = cell.split(",")
    return float(lat), float(lon)


def path_for(db_path):
    return Path(db_path).with_name("weather.db")


def _open(path, write=False):
    if write:
        db = sqlite3.connect(path, timeout=30)
        db.executescript(SCHEMA)
        return db
    if not Path(path).exists():
        return None
    return sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True, timeout=30)


def fetch(cell, past_days, opener=urllib.request.urlopen):
    """(elevation, [(hour ts, {col: value})]) for a square from Open-Meteo, hours up to now only."""
    lat, lon = centre(cell)
    qs = urllib.parse.urlencode({"latitude": f"{lat:.1f}", "longitude": f"{lon:.1f}", "hourly": ",".join(VARS),
                                 "past_days": int(past_days), "forecast_days": 1, "timeformat": "unixtime", "timezone": "GMT"})
    req = urllib.request.Request(f"{API}?{qs}", headers={"User-Agent": "Lorakeet (https://github.com/darthnut/lorakeet)"})
    with opener(req, timeout=30) as r:
        d = json.loads(r.read())
    h, now = d["hourly"], time.time()
    rows = [(int(t), {col: h.get(name, [None] * len(h["time"]))[i] for name, col in VARS.items()})
            for i, t in enumerate(h["time"]) if t <= now]
    return d.get("elevation"), rows


def station_squares(db_path, now=None):
    """{cell: [station ids]} for the stations to fetch weather for: fixed ones at their configured or reported location,
    moving ones at their latest own GPS fix (within a day)."""
    now = now or time.time()
    out = {}
    for sid, loc in list(analytics.STATION_LOCATIONS.items()):
        out.setdefault(cell_of(loc[0], loc[1]), []).append(sid)
    if Path(db_path).exists():
        db = sqlite3.connect(f"file:{Path(db_path).as_posix()}?mode=ro", uri=True, timeout=30)
        try:
            for sid, lat, lon in db.execute(
                    "SELECT p.station, p.lat, p.lon FROM positions p JOIN (SELECT station, MAX(ts) AS ts FROM positions "
                    "WHERE source = 'own' AND node = station AND ts > ? AND (precision_bits IS NULL OR precision_bits >= 32) "
                    "GROUP BY station) m ON p.station = m.station AND p.ts = m.ts WHERE p.source = 'own' AND p.node = p.station",
                    (now - 86400,)):
                if sid not in analytics.STATION_LOCATIONS and lat is not None and lon is not None:
                    out.setdefault(cell_of(lat, lon), []).append(sid)
        finally:
            db.close()
    return out


class Weather:
    """The fetch loop: every square with a station, backfilled to when logging began, then refreshed hourly."""

    def __init__(self, db_path, opener=urllib.request.urlopen):
        self.db_path = Path(db_path)
        self.path = path_for(db_path)
        self.opener = opener
        self.failing = set()      # squares whose last fetch failed: logged once per outage

    def start(self):
        threading.Thread(target=self._run, daemon=True, name="weather").start()

    def _run(self):
        while True:
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - this loop must never die
                log.exception("weather loop error")
            time.sleep(TICK_S)

    def _first_logged(self):
        if not self.db_path.exists():
            return time.time()
        db = sqlite3.connect(f"file:{self.db_path.as_posix()}?mode=ro", uri=True, timeout=30)
        try:
            return db.execute("SELECT MIN(ts) FROM packets").fetchone()[0] or time.time()
        finally:
            db.close()

    def tick(self, now=None):
        now = now or time.time()
        squares = station_squares(self.db_path, now)
        db = _open(self.path, write=True)
        try:
            known = {r[0]: r[1] for r in db.execute("SELECT cell, last_fetch FROM squares")}
            for cell in squares:
                last = known.get(cell)
                if last and now - last < REFRESH_S:
                    continue
                past = RECENT_DAYS if last else min(MAX_PAST_DAYS, math.ceil((now - self._first_logged()) / 86400) + 1)
                try:
                    elevation, rows = fetch(cell, past, self.opener)
                except Exception as e:  # noqa: BLE001 - network, HTTP or a changed answer: record, retry next hour
                    if cell not in self.failing:
                        log.warning("weather: couldn't fetch %s: %s", cell, e)
                        self.failing.add(cell)
                    db.execute("INSERT INTO squares (cell, lat, lon, last_fetch, last_error, last_error_ts) VALUES (?, ?, ?, ?, ?, ?) "
                               "ON CONFLICT(cell) DO UPDATE SET last_fetch = excluded.last_fetch, last_error = excluded.last_error, "
                               "last_error_ts = excluded.last_error_ts", (cell, *centre(cell), now, str(e)[:300], now))
                    db.commit()
                    continue
                if cell in self.failing:
                    log.info("weather: %s fetched again", cell)
                    self.failing.discard(cell)
                db.executemany(f"INSERT OR REPLACE INTO hourly (cell, ts, {', '.join(COLS)}, fetched) VALUES "
                               f"(?, ?, {', '.join('?' * len(COLS))}, ?)",
                               [(cell, t, *(v[c] for c in COLS), now) for t, v in rows])
                db.execute("INSERT INTO squares (cell, lat, lon, elevation, first_ts, last_fetch, last_error) VALUES (?, ?, ?, ?, ?, ?, NULL) "
                           "ON CONFLICT(cell) DO UPDATE SET elevation = excluded.elevation, last_fetch = excluded.last_fetch, "
                           "last_error = NULL, first_ts = MIN(COALESCE(squares.first_ts, excluded.first_ts), excluded.first_ts)",
                           (cell, *centre(cell), elevation, rows[0][0] if rows else None, now))
                db.commit()
        finally:
            db.close()


def squares(db_path):
    """Every square fetched so far: [{cell, lat, lon, elevation, firstTs, lastFetch, lastError}]."""
    db = _open(path_for(db_path))
    if db is None:
        return []
    try:
        return [{"cell": c, "lat": la, "lon": lo, "elevation": el, "firstTs": f, "lastFetch": lf, "lastError": le}
                for c, la, lo, el, f, lf, le in db.execute(
                    "SELECT cell, lat, lon, elevation, first_ts, last_fetch, last_error FROM squares ORDER BY cell")]
    finally:
        db.close()


def series(db_path, cell, t0, t1):
    """{hour ts: {col: value}} for a square."""
    db = _open(path_for(db_path))
    if db is None:
        return {}
    try:
        return {r[0]: dict(zip(COLS, r[1:])) for r in db.execute(
            f"SELECT ts, {', '.join(COLS)} FROM hourly WHERE cell = ? AND ts >= ? AND ts <= ? ORDER BY ts", (cell, t0 - 3600, t1))}
    finally:
        db.close()


def at_station(db, db_path, station, t0, t1):
    """{hour ts: weather} where a station was: its fixed square, or for a moving one the square of its latest own fix
    in each hour (hours with no fix within analytics.TRACK_MAX_AGE_S, or in a square never fetched, are left out)."""
    loc = analytics.STATION_LOCATIONS.get(station)
    if loc:
        return series(db_path, cell_of(loc[0], loc[1]), t0, t1)
    track = analytics.station_track(db, station, t0 - analytics.TRACK_MAX_AGE_S, t1)
    if not track:
        return {}
    cache, out, i = {}, {}, 0
    for h in range(int(t0 // 3600) * 3600, int(t1) + 1, 3600):
        while i + 1 < len(track) and track[i + 1][0] <= h + 1800:
            i += 1
        ts, lat, lon, _alt = track[i]
        if ts > h + 1800 or h + 1800 - ts > analytics.TRACK_MAX_AGE_S:
            continue
        c = cell_of(lat, lon)
        if c not in cache:
            cache[c] = series(db_path, c, t0, t1)
        if h in cache[c]:
            out[h] = cache[c][h]
    return out


def _pearson(xs, ys):
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if not sxx or not syy:
        return 0.0, 0.0
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    return sxy / math.sqrt(sxx * syy), sxy / sxx


def _spread(v):
    s = sorted(v)
    return s[int(len(s) * 0.9)] - s[int(len(s) * 0.1)]


def fit(obs, wx):
    """How a link's signal follows the weather: hourly median readings against the station's temperature and humidity.
    {"temp_c": {r, slope, n, spread}, "rh": {...}, "follows": "temp_c" | "rh" | None}, or None without enough of both.
    slope = dB per degree C / per % humidity. A correlation, not a cause: a warm afternoon is also a busy one."""
    by_hour = {}
    for t, s in obs:
        by_hour.setdefault(int(t // 3600) * 3600, []).append(s)
    out = {}
    for col in ("temp_c", "rh"):
        pairs = [(wx[h][col], statistics.median(v)) for h, v in by_hour.items() if h in wx and wx[h].get(col) is not None]
        if len(pairs) < FIT_MIN_HOURS or _spread([p[0] for p in pairs]) < FIT_MIN_SPREAD[col]:
            continue
        r, slope = _pearson([p[0] for p in pairs], [p[1] for p in pairs])
        out[col] = {"r": round(r, 2), "slope": round(slope, 3), "n": len(pairs), "spread": round(_spread([p[0] for p in pairs]), 1)}
    if not out:
        return None
    best = max(out, key=lambda c: abs(out[c]["r"]))
    out["follows"] = best if abs(out[best]["r"]) >= FOLLOWS_R else None
    return out


def summary(db, db_path, stations, describe, t0, t1):
    """The Health tab's card: each square with its stations, the weather now and over [t0, t1]."""
    by_cell = {}
    for sid in stations:
        loc = analytics.STATION_LOCATIONS.get(sid)
        if loc:
            by_cell.setdefault(cell_of(loc[0], loc[1]), []).append(sid)
        else:
            tr = analytics.station_track(db, sid, t1 - 86400, t1)
            if tr:
                by_cell.setdefault(cell_of(tr[-1][1], tr[-1][2]), []).append(sid)
    out = []
    for sq in squares(db_path):
        rows = series(db_path, sq["cell"], t0, t1)
        if not rows and sq["cell"] not in by_cell:
            continue
        hours = sorted(rows)
        now = rows[hours[-1]] if hours else None
        day = [rows[h] for h in hours if h >= t1 - 86400]
        temps = [r["temp_c"] for r in day if r["temp_c"] is not None]
        out.append({**sq, "stations": [{"id": s, "name": describe(s)["name"], "moving": s not in analytics.STATION_LOCATIONS}
                                       for s in by_cell.get(sq["cell"], [])],
                    "now": {"ts": hours[-1], **now} if now else None,
                    "day": {"minC": min(temps), "maxC": max(temps), "precipMm": round(sum(r["precip_mm"] or 0 for r in day), 1)} if temps else None,
                    "series": [{"t": h, "temp": rows[h]["temp_c"], "rh": rows[h]["rh"], "precip": rows[h]["precip_mm"]} for h in hours]})
    return out
