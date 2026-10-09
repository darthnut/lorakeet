"""Passive analyses over mesh.db: position estimates, mesh health + security findings, coverage, traceroutes.

Nothing here transmits. Every value carries its provenance so the UI can label it:
  reported  - a field the protocol/firmware/node sent us
  observed  - something this dashboard measured or recorded (timestamps, counts, our radio's SNR)
  inferred  - computed from other values (estimates, derived rates, relay-byte attribution)
"""
import json
import math
import statistics
import time
from collections import Counter, defaultdict

import keyflags
import topology
import weak_keys
from analytics import STATION_LOCATIONS, _connect, _window, scope_local

# ------------------------------------------------------------------ helpers


def _latest_positions(q):
    """Latest reported position per node: {id: (lat, lon, precision_bits, ts)}."""
    return {r["node"]: (r["lat"], r["lon"], r["precision_bits"], r["ts"])
            for r in q("SELECT node, lat, lon, precision_bits, MAX(ts) AS ts FROM positions GROUP BY node")}


def precision_km(bits):
    """Half-width of the box a truncated position falls in (0 for full precision)."""
    return 0.0 if not bits or bits >= 32 else 23.3 * 2 ** (10 - bits) / 2


def _haversine_km(a, b):
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * 6371 * math.asin(math.sqrt(h))


def _base_anchor(positions, local, base_id):
    """Our radio has no GPS; it sits in the house with the base station. Use the base's reported position."""
    if local and local not in positions and base_id in positions:
        return {local: positions[base_id]}
    return {}


# ------------------------------------------------------------------ position estimation

SINGLE_ANCHOR_KM = 5.0   # one anchor can only say "somewhere within radio range"
MIN_RADIUS_KM = 1.0
RECENCY_DAYS = 7.0

# From drives (the way wardriving tools place Wi-Fi access points): a moving station's DIRECT receptions of a
# radio, each placed at the station's GPS fix (within DRIVE_FIX_S), grouped into ~200 m spots so a long stop is
# one vantage point, not hundreds. Strict, or the answer is a confident-looking point between two parking spots:
DRIVE_FIX_S = 60          # a reception counts only with an exact own fix this close in time
DRIVE_MIN_SPOTS = 3       # heard directly from at least this many different spots...
DRIVE_MIN_SPREAD_KM = 1.0  # ...spread over at least this distance
DRIVE_MIN_RADIUS_KM = 0.5


def drive_estimates(db_path, exclude=()):
    """Positions for radios heard directly from enough places along moving stations' routes:
    {node: {"lat", "lon", "radiusKm", "spots", "receptions", "spreadKm"}}. Signal-weighted centroid of the spots
    (stronger = closer: weight 10^(median SNR / 20)); radius = weighted spread / sqrt(effective spot count),
    at least DRIVE_MIN_RADIUS_KM. It leans toward the roads driven: a car only listens from where it drove."""
    import bisect
    from collections import defaultdict as dd
    from analytics import ALL_STATIONS, smooth_track
    db = _connect(db_path, ALL_STATIONS)
    try:
        exact = "source = 'own' AND node = station AND (precision_bits IS NULL OR precision_bits >= 32)"
        movers = [r[0] for r in db.execute(f"SELECT DISTINCT node FROM positions WHERE {exact}")]
        stations = set(movers) | {r[0] for r in db.execute("SELECT id FROM _stations")} | set(exclude)
        spots = dd(lambda: dd(list))  # node -> spot -> [(lat, lon, snr)]
        for st in movers:
            fx = smooth_track([tuple(r) for r in db.execute(f"SELECT ts, lat, lon FROM positions WHERE {exact} AND node = ? ORDER BY ts", (st,))])
            fts = [f[0] for f in fx]
            for ts, frm, snr in db.execute("SELECT ts, from_id, snr FROM rx_hops WHERE station = ? AND hops = 0 AND snr IS NOT NULL", (st,)):
                if frm in stations:
                    continue
                i = bisect.bisect_right(fts, ts) - 1
                if i < 0 or ts - fts[i] > DRIVE_FIX_S:
                    continue
                lat, lon = fx[i][1], fx[i][2]
                spots[frm][(round(lat / 0.002), round(lon / 0.003))].append((lat, lon, snr))
    finally:
        db.close()
    out = {}
    for nid, by_spot in spots.items():
        pts = []
        for obs in by_spot.values():
            lat = sum(o[0] for o in obs) / len(obs); lon = sum(o[1] for o in obs) / len(obs)
            pts.append((lat, lon, 10 ** (statistics.median(o[2] for o in obs) / 20), len(obs)))
        if len(pts) < DRIVE_MIN_SPOTS:
            continue
        spread = max(_haversine_km(a[:2], b[:2]) for a in pts for b in pts)
        if spread < DRIVE_MIN_SPREAD_KM:
            continue
        tw = sum(p[2] for p in pts)
        lat = sum(p[0] * p[2] for p in pts) / tw; lon = sum(p[1] * p[2] for p in pts) / tw
        rms = math.sqrt(sum(p[2] * _haversine_km((lat, lon), p[:2]) ** 2 for p in pts) / tw)
        n_eff = tw ** 2 / sum(p[2] ** 2 for p in pts)
        out[nid] = {"lat": lat, "lon": lon, "radiusKm": max(DRIVE_MIN_RADIUS_KM, rms / math.sqrt(n_eff)),
                    "spots": len(pts), "receptions": sum(p[3] for p in pts), "spreadKm": round(spread, 2)}
    return out


def estimate_positions(db_path, local, base_id, describe):
    """Best-guess positions for nodes that never reported one, from observed RF links to positioned nodes.

    Weighted centroid of positioned neighbours: weight = signal (higher SNR ~ closer) x how often the link
    was seen x recency. Uncertainty = weighted spread of the anchors / sqrt(effective anchor count), or a
    flat 5 km for a single anchor, plus the anchors' own position-precision boxes.
    """
    db = _connect(db_path, local)
    local = scope_local(local)
    try:
        q = lambda sql, *a: db.execute(sql, a).fetchall()  # noqa: E731
        positions = _latest_positions(q)
        # listening stations with a configured location anchor estimates like any positioned node (exact:
        # no precision blur); our radio falls back to the base station's spot only if it has no location set
        stations = {sid: (loc[0], loc[1], None) for sid, loc in STATION_LOCATIONS.items() if sid not in positions}
        anchors = {**positions, **(_base_anchor(positions, local, base_id) if local not in stations else {}), **stations}
        topo = topology._compute(db, "30d", local, describe)
    finally:
        db.close()
    now = time.time()
    obs = defaultdict(list)  # unpositioned node -> [(anchor id, weight, snr)]
    for e in topo["edges"]:
        for me, other in ((e["a"], e["b"]), (e["b"], e["a"])):
            if me in anchors or other not in anchors:
                continue
            snr = e["snr"]
            w_snr = 0.3 if snr is None else min(1.0, max(0.1, (snr + 20) / 30))
            w_cnt = math.log2(1 + e["count"])
            w_age = math.exp(-max(0, now - (e["last"] or now)) / (RECENCY_DAYS * 86400))
            obs[me].append((other, w_snr * w_cnt * w_age, snr, e["measured"]))
    out = []
    drives = drive_estimates(db_path)
    for nid, e in drives.items():  # heard directly from enough places along drives: better than link partners
        if nid in positions:
            continue
        out.append({"id": nid, **describe(nid), "lat": e["lat"], "lon": e["lon"], "radiusKm": e["radiusKm"], "anchors": [],
                    "provenance": "inferred", "fromDrives": e,
                    "method": f"heard directly from {e['spots']} spots along drives ({e['receptions']} receptions, "
                              f"{e['spreadKm']:.1f} km apart), signal-weighted; leans toward the roads driven"})
    for nid, items in obs.items():
        if nid in drives:
            continue
        tw = sum(w for _, w, _, _ in items)
        if tw <= 0:
            continue
        lat = sum(anchors[a][0] * w for a, w, _, _ in items) / tw
        lon = sum(anchors[a][1] * w for a, w, _, _ in items) / tw
        prec = max(precision_km(anchors[a][2]) for a, _, _, _ in items)
        if len(items) == 1:
            radius = SINGLE_ANCHOR_KM
        else:
            spread = math.sqrt(sum(w * _haversine_km((lat, lon), anchors[a][:2]) ** 2 for a, w, _, _ in items) / tw)
            n_eff = tw ** 2 / sum(w * w for _, w, _, _ in items)
            radius = max(MIN_RADIUS_KM, spread / math.sqrt(n_eff))
        out.append({
            "id": nid, **describe(nid), "lat": lat, "lon": lon, "radiusKm": radius + prec,
            "anchors": [{"id": a, "name": describe(a)["name"], "weight": round(w / tw, 3), "snr": s, "measured": m,
                         "assumed": a == local and a not in positions} for a, w, s, m in
                        sorted(items, key=lambda x: -x[1])],
            "provenance": "inferred",
            "method": "single anchor: within radio range" if len(items) == 1 else f"{len(items)} anchors, weighted centroid",
        })
    return {"estimates": sorted(out, key=lambda x: x["radiusKm"]),
            "assumptions": ([f"Our radio ({local}) has no GPS and no station location set, so it's placed at the base "
                             "station's reported position."]
                            if local and local not in positions and local not in stations and base_id in positions else [])
            + ([f"{len(stations)} listening station{'s' if len(stations) != 1 else ''} placed at {'their' if len(stations) != 1 else 'its'} configured location."]
               if stations else [])}


# ------------------------------------------------------------------ mesh health

# Expected cadences (Meshtastic defaults) for the "too chatty" check.
DEFAULT_INTERVALS = {"POSITION_APP": (900, "position (default 15 min)"),
                     "TELEMETRY_APP": (1800, "telemetry (default 30 min)"),
                     "NODEINFO_APP": (10800, "node info (default 3 h)")}
CHATTY_FACTOR = 0.33          # flag when the median interval is under a third of the default
AIRTIME_WARN = 10.0           # % TX airtime per node
CHANNEL_UTIL_WARN = 25.0      # % our radio measures; firmware starts deferring traffic around here
SNR_DROP_DB = 5.0             # degrading link threshold
EDGE_SHARE = 0.5              # "no hops to spare": at least this share of a radio's packets arrived on their last hop
EDGE_MIN_PACKETS = 5
ROUTER_ROLES = {"ROUTER", "ROUTER_LATE", "REPEATER", "ROUTER_CLIENT"}


def health(db_path, range_key, local, describe):
    db = _connect(db_path, local)
    local = scope_local(local) or ""
    try:
        return _health(db, range_key, local, describe)
    finally:
        db.close()


def _finding(sev, kind, title, detail, node=None, evidence=None, describe=None):
    f = {"severity": sev, "kind": kind, "title": title, "detail": detail, "evidence": evidence or []}
    if node:
        f["node"] = {"id": node, "name": describe(node)["name"]} if describe else {"id": node}
    f["key"] = f"{kind}:{node or ''}"
    return f


def _health(db, range_key, local, describe):
    since, until, *_ = _window(db, range_key)
    q = lambda sql, *a: db.execute(sql, a).fetchall()  # noqa: E731
    findings = []

    # 1. channel utilization measured by our radio, from its once-a-minute deviceMetrics (localStats carries
    #    it only every ~15 min; switched 2026-10-06, same as the overview chart and the airtime card)
    cu = [r[0] for r in q("SELECT json_extract(data, '$.channelUtilization') FROM telemetry_full "
                          "WHERE kind='deviceMetrics' AND node=? AND ts>=?", local, since) if r[0] is not None]
    if cu:
        avg, peak = statistics.fmean(cu), max(cu)
        if avg >= CHANNEL_UTIL_WARN or peak >= 40:
            findings.append(_finding("warn", "channel-busy", "The channel is busy",
                                     f"Average utilization {avg:.1f}%, peak {peak:.1f}%. Meshtastic firmware starts "
                                     f"deferring non-essential traffic around {CHANNEL_UTIL_WARN:.0f}%.",
                                     evidence=[{"label": "avg channel utilization", "value": f"{avg:.1f}%", "prov": "reported"},
                                               {"label": "peak (one-minute window)", "value": f"{peak:.1f}%", "prov": "reported"},
                                               {"label": "per-minute readings", "value": len(cu), "prov": "observed"}]))

    # 2. per-node TX airtime (from each node's own device telemetry)
    for r in q("""SELECT node, AVG(json_extract(data, '$.airUtilTx')) AS air, MAX(json_extract(data, '$.airUtilTx')) AS peak,
                         COUNT(*) AS n FROM telemetry_full WHERE kind='deviceMetrics' AND ts>=? AND node != ?
                  GROUP BY node""", since, local):
        if r["air"] is not None and r["air"] >= AIRTIME_WARN:
            findings.append(_finding("warn", "airtime-hog", "Uses a lot of airtime",
                                     f"Reports {r['air']:.1f}% of its time transmitting on average (peak {r['peak']:.1f}%). "
                                     f"We flag above {AIRTIME_WARN:.0f}%.", r["node"],
                                     [{"label": "avg airUtilTx", "value": f"{r['air']:.1f}%", "prov": "reported"},
                                      {"label": "telemetry reports", "value": r["n"], "prov": "observed"}], describe))

    # 3. broadcasting more often than the defaults
    per = defaultdict(list)
    for r in q("SELECT from_id, portnum, ts FROM packets WHERE ts>=? AND from_id != ? AND portnum IN (?,?,?) "
               "AND to_id IN ('^all','!ffffffff') ORDER BY ts", since, local, *DEFAULT_INTERVALS):
        per[(r["from_id"], r["portnum"])].append(r["ts"])
    for (nid, port), ts in per.items():
        if len(ts) < 4:
            continue
        med = statistics.median(b - a for a, b in zip(ts, ts[1:]))
        default, label = DEFAULT_INTERVALS[port]
        if med < default * CHATTY_FACTOR:
            findings.append(_finding("warn", f"chatty-{port}", f"Broadcasts {label.split(' ')[0]} very often",
                                     f"Median {med / 60:.1f} min between {label} broadcasts over {len(ts)} packets.",
                                     nid, [{"label": "median interval", "value": f"{med / 60:.1f} min", "prov": "inferred"},
                                           {"label": "packets", "value": len(ts), "prov": "observed"}], describe))

    # 4. high starting hop limit (floods further than the default 3)
    for r in q("""SELECT from_id, MAX(hop_start) AS hs, COUNT(DISTINCT pkt_id) AS n FROM rx_hops
                  WHERE ts>=? AND from_id != ? AND hop_start IS NOT NULL GROUP BY from_id""", since, local):
        if r["hs"] and r["hs"] > 3:
            findings.append(_finding("info" if r["hs"] <= 4 else "warn", "hop-limit", f"Sends with hop limit {r['hs']}",
                                     f"Packets start with {r['hs']} hops (default 3), so each one is relayed further "
                                     "and uses more of everyone's airtime.", r["from_id"],
                                     [{"label": "max hop_start", "value": r["hs"], "prov": "reported"},
                                      {"label": "packets", "value": r["n"], "prov": "observed"}], describe))

    # 4b. at the edge of reach: packets that only just made it (their best copy arrived with no hops left). The ones
    # that needed one more hop never arrived at all, so this radio's delivery to us is hit-or-miss.
    edge = defaultdict(lambda: [0, 0, 0])  # node -> [packets, arrived with 0 hops left, highest hop_start]
    for r in q("""SELECT from_id, MAX(hop_limit) AS left, MAX(hop_start) AS hs FROM rx_hops
                  WHERE ts>=? AND from_id != ? AND hop_start > 0 AND hop_limit IS NOT NULL
                  GROUP BY from_id, pkt_id""", since, local):
        e = edge[r["from_id"]]
        e[0] += 1
        e[1] += r["left"] == 0
        e[2] = max(e[2], r["hs"] or 0)
    for nid, (n, at_edge, hs) in edge.items():
        if n >= EDGE_MIN_PACKETS and at_edge / n >= EDGE_SHARE:
            fix = ("It already starts at the maximum of 7, so only a better-placed relay would help." if hs >= 7 else
                   f"Raising its hop limit from {hs} to {hs + 1}, or a relay closer to it, would make it reliable.")
            findings.append(_finding("info", "hops-edge", "Reaches us with no hops to spare",
                                     f"{at_edge} of its {n} packets arrived on their very last hop, so the ones that "
                                     f"needed one more hop never arrived: delivery from it to here is hit-or-miss. {fix}",
                                     nid, [{"label": "on their last hop", "value": f"{round(100 * at_edge / n)} %", "prov": "observed"},
                                           {"label": "packets", "value": n, "prov": "observed"},
                                           {"label": "hop limit", "value": hs, "prov": "reported"}], describe))

    # 5. router-role nodes we never see relaying
    relay_bytes = Counter(r[0] for r in q("SELECT relay FROM rx_hops WHERE ts>=? AND hops>=1 AND relay IS NOT NULL", since))
    relay_bytes.update(r[0] for r in q("SELECT relay FROM packets WHERE ts>=? AND hops>=1 AND relay IS NOT NULL", since))
    roles = {r["node"]: r["role"] for r in q("SELECT node, role, MAX(ts) FROM node_info GROUP BY node")}
    heard = {r[0]: r[1] for r in q("SELECT from_id, COUNT(*) FROM packets WHERE ts>=? GROUP BY 1", since)}
    for nid, role in roles.items():
        if role in ROUTER_ROLES and heard.get(nid, 0) >= 5 and len(nid) == 9 and not relay_bytes.get(int(nid[-2:], 16)):
            findings.append(_finding("info", "router-silent", f"{role.replace('_', ' ').title()} not seen relaying",
                                     "Configured as infrastructure but no packet reached us via it. It may relay "
                                     "traffic our radio can't hear, so this is weak evidence.", nid,
                                     [{"label": "role", "value": role, "prov": "reported"},
                                      {"label": "packets from it", "value": heard[nid], "prov": "observed"}], describe))

    # 6. degrading direct links (our radio's SNR from 0-hop receptions)
    cut = until - 86400
    for r in q("SELECT DISTINCT from_id FROM rx_hops WHERE hops=0 AND ts>=?", since):
        nid = r[0]
        recent = [x[0] for x in q("SELECT snr FROM rx_hops WHERE from_id=? AND hops=0 AND ts>=? AND snr IS NOT NULL", nid, cut)]
        older = [x[0] for x in q("SELECT snr FROM rx_hops WHERE from_id=? AND hops=0 AND ts>=? AND ts<? AND snr IS NOT NULL",
                                 nid, since, cut)]
        if len(recent) >= 5 and len(older) >= 5:
            drop = statistics.median(older) - statistics.median(recent)
            if drop >= SNR_DROP_DB:
                findings.append(_finding("warn", "link-degrading", "Direct link getting weaker",
                                         f"Median SNR fell {drop:.1f} dB in the last 24 h compared with before.", nid,
                                         [{"label": "SNR before", "value": f"{statistics.median(older):.1f} dB", "prov": "observed"},
                                          {"label": "SNR last 24 h", "value": f"{statistics.median(recent):.1f} dB", "prov": "observed"}],
                                         describe))

    # 7. senders whose firmware doesn't report hop counts
    for r in q("""SELECT from_id, COUNT(*) AS n, SUM(hops IS NULL) AS nohop FROM packets
                  WHERE ts>=? AND from_id != ? GROUP BY from_id HAVING n >= 3 AND nohop = n""", since, local):
        findings.append(_finding("info", "old-firmware", "Doesn't report hop counts",
                                 "None of its packets carry hop_start, which newer firmware always sends. Likely an "
                                 "older firmware version.", r["from_id"],
                                 [{"label": "packets without hop_start", "value": r["n"], "prov": "observed"}], describe))

    findings += _security(q, local, describe)
    order = {"warn": 0, "info": 1}
    findings.sort(key=lambda f: (order.get(f["severity"], 2), f["kind"]))
    return {"range": range_key, "since": since, "until": until, "findings": findings,
            "counts": dict(Counter(f["severity"] for f in findings)),
            "thresholds": {"airtimeWarnPct": AIRTIME_WARN, "channelUtilWarnPct": CHANNEL_UTIL_WARN,
                           "chattyFactor": CHATTY_FACTOR, "snrDropDb": SNR_DROP_DB}}


# ------------------------------------------------------------------ security

def _security(q, local, describe):
    out = []
    latest = {}
    history = defaultdict(list)
    for r in q("SELECT node, public_key, ts FROM node_info ORDER BY ts"):
        if r["public_key"]:
            history[r["node"]].append((r["ts"], r["public_key"]))
            latest[r["node"]] = r["public_key"]
    # old -> new numbers of radios renumbered by firmware 2.8 (analytics views already merge most of them)
    aliases = {r[0]: r[1] for r in q("SELECT old, new FROM _alias")} if q("SELECT 1 FROM sqlite_temp_master WHERE name = '_alias'") else {}

    # weak keys (firmware's own list)
    for nid, key in latest.items():
        if keyflags.is_weak(key):
            out.append(_finding("warn", "weak-key", "Uses a known compromised key",
                                "Its public key is on the Meshtastic firmware's list of low-entropy keys, so its "
                                "direct messages can be decrypted and it can be impersonated. Regenerating the key "
                                "on that device fixes it.", nid,
                                [{"label": "source", "value": weak_keys.SOURCE, "prov": "reported"}], describe))

    # duplicate keys (with the firmware 2.8 renumber exception)
    by_key = defaultdict(list)
    for nid, key in latest.items():
        by_key[key].append(nid)
    for key, nodes in by_key.items():
        if len(nodes) < 2:
            continue
        benign = keyflags.renumbered(nodes, aliases)
        names = ", ".join(describe(n)["name"] for n in nodes)
        out.append(_finding("info" if benign else "warn", "duplicate-key",
                            "Same key, two node numbers (likely a 2.8 firmware upgrade)" if benign
                            else "Several nodes share one public key",
                            ("Firmware 2.8 renumbers a node to crc32(publicKey) on first boot, leaving the old number "
                             "stale. Benign." if benign else
                             "Each node should have a unique key. Sharing one points to a cloned device or copied "
                             "config, and lets one impersonate the other.") + f" Nodes: {names}.",
                            nodes[0], [{"label": "nodes", "value": len(nodes), "prov": "reported"}], describe))

    # key changes
    for nid, hist in history.items():
        keys = []
        for ts, k in hist:
            if not keys or keys[-1][1] != k:
                keys.append((ts, k))
        if len(keys) > 1:
            out.append(_finding("info", "key-changed", "Public key changed",
                                f"Changed {len(keys) - 1} time(s), last {time.strftime('%Y-%m-%d %H:%M', time.localtime(keys[-1][0]))}. "
                                "Usually a factory reset or re-flash; occasionally impersonation.", nid,
                                [{"label": "distinct keys", "value": len(keys), "prov": "reported"}], describe))

    # impersonation of our own radio
    if local:
        tx_ids = {r[0] for r in q("SELECT pkt_id FROM tx_log WHERE from_id = ?", local)}
        tx_start = q("SELECT MIN(ts) FROM tx_log")[0][0]
        direct = q("SELECT COUNT(*) FROM rx_hops WHERE from_id = ? AND hops = 0", local)[0][0]
        if direct:
            out.append(_finding("warn", "impersonation", "Someone transmitted as your radio",
                                f"{direct} packet(s) claiming to come from your radio were heard DIRECTLY (0 hops). "
                                "A radio can't hear its own transmission, and a genuine echo arrives via a relay with "
                                "hops >= 1, so these came from another transmitter.", local,
                                [{"label": "0-hop receptions from our id", "value": direct, "prov": "observed"}], describe))
        if tx_start:
            unk = q("SELECT COUNT(*) FROM rx_hops WHERE from_id = ? AND hops >= 1 AND ts >= ? AND pkt_id NOT IN "
                    "(SELECT pkt_id FROM tx_log WHERE pkt_id IS NOT NULL)", local, tx_start + 60)[0][0]
            if unk:
                out.append(_finding("info", "impersonation-possible", "Unexplained packets with your radio's ID",
                                    f"{unk} relayed packet(s) carry your radio's ID but don't match anything it "
                                    "transmitted. Could be a spoof, or a transmission whose log line was missed "
                                    "during a reconnect.", local,
                                    [{"label": "unmatched receptions", "value": unk, "prov": "observed"},
                                     {"label": "own transmissions logged", "value": len(tx_ids), "prov": "observed"}],
                                    describe))
    return out


# ------------------------------------------------------------------ coverage

def coverage(db_path, range_key, node=None, include_relayed=False, station=None):
    """Position packets our radio heard, located where the sender said it was, with the signal we measured.

    Direct (0-hop) points measure our radio's coverage; relayed points only show the mesh delivered them.
    """
    db = _connect(db_path, station)
    try:
        since = _window(db, range_key)[0]
        sql = """SELECT ts, from_id, hops, snr, rssi,
                        json_extract(raw, '$.decoded.position.latitude') AS lat,
                        json_extract(raw, '$.decoded.position.longitude') AS lon,
                        json_extract(raw, '$.decoded.position.precisionBits') AS bits
                 FROM packets WHERE portnum = 'POSITION_APP' AND ts >= ? AND raw IS NOT NULL"""
        args = [since]
        if not include_relayed:
            sql += " AND hops = 0"
        if node:
            sql += " AND from_id = ?"
            args.append(node)
        rows = [dict(r) for r in db.execute(sql, args).fetchall() if r["lat"] is not None]
        senders = [r[0] for r in db.execute(
            "SELECT from_id FROM packets WHERE portnum='POSITION_APP' AND ts >= ? GROUP BY 1 ORDER BY COUNT(*) DESC, from_id",
            (since,)).fetchall()]
    finally:
        db.close()
    for r in rows:
        r["precisionKm"] = precision_km(r["bits"])
    return {"range": range_key, "points": rows, "senders": senders}


# ------------------------------------------------------------------ traceroute explorer

def traceroutes(db_path, range_key, describe, station=None):
    db = _connect(db_path, station)
    try:
        since = _window(db, range_key)[0]
        ours = [dict(r) for r in db.execute(
            "SELECT * FROM traceroutes WHERE ts >= ? ORDER BY ts DESC", (since,)).fetchall()]
        overheard = [dict(r) for r in db.execute(
            # no recipient = a phone-log import (import_datalog.py): no route to show
            """SELECT ts, from_id, to_id, json_extract(raw, '$.decoded.traceroute') AS tr FROM packets
               WHERE portnum = 'TRACEROUTE_APP' AND ts >= ? AND raw IS NOT NULL AND to_id IS NOT NULL ORDER BY ts DESC""", (since,)).fetchall()]
        cols = [r[1] for r in db.execute("PRAGMA table_info(traceroutes)")]
    finally:
        db.close()
    runs = []
    for t in ours:
        fwd = json.loads(t["forward"]) if t["forward"] else None
        back = json.loads(t["back"]) if t["back"] else None
        name = lambda h: describe(h["id"])["name"] if h.get("id") else "unknown relay"  # noqa: E731
        runs.append({"ts": t["ts"], "source": "ours", "origin": t.get("origin") if "origin" in cols else None,
                     "target": t["target"], "targetName": describe(t["target"])["name"], "status": t["status"],
                     "answered": t["status"] == "ok",
                     "forward": [{**h, "name": name(h)} for h in fwd] if fwd else None,
                     "back": [{**h, "name": name(h)} for h in back] if back else None,
                     "relays": (len(fwd) - 2) if fwd else None})
    for o in overheard:
        try:
            tr = json.loads(o["tr"]) if o["tr"] else {}
        except ValueError:
            tr = {}
        route = [f"!{n:08x}" for n in tr.get("route", [])]
        runs.append({"ts": o["ts"], "source": "overheard", "target": o["to_id"], "targetName": describe(o["to_id"])["name"],
                     "status": "overheard", "answered": None,
                     "forward": [{"id": i, "name": describe(i)["name"], "snr": None} for i in [o["from_id"], *route]],
                     "back": None, "relays": len(route)})
    return {"range": range_key, "runs": runs}
