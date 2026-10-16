"""Airtime on a MeshCore station (airtime._compute_meshcore): calculated from each reception's length at the radio's
own settings, set against the radio's measured receive airtime; MeshCore packets in the same type groups."""
import airtime
import analytics
import demo


def test_a_meshcore_stations_airtime(tmp_path):
    path = tmp_path / "demo.db"
    demo.build(path)
    mc = next(s["id"] for s in analytics.stations(path) if s["id"].startswith("mc:"))
    r = airtime.compute(path, "24h", mc, lambda i: {"name": i})
    t = r["totals"]
    assert r["protocol"] == "meshcore" and not r["radioAssumed"] and r["radio"]["sf"] == 7
    assert t["heardS"] > 0 and t["transmissions"] > 0
    assert {g["group"] for g in r["byGroup"]} >= {"Text", "Node info", "Routing"}   # not all "Other"
    # the demo radio's counter adds up what it heard (+8 %, whole seconds): the measurement lands near the maths
    assert t["chUtilAvg"] is not None and 0.5 < t["chUtilAvg"] / t["heardPct"] < 2
    assert any(x["name"] == "sender not named" for x in r["originators"])           # most MeshCore packets
    assert all(not x["id"] or x["id"].startswith("mc:") for x in r["transmitters"])
    # a Meshtastic station's airtime is as before
    assert airtime.compute(path, "24h", demo.home_of(path), lambda i: {"name": i}).get("protocol") is None
