"""Critical relays: which radios (and links) the mesh, as our stations see it, depends on.

Built on the link graph topology.compute draws (measured links: direct receptions, traceroutes, neighbor reports;
inferred: relay bytes and MeshCore paths resolved to one radio). Two questions, answered on that graph:

- **Centrality** (betweenness, Brandes' algorithm, unweighted): the share of shortest paths between other radios that
  pass through a radio. A high value: much of what's heard travels through it.
- **What it would cut off**: take a radio (or a link) away, and count the radios that could then reach none of our
  listening stations. A radio that cuts off any is a single point of failure, as far as our stations can tell. A
  link is listed only when it joins parts of the mesh (a radio's one and only link would just repeat the radio list).

Caveat, kept on the page: the graph is only what our stations observed, partly inferred, over the chosen range. A radio
that looks critical may have links we never heard; one that looks redundant may not be. It's a view of the mesh from
here, not a survey of it. Stations themselves are left out of the rankings: removing the observer says nothing.
"""
from collections import defaultdict, deque

import topology

TOP_N = 25


def compute(db_path, range_key, local_id, describe, everyone=False):
    t = topology.compute(db_path, range_key, local_id, describe)
    stations = set(t["stations"] if isinstance(t["stations"], list) else t["stations"] or [])
    stations |= {t["local"]} if t["local"] else set()
    return analyse(t["nodes"], t["edges"], stations, describe, everyone) | {"range": range_key, "since": t["since"],
                                                                  "until": t["until"], "local": t["local"]}


def analyse(nodes, edges, stations, describe=lambda i: {"name": i}, everyone=False):
    """everyone: every radio, not just the top TOP_N (the Visualizations page colours the whole graph by these)."""
    adj = defaultdict(set)
    for e in edges:
        adj[e["a"]].add(e["b"])
        adj[e["b"]].add(e["a"])
    ids = sorted(adj)
    stations = {s for s in stations if s in adj}
    btw = _betweenness(ids, adj)
    reach = _reachable(adj, stations)               # radios linked to a station at all, with nothing removed

    def cut_off(without=None, edge=None):
        """Radios that reach a station now but wouldn't without `without` (a radio) or `edge` (a link)."""
        r = _reachable(adj, stations - {without}, skip=without, skip_edge=edge)
        return sorted(reach - r - {without})

    radios = []
    for i in ids:
        if i in stations:
            continue
        lost = cut_off(without=i) if len(adj[i]) > 1 else []   # a leaf cuts off nothing but itself
        radios.append({"id": i, **describe(i), "betweenness": btw[i], "degree": len(adj[i]),
                       "cutsOff": len(lost), "cutList": lost[:40]})
    radios.sort(key=lambda r: (-r["cutsOff"], -r["betweenness"]))
    links = []
    for e in edges:
        if e["a"] in stations and e["b"] in stations:
            continue
        lost = cut_off(edge=(e["a"], e["b"]))
        # a radio with this one link loses only itself: that's the radio list's "cuts off" already, not a link worth
        # listing. The links shown are the ones joining parts of the mesh.
        if lost and not (len(lost) == 1 and lost[0] in (e["a"], e["b"]) and len(adj[lost[0]]) == 1):
            links.append({"a": e["a"], "b": e["b"], "aName": describe(e["a"])["name"], "bName": describe(e["b"])["name"],
                          "measured": e.get("measured", False), "cutsOff": len(lost), "cutList": lost[:40]})
    links.sort(key=lambda x: -x["cutsOff"])
    spof = [r for r in radios if r["cutsOff"]]
    return {
        "radios": radios if everyone else radios[:TOP_N], "links": links[:TOP_N],
        "stats": {"radios": len(ids), "links": len(edges), "stations": sorted(stations), "reachable": len(reach),
                  "singlePoints": len(spof), "singleLinks": len(links),
                  "dependOnOne": len({x for r in spof for x in r["cutList"]}),
                  "inferredShare": (sum(1 for e in edges if not e.get("measured")) / len(edges)) if edges else None},
    }


def _reachable(adj, sources, skip=None, skip_edge=None):
    """Every radio connected to any of `sources` (breadth-first), not passing through `skip` or along `skip_edge`."""
    blocked = {tuple(sorted(skip_edge))} if skip_edge else set()
    seen = set(s for s in sources if s != skip)
    q = deque(seen)
    while q:
        u = q.popleft()
        for v in adj[u]:
            if v == skip or v in seen or tuple(sorted((u, v))) in blocked:
                continue
            seen.add(v)
            q.append(v)
    return seen


def _betweenness(ids, adj):
    """Brandes (2001), unweighted, undirected; normalised to 0..1 by the number of other pairs."""
    cb = dict.fromkeys(ids, 0.0)
    for s in ids:
        stack, preds = [], defaultdict(list)
        sigma = dict.fromkeys(ids, 0)
        dist = dict.fromkeys(ids, -1)
        sigma[s], dist[s] = 1, 0
        q = deque([s])
        while q:
            v = q.popleft()
            stack.append(v)
            for w in adj[v]:
                if dist[w] < 0:
                    dist[w] = dist[v] + 1
                    q.append(w)
                if dist[w] == dist[v] + 1:
                    sigma[w] += sigma[v]
                    preds[w].append(v)
        delta = dict.fromkeys(ids, 0.0)
        while stack:
            w = stack.pop()
            for v in preds[w]:
                delta[v] += sigma[v] / sigma[w] * (1 + delta[w])
            if w != s:
                cb[w] += delta[w]
    n = len(ids)
    scale = 1 / ((n - 1) * (n - 2)) if n > 2 else 0   # undirected: each pair counted twice above
    return {i: cb[i] * scale for i in ids}


# ------------------------------------------------------------------ over time

WINDOW_DAYS = 7      # each day's graph: the links seen in the 7 days up to the end of that day
CENTRAL_JUMP = 0.05  # a centrality change counts when it moves this much (5 points of shortest routes) and
CENTRAL_RATIO = 2.0  # doubles or halves, or moves CENTRAL_BIG whatever the ratio ...
CENTRAL_BIG = 0.10
HOLD_DAYS = 2        # ... and holds for this many days


def history(db_path, range_key, station, describe, window_days=WINDOW_DAYS):
    """Critical relays day by day: each day's graph is the links (topology's, measured and inferred) seen in the
    window_days up to the end of that day. Per radio: centrality, what it would cut off and its links each day;
    the days it became (or stopped being) a single point of failure, and big centrality changes, with station
    events logged near them (linkhistory.station_events)."""
    from datetime import datetime, timedelta

    import corruption
    import linkhistory
    from analytics import RANGES, _connect, _window, scope_local, station_ids

    if range_key not in RANGES:
        raise ValueError(f"range must be one of {', '.join(RANGES)}")
    window = max(1, min(int(window_days), 30)) * 86400
    db = _connect(db_path, station)
    try:
        local = scope_local(station) or ""
        since, until, *_ = _window(db, range_key)
        obs = []
        topology._compute(db, range_key, local, describe, since=since - window, collect=obs)
        first = linkhistory.first_heard(db)
        # a relay byte resolved against today's radios can name one not yet heard then (linkhistory.first_heard)
        obs = [o for o in obs if o[2] != "relay" or (first.get(o[0], 0) <= o[3] + 60 and first.get(o[1], 0) <= o[3] + 60)]
        stations = set(station_ids(db, local))
        events = linkhistory.station_events(db, since - window, until)
    finally:
        db.close()
    ph = {i for o in obs for i in o[:2] if corruption.phantom((describe(i) or {}).get("corrupt"))}
    obs = sorted(o for o in obs if o[0] not in ph and o[1] not in ph)

    day = datetime.fromtimestamp(since).replace(hour=0, minute=0, second=0, microsecond=0)
    ends, label = [], {}   # each day's graph ends at the end of that day (or now); it's labelled by the day itself
    while day.timestamp() < until:
        e = min(until, (day + timedelta(days=1)).timestamp())
        ends.append(e)
        label[e] = day.timestamp()
        day += timedelta(days=1)
    filled = min((o[3] for o in obs), default=until) + window   # before this, a day's graph is still filling in: no "changes"
    days, per = [], defaultdict(dict)
    name = lambda i: {"name": i}  # noqa: E731  (names once, at the end)
    for end in ends:
        lo = end - window
        keys = {(a, b) for a, b, _s, ts in obs if lo < ts <= end}
        edges = [{"a": a, "b": b} for a, b in keys]
        if not edges:
            days.append({"t": label[end], "radios": 0, "links": 0, "singlePoints": 0, "dependOnOne": 0, "reachable": 0})
            continue
        r = analyse([], edges, stations, name, everyone=True)
        st = r["stats"]
        days.append({"t": label[end], "radios": st["radios"], "links": st["links"], "singlePoints": st["singlePoints"],
                     "dependOnOne": st["dependOnOne"], "reachable": st["reachable"]})
        for x in r["radios"]:
            per[x["id"]][end] = {"b": round(x["betweenness"], 4), "c": x["cutsOff"], "d": x["degree"], "l": x["cutList"]}

    def changes(series):
        out, vals = [], [series.get(e) for e in ends]
        for i in range(1, len(ends) - HOLD_DAYS + 1):
            prev, now = vals[i - 1], vals[i:i + HOLD_DAYS]
            if prev is None or any(v is None for v in now) or ends[i - 1] < filled:
                continue
            if prev["c"] == 0 and all(v["c"] > 0 for v in now):
                out.append({"t": label[ends[i]], "kind": "spof", "cutList": now[0]["l"],
                            "text": f"Became a single point of failure: without it, {now[0]['c']} radio{'s' if now[0]['c'] != 1 else ''} "
                                    "couldn't reach your stations"})
            elif prev["c"] > 0 and all(v["c"] == 0 for v in now):
                out.append({"t": label[ends[i]], "kind": "spof-end", "text": "No longer a single point of failure"})
            b0, b1 = prev["b"], min(v["b"] for v in now) if now[0]["b"] > prev["b"] else max(v["b"] for v in now)
            if abs(b1 - b0) >= CENTRAL_BIG or (abs(b1 - b0) >= CENTRAL_JUMP and (b1 >= b0 * CENTRAL_RATIO or b1 <= b0 / CENTRAL_RATIO)):
                out.append({"t": label[ends[i]], "kind": "central", "text": f"Centrality {'rose' if b1 > b0 else 'fell'} from {b0 * 100:.0f} % to {b1 * 100:.0f} %"})
        for c in out:   # what was logged that day or the day before (a station moved, radio settings changed)
            c["events"] = [e for e in events if c["t"] - 86400 <= e["ts"] < c["t"] + 86400]
        return out

    for series in per.values():   # radio names for the cut lists, once
        for v in series.values():
            v["l"] = [{"id": i, "name": describe(i)["name"]} for i in v["l"]]
    radios = []
    for nid, series in per.items():
        if nid in stations:
            continue
        vals = list(series.values())
        peak_b, peak_c = max(v["b"] for v in vals), max(v["c"] for v in vals)
        ch = changes(series)
        if peak_b < 0.01 and not peak_c and not ch:
            continue
        last = series.get(ends[-1])
        radios.append({"id": nid, **describe(nid), "series": [{"t": label[e], **series[e]} if e in series else {"t": label[e]} for e in ends],
                       "now": last, "peakCentral": peak_b, "peakCutsOff": peak_c, "daysCritical": sum(1 for v in vals if v["c"]),
                       "changes": ch})
    radios.sort(key=lambda r: (-bool(r["changes"]), -r["peakCutsOff"], -r["peakCentral"]))
    flat = sorted(({**c, "id": r["id"], "name": r["name"]} for r in radios for c in r["changes"]), key=lambda c: -c["t"])
    return {"range": range_key, "since": since, "until": until, "windowDays": window // 86400, "days": days,
            "radios": radios[:40], "changes": flat[:50], "stations": sorted(stations)}
