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
