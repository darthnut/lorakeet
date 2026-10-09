"""Radios that reach us with no hops to spare: most of their packets arrive on their very last hop."""
import time

import insights
import server
from synth import describe

LOCAL, EDGE, FINE = "!a0000001", "!c0000001", "!c0000002"


def test_a_radio_whose_packets_arrive_on_their_last_hop_is_flagged(tmp_path):
    path = tmp_path / "mesh.db"
    store = server.Store(path)
    store.station = LOCAL
    t = time.time() - 3600
    store.insert("packets", ts=t, from_id=EDGE, pkt_id=0)
    for k in range(8):
        # EDGE: hop limit 3, and 6 of 8 packets used all 3 (one of those also arrived again with a hop left: the
        # best copy counts, so that packet isn't "at the edge")
        left = 0 if k < 6 else 1
        store.insert("rx_hops", ts=t + k, from_id=EDGE, pkt_id=k, hop_start=3, hop_limit=left, hops=3 - left)
        if k == 5:
            store.insert("rx_hops", ts=t + k + 1, from_id=EDGE, pkt_id=k, hop_start=3, hop_limit=1, hops=2)
        store.insert("rx_hops", ts=t + k, from_id=FINE, pkt_id=100 + k, hop_start=3, hop_limit=2, hops=1)
    store.db.close()
    found = {f["node"]["id"]: f for f in insights.health(path, "24h", LOCAL, describe)["findings"] if f["kind"] == "hops-edge"}
    assert set(found) == {EDGE}
    assert "5 of its 8 packets" in found[EDGE]["detail"] and "from 3 to 4" in found[EDGE]["detail"]
