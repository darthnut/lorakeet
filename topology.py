"""RF topology of the mesh: which radios have been observed hearing each other.

An edge means two radios exchanged packets over the air directly (one heard the other with no relay
in between). It says nothing about who was talking to whom; a relay carries everyone's traffic.

Evidence, strongest first:
  direct       our radio heard the node with 0 hops (we measured the SNR)
  traceroute   consecutive hops in a traced path (each hop measured the SNR)
  neighborinfo the node's own NeighborInfo report of who it hears
  relay        the last hop of a relayed packet must have been heard by us directly; and when a packet
               took exactly one hop, its sender must have been heard by that relayer. Packets name the
               relayer by one byte only, so this is used only when exactly one known node matches.
"""
import json
import sqlite3
from collections import defaultdict

from analytics import (_connect, alias_map, _coverage, _window, is_combined, measured_neighbors, port_group, resolve_relay, scope_local,
                       station_ids, station_location)

MEASURED = ("direct", "traceroute", "neighborinfo")


def relay_resolver(db, local):
    """relay byte -> (node id or None, how), as resolve_relay decides it against every known node."""
    known = {r[0] for r in db.execute("SELECT from_id FROM packets UNION SELECT node FROM node_info "
                                      "UNION SELECT a FROM links UNION SELECT b FROM links") if r[0]}
    aliases = alias_map(db)
    known |= set(aliases)  # pre-2.8 numbers: relay bytes logged before an upgrade end in them
    by_byte = defaultdict(list)
    for nid in known:
        if len(nid) == 9 and nid != local:
            by_byte[int(nid[-2:], 16)].append(nid)
    neighbors = measured_neighbors(db, local)
    return lambda relay: resolve_relay(by_byte.get(relay, []), neighbors, aliases)


def compute(db_path, range_key, local_id, describe):
    db = _connect(db_path, local_id)
    try:
        return _compute(db, range_key, scope_local(local_id) or "", describe)
    finally:
        db.close()


def _compute(db, range_key, local, describe):
    since, until, *_ = _window(db, range_key)
    q = lambda sql, *a: db.execute(sql, a).fetchall()  # noqa: E731

    edges = {}

    def add(a, b, source, snr=None, ts=None):
        if not a or not b or a == b or a in ("^all", "!ffffffff") or b in ("^all", "!ffffffff"):
            return
        key = tuple(sorted((a, b)))
        e = edges.setdefault(key, {"a": key[0], "b": key[1], "sources": defaultdict(int), "snr": [], "last": 0})
        e["sources"][source] += 1
        if snr is not None:
            e["snr"].append(snr)
        e["last"] = max(e["last"], ts or 0)

    # Receptions mined from the firmware debug log (rx_hops) see every copy, duplicates included; the
    # packets table sees the first copy the firmware passed to us. Use both, but skip a packet-based
    # observation when a reception with the same sender, relay byte and hop count was logged within a
    # few seconds: that's the same transmission. (Neither table stores a packet id, so match on these.)
    rx = q("SELECT from_id, relay, hops, snr, ts, pkt_id, COALESCE(station, ?) AS at FROM rx_hops WHERE ts >= ?",
           local, since - 60)
    rx_index = defaultdict(list)
    rx_ids = set()
    for r in rx:
        rx_index[(r["from_id"], r["hops"], r["at"])].append((r["ts"], r["relay"]))
        if r["pkt_id"] is not None:
            rx_ids.add((r["from_id"], r["pkt_id"], r["at"]))

    def mined(frm, hops, ts, relay=None, pkt_id=None, at=None):
        at = at or local
        if pkt_id is not None and (frm, pkt_id, at) in rx_ids:
            return True  # exact: the same packet was mined from that station's reception log
        return any(abs(t - ts) <= 5 and (relay is None or rl == relay) for t, rl in rx_index.get((frm, hops, at), ()))

    # measured links recorded as they happened (direct receptions, traceroute hops, neighbor reports)
    for r in q("SELECT a, b, source, snr, ts FROM links WHERE ts >= ?", since):
        if r["source"] == "direct" and mined(r["a"], 0, r["ts"], at=r["b"]):
            continue  # the same 0-hop reception is counted from rx_hops below
        add(r["a"], r["b"], r["source"], r["snr"], r["ts"])

    # relay inference, only when the 1-byte relay ID maps to exactly one known node. Resolved against the
    # neighbours of the station that heard it (a relay must be in THAT station's range).
    resolvers = {}
    ambiguous = unknown = by_link = 0

    def relay_evidence(frm, relay, hops, snr, ts, at=None):
        nonlocal ambiguous, unknown, by_link
        at = at or local
        if at not in resolvers:
            resolvers[at] = relay_resolver(db, at)
        relayer, how = resolvers[at](relay)
        if relayer is None:
            if how == "unknown":
                unknown += 1
            else:
                ambiguous += 1
            return
        by_link += how == "by-link"
        if at and relayer != at:
            add(relayer, at, "relay", snr, ts)  # this copy reached the station from the relayer; SNR is that hop's
        if hops == 1 and relayer != frm:
            add(frm, relayer, "relay", None, ts)  # one hop: the sender was heard by the relayer

    for r in q("SELECT from_id, relay, hops, snr, ts, pkt_id, COALESCE(station, ?) AS at FROM packets WHERE ts >= ? "
               "AND hops >= 1 AND relay IS NOT NULL AND from_id != COALESCE(station, ?)", local, since, local):
        if not mined(r["from_id"], r["hops"], r["ts"], r["relay"], r["pkt_id"], r["at"]):
            relay_evidence(r["from_id"], r["relay"], r["hops"], r["snr"], r["ts"], r["at"])

    rx_rows = 0
    for r in rx:
        if r["ts"] < since:
            continue
        rx_rows += 1
        if r["hops"] == 0:
            if r["from_id"] != r["at"]:
                add(r["from_id"], r["at"], "direct", r["snr"], r["ts"])  # the station heard the originator itself
        elif r["hops"] is not None and r["relay"] is not None:
            relay_evidence(r["from_id"], r["relay"], r["hops"], r["snr"], r["ts"], r["at"])
    rx_start = min((r["ts"] for r in rx), default=None)

    # nodes: every endpoint, with identity, latest position and activity in range
    ids = {i for e in edges.values() for i in (e["a"], e["b"])}
    pos = {r["node"]: (r["lat"], r["lon"]) for r in q(
        "SELECT node, lat, lon, MAX(ts) FROM positions GROUP BY node")}
    for i in ids:  # a listening station without its own GPS fix: its configured antenna location
        if i not in pos and station_location(i):
            pos[i] = station_location(i)[:2]
    pkts = {r[0]: r[1] for r in q("SELECT from_id, COUNT(*) FROM packets WHERE ts >= ? GROUP BY 1", since)}
    degree = defaultdict(int)
    for e in edges.values():
        degree[e["a"]] += 1
        degree[e["b"]] += 1
    nodes = [{"id": i, **describe(i), "packets": pkts.get(i, 0), "degree": degree[i],
              "lat": pos.get(i, (None, None))[0], "lon": pos.get(i, (None, None))[1]} for i in sorted(ids)]

    out_edges = []
    for e in edges.values():
        src = dict(e["sources"])
        out_edges.append({
            "a": e["a"], "b": e["b"], "sources": src, "count": sum(src.values()),
            "measured": any(s in src for s in MEASURED),
            "snr": sum(e["snr"]) / len(e["snr"]) if e["snr"] else None, "last": e["last"],
        })
    out_edges.sort(key=lambda e: -e["count"])
    return {"range": range_key, "since": since, "until": until, "local": local, "stations": station_ids(db, local),
            "nodes": nodes, "edges": out_edges,
            "stats": {"nodes": len(nodes), "edges": len(out_edges),
                      "measured": sum(e["measured"] for e in out_edges),
                      "inferred": sum(not e["measured"] for e in out_edges),
                      "ambiguousRelayPackets": ambiguous, "unknownRelayPackets": unknown,
                      "relayResolvedByLink": by_link, "receptionsMined": rx_rows,
                      "miningSince": rx_start}}


# ---------------------------------------------------------------- traffic replay

REPLAY_CAP = 50000  # events per response; the newest are kept when a range holds more


def replay(db_path, range_key, local_id, describe):
    db = _connect(db_path, local_id)
    try:
        out = _replay(db, range_key, scope_local(local_id) or "")
        ids = ({e["from"] for e in out["events"]} | {e["relay"] for e in out["events"] if e.get("relay")}
               | {i for e in out["events"] for i in e.get("path", []) + e.get("back", []) if i})
        pos = {r[0]: (r[1], r[2]) for r in db.execute("SELECT node, lat, lon, MAX(ts) FROM positions GROUP BY node")}
        for i in ids:  # stations without a GPS fix: their configured antenna location
            if i not in pos and station_location(i):
                pos[i] = station_location(i)[:2]
        out["nodes"] = {i: {**describe(i), "lat": pos.get(i, (None, None))[0], "lon": pos.get(i, (None, None))[1]}
                        for i in sorted(i for i in ids if i)}
        return out
    finally:
        db.close()


def _replay(db, range_key, local):
    """Every over-the-air event in the range, in time order, for animating on the topology graph.

    One event per reception our radio logged, duplicates included (rx_hops), plus first copies from
    `packets` that the debug-log miner didn't see, plus our own transmissions (tx_log). Each names only
    what the radio actually knew: the originator, the hop count and the 1-byte relay ID (resolved to a node
    only when unambiguous). The path between originator and relayer is never invented: when hops > 1 the
    intermediate radios are unknown, and the event says so.
    """
    since, until, *_ = _window(db, range_key)
    q = lambda sql, *a: db.execute(sql, a).fetchall()  # noqa: E731
    resolvers = {}

    def resolve_at(at):
        if at not in resolvers:
            resolvers[at] = relay_resolver(db, at)
        return resolvers[at]
    port, rowid = {}, {}  # (sender, packet id) -> port number / packets rowid (for the packet anatomy link)
    for r in q("SELECT from_id, pkt_id, portnum, rowid FROM packets WHERE ts >= ? AND pkt_id IS NOT NULL", since - 600):
        port[(r[0], r[1])], rowid[(r[0], r[1])] = r[2], r[3]

    events = []
    seen = set()  # (from, pkt_id, hops, relay) of mined receptions, to skip the same copy from `packets`
    for r in q("SELECT ts, from_id, relay, hops, snr, pkt_id, to_id, COALESCE(station, ?) AS at FROM rx_hops "
               "WHERE ts >= ? ORDER BY ts", local, since):
        seen.add((r["from_id"], r["pkt_id"], r["hops"], r["relay"], r["at"]))
        p = port.get((r["from_id"], r["pkt_id"]))
        events.append(_event(r, p, resolve_at(r["at"]), local, "rx", rowid.get((r["from_id"], r["pkt_id"]))))
    for r in q("SELECT ts, from_id, relay, hops, snr, pkt_id, to_id, portnum, rowid, COALESCE(station, ?) AS at "
               "FROM packets WHERE ts >= ? AND from_id != COALESCE(station, ?) ORDER BY ts", local, since, local):
        if (r["from_id"], r["pkt_id"], r["hops"], r["relay"], r["at"]) in seen:
            continue
        events.append(_event(r, r["portnum"], resolve_at(r["at"]), local, "rx", r["rowid"]))
    # tx_log has no port number; our own sent texts are known from `messages`, the rest stay unknown
    texts = {r[0] for r in q("SELECT pkt_id FROM messages WHERE outgoing = 1 AND pkt_id IS NOT NULL")}
    for r in q("SELECT ts, from_id, to_id, pkt_id, COALESCE(station, ?) AS at FROM tx_log WHERE ts >= ? ORDER BY ts",
               local, since):
        events.append({"ts": r["ts"], "from": r["at"] or r["from_id"], "kind": "tx", "to": r["to_id"],
                       "group": "Text" if r["pkt_id"] in texts else "Unknown"})
    # Our successful traceroutes: the only multi-hop paths known hop by hop (every hop measured its SNR),
    # including links between radios we never hear. Animated out along the route and back.
    for r in q("SELECT ts, target, forward, back, origin, done_ts, COALESCE(station, ?) AS at FROM traceroutes "
               "WHERE ts >= ? AND status = 'ok' AND forward IS NOT NULL", local, since):
        fwd, back = json.loads(r["forward"]), json.loads(r["back"] or "[]")
        events.append({"ts": r["ts"], "kind": "trace", "group": "Routing", "from": r["at"], "to": r["target"],
                       "path": [h.get("id") for h in fwd], "snr": [h.get("snr") for h in fwd],
                       "back": [h.get("id") for h in back], "snrBack": [h.get("snr") for h in back],
                       "origin": r["origin"], "doneTs": r["done_ts"]})
    events.sort(key=lambda e: e["ts"])
    # Text messages we could read, once each (several stations, or our own copy and a relayed one, can log the
    # same packet): the replay highlights them as they play. A channel number means different things on
    # different radios, so it's named from the logging station's own channel list (its latest connect snapshot).
    names = _channel_names(db)
    messages, seen_msg = [], set()
    for r in q("SELECT ts, from_id, to_id, channel, text, pkt_id, COALESCE(station, ?) AS at FROM messages WHERE ts >= ? "
               "AND COALESCE(encrypted, 0) = 0 AND text IS NOT NULL AND text != '' ORDER BY ts", local, since):
        k = (r["from_id"], r["pkt_id"]) if r["pkt_id"] is not None else ("row", r["ts"], r["text"])
        if k in seen_msg:
            continue
        seen_msg.add(k)
        direct = bool(r["to_id"]) and r["to_id"] not in ("^all", "!ffffffff")
        messages.append({"ts": r["ts"], "from": r["from_id"], "to": r["to_id"], "text": r["text"][:240],
                         "channel": r["channel"], "direct": direct,
                         "channelName": None if direct else names.get(r["at"], {}).get(r["channel"] or 0)})
    truncated = len(events) > REPLAY_CAP
    if truncated:
        events = events[-REPLAY_CAP:]
    return {"range": range_key, "since": since, "until": until, "local": local, "stations": station_ids(db, local),
            "events": events, "messages": messages,
            "coveredHours": sorted(_coverage(db, since, local)), "truncated": truncated,
            "miningSince": (q("SELECT MIN(ts) FROM rx_hops")[0][0])}


def _channel_names(db):
    """{station: {channel index: name}} from each station's latest connect snapshot (keys already removed)."""
    out = {}
    for st, detail in db.execute("SELECT station, detail FROM events WHERE kind = 'connected' AND station IS NOT NULL "
                                 "ORDER BY ts"):
        try:
            chans = json.loads(detail).get("channels") or []
        except (TypeError, ValueError):
            continue
        out[st] = {c.get("index", 0): ((c.get("settings") or {}).get("name") or "LongFast") for c in chans
                   if c.get("role") in ("PRIMARY", "SECONDARY")}
    return out


def _event(r, portnum, resolve, local, kind, row=None):
    relayer, how = (None, None)
    if r["hops"] and r["relay"] is not None:
        relayer, how = resolve(r["relay"])
    e = {"ts": r["ts"], "from": r["from_id"], "hops": r["hops"], "kind": kind,
         "group": port_group(portnum) if portnum else "Unknown", "to": r["to_id"]}
    if r["at"] and r["at"] != local:
        e["at"] = r["at"]  # heard by another station: the animation ends there, not at our radio
    if r["hops"]:
        e["relay"] = relayer
        e["relayByte"] = r["relay"]
        e["relayHow"] = how
    if r["snr"] is not None:
        e["snr"] = r["snr"]
    if row is not None:
        e["row"] = row
    return e
