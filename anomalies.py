"""Radios behaving unusually right now, each judged against its own history rather than a fixed threshold.

Every radio Lorakeet hears steadily gets a baseline from the past BASE_DAYS (only time when the station(s) were
logging counts: a gap in logging is never a radio's silence). Four checks, on what's happened lately:

  quiet    silent for longer than it ever usually is: more than QUIET_X times its 99th-percentile gap, QUIET_MED_X
           times its median gap, and at least QUIET_MIN_S, all counted in logged time.
  traffic  packets per logged hour over the last RECENT_S against its daily rates: QUIET_RATIO times fewer or
           BUSY_RATIO times more, AND a robust z-score (median / MAD) of Z_MIN or more.
  relay    how much it relays to your stations (resolved relay bytes), the same way: down by half or up threefold.
  signal   its direct signal at a station over the last RECENT_S against its usual, after taking out what the weather
           explains (a link that follows the station's temperature, weather.py) or else the link's daily pattern
           (linkhistory.daily_pattern): a move of SIGNAL_DB or more with a z-score of Z_MIN or more.

Stations are judged too, from their own radio's reports (noise.py):

  noise    the noise floor at a station over the last RECENT_S against its usual: NOISE_DB or more higher with a
           z-score of Z_MIN or more (everything that station hears loses that much margin).
  restarts the station's radio restarting noise.RESTARTS_WARN times or more in the last day: an absolute rule, not
           against its history (a radio that restarts all the time has a "usual" that's already broken).

Only "steady" radios are judged: heard on at least STEADY_DAYS days and in STEADY_SHARE of the logged hours. A
tracker that comes and goes has no "usual" to break. Phantoms (corruption.py) are left out. Worked out on request
from the log; nothing is stored here (alerts.py remembers which ones it has already announced).
"""
import statistics
import time
from collections import defaultdict
from datetime import datetime

import corruption
import linkhistory
import noise
import radioid
import weather
from analytics import _connect, _coverage, scope_local, station_ids
from topology import relay_resolver

BASE_DAYS = 14
RECENT_S = 24 * 3600
STEADY_DAYS = 4
STEADY_SHARE = 0.4
MIN_PACKETS = 30
QUIET_X, QUIET_MED_X, QUIET_MIN_S = 2.0, 6.0, 3600
QUIET_RATIO, BUSY_RATIO = 3.0, 3.0
RELAY_DROP, RELAY_RISE = 0.5, 3.0
SIGNAL_DB = 6.0
NOISE_DB = 6.0
NOISE_NOTE_DB = 4.0        # a weaker link at a station whose noise rose this much: say it may be the station
WEATHER_R = 0.5            # a link following its station's temperature this closely has it taken out instead
Z_MIN = 4.0
MIN_RECENT_HOURS = 12      # the recent window needs this much logging to be judged at all


def _hour(ts):
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H")


def _robust(vals):
    """(median, spread) with spread = 1.4826 x MAD, at least a small floor so a perfectly steady radio isn't infinitely sensitive."""
    m = statistics.median(vals)
    mad = statistics.median(abs(v - m) for v in vals)
    return m, max(1.4826 * mad, 0.05 * abs(m), 1e-6)


def _logged_seconds(t0, t1, covered):
    """Seconds between t0 and t1 that fell in logged hours."""
    out, t = 0.0, t0
    while t < t1:
        nxt = min(t1, (int(t // 3600) + 1) * 3600)
        if _hour(t) in covered:
            out += nxt - t
        t = nxt
    return out


def _fmt_dur(s):
    return f"{s / 86400:.1f} days" if s >= 2 * 86400 else f"{s / 3600:.1f} h" if s >= 5400 else f"{s / 60:.0f} min"


def detect(db_path, station, describe, now=None):
    """[{kind, severity, node, title, detail, evidence, key}] for the radios behaving unusually now."""
    now = now or time.time()
    db = _connect(db_path, station)
    try:
        local = scope_local(station) or ""
        since = now - BASE_DAYS * 86400 - RECENT_S
        covered = _coverage(db, since, local)
        stations = set(station_ids(db, local))
        q = lambda sql, *a: db.execute(sql, a).fetchall()  # noqa: E731
        heard = defaultdict(list)
        for frm, ts in q("SELECT from_id, ts FROM packets WHERE ts >= ? AND from_id IS NOT NULL ORDER BY ts", since):
            heard[frm].append(ts)
        # relayed-to-us receptions per relayer (resolved relay bytes; a radio not heard yet can't be the relay)
        first = linkhistory.first_heard(db)
        resolvers, relayed = {}, defaultdict(list)
        for ts, relay, at in q("SELECT ts, relay, COALESCE(station, ?) FROM rx_hops WHERE ts >= ? AND hops >= 1 AND relay IS NOT NULL",
                               local, since):
            if at not in resolvers:
                resolvers[at] = relay_resolver(db, at)
            who, _how = resolvers[at](relay)
            if who and who != at and first.get(who, 0) <= ts + 60:
                relayed[who].append(ts)
        direct = defaultdict(list)    # (radio, station) -> [(ts, snr)]
        for ts, a, b, snr in q("SELECT ts, a, b, snr FROM links WHERE source = 'direct' AND ts >= ? AND snr IS NOT NULL", since):
            if b in stations:
                direct[(a, b)].append((ts, snr))
        wx = {st: weather.at_station(db, db_path, st, since, now) for st in {b for _, b in direct}}
        noise_by = {st: noise.station_noise(db, st, since) for st in stations}
        restarts_by = {st: noise.station_restarts(db, st, now - 86400) for st in stations}
    finally:
        db.close()

    phantoms = corruption.flags(db_path)["senders"]
    recent0 = now - RECENT_S
    base_hours = {h for h in covered if _hour(since) <= h < _hour(recent0)}
    recent_hours = {h for h in covered if h >= _hour(recent0)}
    if len(base_hours) < STEADY_DAYS * 24 * STEADY_SHARE:
        return []
    days = sorted({h[:10] for h in base_hours})
    hours_per_day = {d: sum(1 for h in base_hours if h.startswith(d)) for d in days}
    full_days = [d for d in days if hours_per_day[d] >= 12]
    out = []

    def add(kind, sev, nid, title, detail, evidence):
        out.append({"kind": kind, "severity": sev, "node": {"id": nid, "name": describe(nid)["name"]}, "title": title,
                    "detail": detail, "evidence": evidence, "key": f"{kind}:{nid}"})

    def daily_rates(ts_list):
        per = defaultdict(int)
        for t in ts_list:
            if t < recent0:
                per[_hour(t)[:10]] += 1
        return [per.get(d, 0) / hours_per_day[d] for d in full_days]

    for nid, ts in heard.items():
        if nid in stations or corruption.phantom(phantoms.get(nid)) or not radioid.is_radio_id(nid):
            continue
        base = [t for t in ts if t < recent0]
        hours_heard = {_hour(t) for t in base} & base_hours
        if (len(base) < MIN_PACKETS or len({h[:10] for h in hours_heard}) < STEADY_DAYS
                or len(hours_heard) < STEADY_SHARE * len(base_hours)):
            continue    # not steady enough to have a "usual"
        # gaps between packets, in logged time (a gap across a logging outage is the logger's, not the radio's)
        gaps = [_logged_seconds(a, b, covered) for a, b in zip(ts, ts[1:])]
        gaps = sorted(g for g in gaps if g > 0)
        if len(gaps) >= 20:
            p99, med = gaps[min(len(gaps) - 1, int(len(gaps) * 0.99))], statistics.median(gaps)
            limit = max(QUIET_X * p99, QUIET_MED_X * med, QUIET_MIN_S)
            silent = _logged_seconds(ts[-1], now, covered)
            if silent > limit:
                add("quiet", "warn", nid, "Unusually quiet",
                    f"Usually heard every {_fmt_dur(med)} (its longest usual gap: {_fmt_dur(p99)}), but silent for "
                    f"{_fmt_dur(silent)} of logging, since {time.strftime('%b %d %H:%M', time.localtime(ts[-1]))}.",
                    [{"label": "silent", "value": _fmt_dur(silent), "prov": "observed"},
                     {"label": "usual gap", "value": _fmt_dur(med), "prov": "inferred"}])
                continue    # silent: the traffic and relay checks would only say the same thing
        if len(recent_hours) >= MIN_RECENT_HOURS and len(full_days) >= STEADY_DAYS:
            rates = daily_rates(ts)
            m, s = _robust(rates)
            n = sum(1 for t in ts if t >= recent0)
            r = n / len(recent_hours)
            z = (r - m) / s
            if m > 0 and r * QUIET_RATIO <= m and z <= -Z_MIN and m * len(recent_hours) >= 10:
                add("traffic", "warn", nid, "Sending much less than usual",
                    (f"Not heard at all in the last day of logging, against its usual {m:.1f} packets an hour." if n == 0 else
                     f"Heard {n} time{'s' if n != 1 else ''} in the last {len(recent_hours)} logged hours, against its usual "
                     f"{m:.1f} packets an hour." if r < 0.1 else
                     f"{r:.1f} packets per logged hour lately against its usual {m:.1f}."),
                    [{"label": "lately", "value": f"{r:.1f}/h", "prov": "observed"}, {"label": "usual", "value": f"{m:.1f}/h", "prov": "inferred"}])
            elif r >= BUSY_RATIO * max(m, 0.1) and z >= Z_MIN and r * len(recent_hours) >= 20:
                add("traffic", "info", nid, "Sending much more than usual",
                    f"{r:.1f} packets per logged hour lately against its usual {m:.1f}: a changed setting, or a busy event.",
                    [{"label": "lately", "value": f"{r:.1f}/h", "prov": "observed"}, {"label": "usual", "value": f"{m:.1f}/h", "prov": "inferred"}])
    # relaying: radios that pass a steady stream of others' packets to your stations
    if len(recent_hours) >= MIN_RECENT_HOURS and len(full_days) >= STEADY_DAYS:
        quiet = {a["node"]["id"] for a in out if a["kind"] == "quiet"}
        for nid, ts in relayed.items():
            if nid in stations or nid in quiet:
                continue
            rates = daily_rates(ts)
            if sum(1 for x in rates if x > 0) < STEADY_DAYS:
                continue
            m, s = _robust(rates)
            r = sum(1 for t in ts if t >= recent0) / len(recent_hours)
            z = (r - m) / s
            if m * 24 >= 20 and r <= RELAY_DROP * m and z <= -Z_MIN:
                add("relay", "warn", nid, "Relaying much less than usual",
                    f"Passes {r:.1f} packets an hour to your stations lately, against its usual {m:.1f}: it may have moved, "
                    "lost power or changed role, or a link to it weakened.",
                    [{"label": "lately", "value": f"{r:.1f}/h", "prov": "inferred"}, {"label": "usual", "value": f"{m:.1f}/h", "prov": "inferred"}])
            elif m * 24 >= 10 and r >= RELAY_RISE * m and z >= Z_MIN and r * len(recent_hours) >= 30:
                # (only a radio that usually relays: one that normally passes nothing "rising" is a visitor)
                add("relay", "info", nid, "Relaying much more than usual",
                    f"Passes {r:.1f} packets an hour to your stations lately, against its usual {m:.1f}: another route may be down.",
                    [{"label": "lately", "value": f"{r:.1f}/h", "prov": "inferred"}, {"label": "usual", "value": f"{m:.1f}/h", "prov": "inferred"}])
    # direct signal at a station, with the link's daily pattern taken out
    for (nid, st), obs in direct.items():
        if corruption.phantom(phantoms.get(nid)):
            continue
        base = [(t, s) for t, s in obs if t < recent0]
        recent = [(t, s) for t, s in obs if t >= recent0]
        if len(base) < 30 or len(recent) < 5 or len({_hour(t)[:10] for t, _ in base}) < STEADY_DAYS:
            continue
        temp = {h: w["temp_c"] for h, w in wx.get(st, {}).items() if w.get("temp_c") is not None}
        fit = weather.fit(base, wx[st]) if temp else None
        tf = fit and fit.get("temp_c")
        allowing, b_adj, r_adj = "", None, None
        if tf and abs(tf["r"]) >= WEATHER_R:
            # take out what the temperature explains: dB per degree x how far each hour was from the usual temperature
            hr = lambda t: int(t // 3600) * 3600  # noqa: E731
            t_usual = statistics.fmean(temp[hr(t)] for t, _ in base if hr(t) in temp)
            b_adj = [x - tf["slope"] * (temp[hr(t)] - t_usual) for t, x in base if hr(t) in temp]
            r_adj = [x - tf["slope"] * (temp[hr(t)] - t_usual) for t, x in recent if hr(t) in temp]
            allowing = " (allowing for the temperature, which this link follows)"
        if not r_adj or len(r_adj) < 5:
            pattern = linkhistory.daily_pattern(base)
            b_adj = [x for _, x in linkhistory.deseasoned(base, pattern)]
            r_adj = [x for _, x in linkhistory.deseasoned(recent, pattern)]
            allowing = " (allowing for its daily pattern)" if pattern else ""
        m, s = _robust(b_adj)
        s = max(s, 1.0)    # SNR comes in quarter-dB steps: never call less than ~1 dB of spread "steady"
        r = statistics.median(r_adj)
        z = (r - m) / s
        sname = describe(st)["name"]
        lately = [temp[h] for h in temp if h >= recent0 - 3600]
        usual = [temp[h] for h in temp if h < recent0]
        wx_ev = ([{"label": "temperature lately", "value": f"{statistics.fmean(lately):.0f} °C", "prov": "reported"},
                  {"label": "usual", "value": f"{statistics.fmean(usual):.0f} °C", "prov": "reported"}]
                 if lately and usual else [])
        nb = [v for t, v in noise_by.get(st, []) if t < recent0]
        nr = [v for t, v in noise_by.get(st, []) if t >= recent0]
        rise = statistics.median(nr) - statistics.median(nb) if len(nb) >= 20 and len(nr) >= 4 else 0
        if r - m <= -SIGNAL_DB and z <= -Z_MIN:
            add("signal", "warn", nid, "Weaker signal than usual",
                f"Heard directly at {sname} at {r:.1f} dB lately against its usual {m:.1f} dB{allowing}: "
                + (f"{sname}'s own noise floor rose {rise:.0f} dB too, so it may be noise at the station rather than the link."
                   if rise >= NOISE_NOTE_DB else "an antenna, a move, or something in the way."),
                [{"label": "lately", "value": f"{r:.1f} dB", "prov": "observed"}, {"label": "usual", "value": f"{m:.1f} dB", "prov": "inferred"},
                 {"label": "station", "value": sname, "prov": "observed"}, *wx_ev])
            out[-1]["key"] = f"signal:{nid}:{st}"
        elif r - m >= SIGNAL_DB and z >= Z_MIN:
            add("signal", "info", nid, "Stronger signal than usual",
                f"Heard directly at {sname} at {r:.1f} dB lately against its usual {m:.1f} dB.",
                [{"label": "lately", "value": f"{r:.1f} dB", "prov": "observed"}, {"label": "usual", "value": f"{m:.1f} dB", "prov": "inferred"},
                 {"label": "station", "value": sname, "prov": "observed"}])
            out[-1]["key"] = f"signal:{nid}:{st}"
    # the stations themselves: noise at the antenna, and a radio that keeps restarting
    for st in stations:
        sname = describe(st)["name"]
        obs = noise_by.get(st, [])
        nb = [v for t, v in obs if t < recent0]
        nr = [v for t, v in obs if t >= recent0]
        if len(nb) >= 100 and len(nr) >= 12 and len({_hour(t)[:10] for t, _ in obs if t < recent0}) >= STEADY_DAYS:
            # dBm: _robust's floor of 5% of the median would be ~4.5 dB here, so the spread is the MAD alone, 1 dB at least
            m = statistics.median(nb)
            s = max(1.4826 * statistics.median(abs(v - m) for v in nb), 1.0)
            r = statistics.median(nr)
            if r - m >= NOISE_DB and (r - m) / s >= Z_MIN:
                add("noise", "warn", st, "Noisier than usual",
                    f"The noise floor at {sname} is {r:.0f} dBm lately against its usual {m:.0f} dBm: every radio it hears "
                    f"has about {r - m:.0f} dB less margin. Something new near the antenna (a power supply, a USB 3 device, "
                    "a computer) is the usual cause.",
                    [{"label": "lately", "value": f"{r:.0f} dBm", "prov": "reported"}, {"label": "usual", "value": f"{m:.0f} dBm", "prov": "inferred"}])
        rs = restarts_by.get(st, [])
        if len(rs) >= noise.RESTARTS_WARN:
            add("restarts", "warn", st, "Radio keeps restarting",
                f"{sname}'s radio restarted {len(rs)} times in the last day, last at {time.strftime('%H:%M', time.localtime(rs[-1]))}. "
                "Power is the usual cause: a USB port cutting out on over-current (a Pi 5 on a 3 A supply allows 600 mA "
                "for all its USB ports), a weak supply or a poor cable. Each restart loses a little logging.",
                [{"label": "restarts (24 h)", "value": str(len(rs)), "prov": "reported"}])
    out.sort(key=lambda a: (a["severity"] != "warn", a["kind"], a["node"]["name"].lower()))
    return out
