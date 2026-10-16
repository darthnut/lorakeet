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
