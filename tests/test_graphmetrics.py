"""Critical relays (graphmetrics.py) on a graph with known answers, and on the demo mesh."""
import demo
import graphmetrics

S = "!a0000001"                    # the listening station
R1, R2 = "!a0000011", "!a0000012"  # two routers, linked to each other
A, B, C, D = "!a0000021", "!a0000022", "!a0000023", "!a0000024"


def _e(a, b, measured=True):
    return {"a": a, "b": b, "measured": measured}


def test_known_graph():
    # S - R1 - {A, B};  S - R2 - C;  R1 - R2;  S - D
    edges = [_e(S, R1), _e(R1, A), _e(R1, B, measured=False), _e(S, R2), _e(R2, C), _e(R1, R2), _e(S, D)]
    r = graphmetrics.analyse([], edges, {S})
    by = {x["id"]: x for x in r["radios"]}
    assert S not in by                                    # the observer isn't ranked
    assert by[R1]["cutsOff"] == 2 and sorted(by[R1]["cutList"]) == [A, B]
    assert by[R2]["cutsOff"] == 1 and by[R2]["cutList"] == [C]
    assert by[D]["cutsOff"] == 0 and by[A]["cutsOff"] == 0  # leaves cut off nothing but themselves
    assert by[R1]["betweenness"] > by[R2]["betweenness"] > by[A]["betweenness"] == 0
    assert r["radios"][0]["id"] == R1                     # ranked by what it would cut off
    links = {(x["a"], x["b"]): x["cutsOff"] for x in r["links"]}
    assert not links      # every cut is a leaf's only link (already in the radio list); R1 and R2 have other ways in
    # a link that joins two parts: S - R1 - R2 - {A, B}; losing R1-R2 strands R2, A and B
    chain = graphmetrics.analyse([], [_e(S, R1), _e(R1, R2), _e(R2, A), _e(R2, B)], {S})
    assert {(x["a"], x["b"]): x["cutsOff"] for x in chain["links"]} == {(S, R1): 4, (R1, R2): 3}
    st = r["stats"]
    assert st["singlePoints"] == 2 and st["dependOnOne"] == 3 and st["reachable"] == 7
    assert abs(st["inferredShare"] - 1 / 7) < 1e-9


def test_a_ring_has_no_single_point():
    ring = [_e(S, R1), _e(R1, R2), _e(R2, A), _e(A, S)]
    r = graphmetrics.analyse([], ring, {S})
    assert r["stats"]["singlePoints"] == 0 and not r["links"]


def test_on_the_demo_mesh(tmp_path):
    path = tmp_path / "demo.db"
    home = demo.build(path)
    r = graphmetrics.compute(path, "7d", home, lambda i: {"name": i})
    assert r["stats"]["radios"] > 10 and r["radios"] and all(0 <= x["betweenness"] <= 1 for x in r["radios"])
    assert home not in {x["id"] for x in r["radios"]}


def test_history_finds_the_day_a_radio_became_a_single_point_of_failure(tmp_path):
    """Critical relays day by day: RELAY_R links the station all along; FAR_R shows up on day 10, reachable only
    through it, so from day 10 RELAY_R would cut FAR_R off. The station's Wi-Fi changed that day too."""
    import json
    import time as _t
    from datetime import datetime, timedelta

    import store as store_mod
    from synth import describe as _describe
    STATION, RELAY_R, FAR_R = "!a0000001", "!c0000011", "!c0000012"
    path = tmp_path / "mesh.db"
    st = store_mod.Store(path)
    st.claim_station(STATION)
    start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=19)
    for d in range(20):
        for h in (2, 8, 14, 20):
            t = (start + timedelta(days=d, hours=h)).timestamp()
            if t > _t.time():
                break
            st.insert("links", ts=t, a=RELAY_R, b=STATION, snr=-8, source="direct", station=STATION)
            st.insert("packets", ts=t, from_id=RELAY_R, pkt_id=int(t), portnum="TELEMETRY_APP", hops=0, station=STATION, raw="{}")
            if d >= 10:
                st.insert("links", ts=t + 5, a=FAR_R, b=RELAY_R, snr=-12, source="traceroute", station=STATION)
    moved = (start + timedelta(days=10, hours=1)).timestamp()
    for net, t in (("HomeWifi", start.timestamp() + 60), ("OtherWifi", moved)):
        st.insert("events", ts=t, kind="network", detail=json.dumps({"network": net}), station=STATION)
    st.close()
    h = graphmetrics.history(path, "30d", STATION, _describe)
    spof = [c for c in h["changes"] if c["kind"] == "spof"]
    assert len(spof) == 1 and spof[0]["id"] == RELAY_R
    assert datetime.fromtimestamp(spof[0]["t"]).date() == (start + timedelta(days=10)).date()
    assert [x["id"] for x in spof[0]["cutList"]] == [FAR_R]
    assert any("HomeWifi to OtherWifi" in e["text"] for e in spof[0]["events"])
    by_day = {datetime.fromtimestamp(x["t"]).date(): x for x in h["days"]}
    assert by_day[(start + timedelta(days=5)).date()]["singlePoints"] == 0
    assert by_day[(start + timedelta(days=12)).date()]["singlePoints"] == 1
    r = next(r for r in h["radios"] if r["id"] == RELAY_R)
    assert r["daysCritical"] >= 9 and r["now"]["c"] == 1
