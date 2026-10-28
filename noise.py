"""The radio noise at each listening station, the packets it costs, and radios that keep restarting.

A station hears a packet only if it stands far enough above the noise at its antenna, so the noise floor sets every
link's margin: 10 dB more noise is 10 dB less range, for every radio that station hears. Noise is often local (a
Pi's USB 3 port, a switching power supply, a PC), so it's worth watching per station, against its own history.

Everything comes from what the stations' own radios report (never stored separately, worked out on request):
  noise floor   Meshtastic localStats `noiseFloor` (dBm, about every 15 min) and MeshCore `noise_floor`
                (meshcore_stats_radio, every 5 min). A reading at or below NOT_MEASURED_DBM is the firmware's value
                before its first sample (seen in the first ~45 min after a boot) and is skipped.
  failed        receptions that failed their checksum: localStats numPacketsRxBad against numPacketsRx, MeshCore
                recv_errors; both are counters since boot, so only increases between two reports of one boot count.
  restarts      the radio's uptime going backwards between reports: a reboot, a crash, or power lost (a Pi's USB port
                cutting out on over-current did it ~45 times a day to one station's radio).
Noise floors from different radio models aren't directly comparable (each chip measures in its own way); a station
against itself is.
"""
import json
import statistics
import time
from collections import defaultdict
from datetime import datetime

import linkhistory
from analytics import RANGES, _connect, _coverage, scope_local, station_ids

NOT_MEASURED_DBM = -119      # -120 = no sample yet
SPELL_DB = 6.0               # a noisy spell: this far above the station's usual
SPELL_MIN_S = 30 * 60        #   for at least this long
RESTARTS_WARN = 8            # radio restarts in a day that are never normal


def _readings(db, station):
    """[(ts, noise dBm | None, bad, rx, uptime_s)] from a station radio's own reports, oldest first."""
    out = []
    for ts, kind, data in db.execute(
            "SELECT ts, kind, data FROM telemetry_full WHERE node = ? AND kind IN "
            "('localStats', 'meshcore_stats_radio', 'meshcore_stats_packets', 'meshcore_stats_core') ORDER BY ts", (station,)):
        try:
            d = json.loads(data or "{}")
        except ValueError:
            continue
        if kind == "localStats":
            nf = d.get("noiseFloor")
            out.append((ts, nf if isinstance(nf, (int, float)) and nf > NOT_MEASURED_DBM else None,
                        d.get("numPacketsRxBad"), d.get("numPacketsRx"), d.get("uptimeSeconds")))
        elif kind == "meshcore_stats_radio":
            nf = d.get("noise_floor")
            out.append((ts, nf if isinstance(nf, (int, float)) and nf > NOT_MEASURED_DBM else None, None, None, None))
        elif kind == "meshcore_stats_packets":
            out.append((ts, None, d.get("recv_errors"), d.get("recv"), None))
        elif kind == "meshcore_stats_core":
            out.append((ts, None, None, None, d.get("uptime_secs")))
    return out


def _restarts(rows):
    """Times the radio's uptime went backwards."""
    out, last = [], None
    for ts, _nf, _b, _r, up in rows:
        if up is None:
            continue
        if last is not None and up + 60 < last:
            out.append(ts)
        last = up
    return out


def _failed(rows):
    """[(ts, bad, received)] increases between consecutive reports of one boot."""
    out, last = [], None
    for ts, _nf, bad, rx, up in rows:
        if bad is None or rx is None:
            continue
        if last is not None and bad >= last[0] and rx >= last[1] and (up is None or last[2] is None or up >= last[2]):
            out.append((ts, bad - last[0], rx - last[1]))
        last = (bad, rx, up)
    return out


def _spells(obs, usual):
    """Runs of readings SPELL_DB or more above usual lasting SPELL_MIN_S: [{start, end, peak}]."""
    out, cur = [], None
    for ts, v in obs:
        if v >= usual + SPELL_DB:
            if cur and ts - cur["end"] <= 3600:
                cur["end"], cur["peak"] = ts, max(cur["peak"], v)
            else:
                cur = {"start": ts, "end": ts, "peak": v}
                out.append(cur)
        elif cur and ts - cur["end"] > 3600:
            cur = None
    return [dict(s, peak=round(s["peak"], 1), above=round(s["peak"] - usual, 1)) for s in out if s["end"] - s["start"] >= SPELL_MIN_S]


def report(db_path, range_key, station, describe):
    """Per station in view: noise floor over the range, its daily shape, steps, noisy spells, failed receptions and
    radio restarts."""
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
        events = linkhistory.station_events(db, since, until)
        meta = {}
        try:
            meta = json.loads((db.execute("SELECT value FROM main.settings WHERE key = 'station_meta'").fetchone() or ["{}"])[0])
        except ValueError:
            pass
        out = []
        for st in station_ids(db, local):
            rows = _readings(db, st)
            restarts = _restarts(rows)
            inr = [r for r in rows if r[0] >= since]
            obs = [(r[0], float(r[1])) for r in inr if r[1] is not None]
            fails = [f for f in _failed(rows) if f[0] >= since]
            if not obs and not fails:
                continue
            by_b = defaultdict(list)
            for t, v in obs:
                by_b[datetime.fromtimestamp(t).strftime(fmt)].append(v)
            fail_b = defaultdict(lambda: [0, 0])
            for t, b, r in fails:
                k = datetime.fromtimestamp(t).strftime(fmt)
                fail_b[k][0] += b
                fail_b[k][1] += r
            series = []
            for start in linkhistory._starts(since, until, bucket):
                k = start.strftime(fmt)
                v = sorted(by_b.get(k, []))
                fb, fr = fail_b.get(k, (0, 0))
                series.append({"t": start.timestamp(), "covered": k in covered,
                               **({"med": statistics.median(v), "lo": v[len(v) // 10], "hi": v[(len(v) * 9) // 10], "n": len(v)} if v else {}),
                               **({"failedPct": round(100 * fb / (fb + fr), 1)} if fr else {})})
            vals = sorted(v for _, v in obs)
            med = statistics.median(vals) if vals else None
            mine = [e for e in events if e["station"] == st]
            bad, got = sum(f[1] for f in fails), sum(f[2] for f in fails)
            day_restarts = [t for t in restarts if t >= until - 86400]
            hw = (meta.get(st) or {}).get("radioHw") or describe(st).get("hw")
            out.append({
                "station": st, "name": describe(st)["name"], "hw": hw,
                "noise": {"med": med, "lo": vals[len(vals) // 10], "hi": vals[(len(vals) * 9) // 10], "n": len(vals)} if vals else None,
                "daily": linkhistory.daily_pattern(obs) if obs else None,
                "steps": linkhistory.find_steps(obs, [e["ts"] for e in mine], mine) if obs else [],   # 5 dB or more
                "spells": _spells(obs, med)[-20:] if obs else [],
                # MeshCore's "recv" counter stays 0 on some firmware: then only the errors are known, not a share
                "failed": {"bad": bad, "received": got, "pct": round(100 * bad / (bad + got), 1) if got else None} if bad + got else None,
                "restarts": {"inRange": sum(1 for t in restarts if t >= since), "lastDay": len(day_restarts),
                             "last": restarts[-1] if restarts else None},
                "series": series,
            })
        out.sort(key=lambda s: (-(s["restarts"]["lastDay"] >= RESTARTS_WARN), -(s["noise"] or {}).get("med", -999)))
        return {"range": range_key, "since": since, "until": until, "bucket": bucket, "stations": out,
                "thresholds": {"spellDb": SPELL_DB, "stepDb": linkhistory.STEP_DB, "restartsWarn": RESTARTS_WARN}}
    finally:
        db.close()


def station_noise(db, station, since):
    """[(ts, dBm)] noise floor readings of one station from `since` (for anomalies.py)."""
    return [(r[0], float(r[1])) for r in _readings(db, station) if r[1] is not None and r[0] >= since]


def station_restarts(db, station, since):
    return [t for t in _restarts(_readings(db, station)) if t >= since]
