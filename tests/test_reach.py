"""Station reach and gaps (reach.py): each fixed station's direct neighbours, and the radios no station hears
directly, grouped into squares. Moving stations and stations with no location are listed, not drawn."""
import time

import analytics
import reach
import store as store_mod

S1, S2, CAR, LOST = "!a0000001", "!a0000002", "!a0000003", "!a0000004"
NEAR, BOTH, FAR, NOPOS = "!c0000001", "!c0000002", "!c0000003", "!c0000004"


def pk(st, ts, frm, station, hops, snr=None):
    st.insert("packets", ts=ts, from_id=frm, to_id="^all", portnum="POSITION_APP", station=station, hops=hops, snr=snr)


def test_reach_and_gaps(tmp_path, monkeypatch):
    monkeypatch.setattr(analytics, "STATION_LOCATIONS", {S1: (45.70, -121.50, None), S2: (45.70, -121.40, None)})
    monkeypatch.setattr(reach, "STATION_LOCATIONS", analytics.STATION_LOCATIONS)
    st = store_mod.Store(tmp_path / "mesh.db")
    now = time.time()
    for s in (S1, S2, CAR, LOST):
        pk(st, now - 7200, "!c00000ff", s, 3)
    for i in range(5):                                            # the car drives 10 km
        st.insert("positions", ts=now - 3600 + i * 60, node=CAR, lat=45.6 + i * 0.02, lon=-121.6, station=CAR, source="own")
    st.insert("positions", ts=now - 600, node=NEAR, lat=45.71, lon=-121.49, precision_bits=32)   # 1 km from S1
    st.insert("positions", ts=now - 600, node=BOTH, lat=45.70, lon=-121.45, precision_bits=32)
    st.insert("positions", ts=now - 900, node=FAR, lat=45.90, lon=-121.50, precision_bits=32)    # a rounded one later:
    st.insert("positions", ts=now - 300, node=FAR, lat=45.91, lon=-121.51, precision_bits=13)    # the precise one wins
    pk(st, now - 500, NEAR, S1, 0, -3.0)
    pk(st, now - 400, NEAR, S1, 0, 2.0)
    pk(st, now - 300, BOTH, S1, 0, -8.0)
    pk(st, now - 300, BOTH, S2, 0, -1.0)
    pk(st, now - 200, FAR, S1, 2)
    pk(st, now - 200, FAR, S2, 3)
    pk(st, now - 100, NOPOS, S2, 1)
    pk(st, now - 100, S2, S1, 0)                                   # one of our stations: not a neighbour
    st.flush()
    r = reach.compute(tmp_path / "mesh.db", "24h", "*", lambda i: {"name": i})
    st.close()

    assert [s["id"] for s in r["stations"]] == [S1, S2]
    assert {s["id"]: s["why"] for s in r["skipped"]} == {CAR: "moving", LOST: "no location"}
    s1 = r["stations"][0]
    assert s1["direct"] == 2 and s1["onlyHere"] == 1 and 0.5 < s1["farthestKm"] < 4.5
    near = next(lk for lk in r["links"] if lk["radio"] == NEAR)
    assert near["receptions"] == 2 and near["bestSnr"] == 2.0 and 1.0 < near["km"] < 1.6
    radios = {x["id"]: x for x in r["radios"]}
    assert radios[BOTH]["direct"] == [S1, S2] and radios[FAR]["direct"] == [] and radios[FAR]["minHops"] == 2
    assert radios[FAR]["lat"] == 45.90 and radios[FAR]["precisionKm"] == 0.0            # the most precise position
    assert radios[NOPOS]["lat"] is None and "!c00000ff" in radios
    assert r["stats"]["relayedOnly"] == 3 and r["stats"]["gapsPlaced"] == 1
    (gap,) = [c for c in r["cells"] if c["gap"]]
    assert gap["relayed"] == [FAR] and gap["bounds"][0][0] <= 45.90 <= gap["bounds"][1][0]


def test_one_station(tmp_path, monkeypatch):
    monkeypatch.setattr(reach, "STATION_LOCATIONS", {S1: (45.70, -121.50, None), S2: (45.70, -121.40, None)})
    st = store_mod.Store(tmp_path / "mesh.db")
    now = time.time()
    st.insert("positions", ts=now - 600, node=BOTH, lat=45.70, lon=-121.45, precision_bits=32)
    pk(st, now - 300, BOTH, S1, 2)
    pk(st, now - 300, BOTH, S2, 0)
    st.flush()
    r = reach.compute(tmp_path / "mesh.db", "24h", S1, lambda i: {"name": i})
    st.close()
    # judged by S1 alone: S2 hearing it directly doesn't count
    assert [s["id"] for s in r["stations"]] == [S1] and r["radios"][0]["direct"] == [] and r["cells"][0]["gap"]
