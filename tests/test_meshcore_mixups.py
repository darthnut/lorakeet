"""A Lorakeet (or hub) with Meshtastic and MeshCore stations side by side: each kind of radio counted, named and
threaded as its own, never one mistaken for the other (review before 0.5.0)."""
import json
import time

import analytics
import compare
import mesh as mesh_mod
import node_analytics
import store as store_mod

HOME, MC = "!a0000001", "mc:c0000099d0d0"
RELAY = "!a00000be"          # a Meshtastic relay whose byte is 0xbe
MC_RADIO = "mc:a0000017c2be"  # a MeshCore radio whose id also ends in "be"


def describe(nid):
    return {"name": nid or "(sender not named)"}


def _build(path):
    st = store_mod.Store(path)
    st.claim_station(HOME)
    now = time.time()
    st.insert("packets", ts=now - 600, from_id="!a0000005", to_id="^all", portnum="TEXT_MESSAGE_APP", hops=1, relay=0xbe,
              pkt_id=7)
    st.insert("node_info", ts=now - 700, node=RELAY, long_name="Relay")
    st.insert("telemetry_full", ts=now - 300, node=HOME, kind="localStats", data="{}")
    for i, (frm, port) in enumerate([(None, "MC_GRP_TXT"), (None, "MC_ACK"), (MC_RADIO, "MC_ADVERT")]):
        st.insert("packets", ts=now - 500 + i, from_id=frm, to_id=None, portnum=port, hops=2, pkt_id=100 + i,
                  station=MC, raw=json.dumps({"meshcore": {}}))
    st.insert("node_info", ts=now - 500, node=MC_RADIO, long_name="Hilltop", station=MC)
    st.insert("telemetry_full", ts=now - 400, node=MC, kind="meshcore_stats_core", data="{}", station=MC)
    st.insert("messages", ts=now - 450, from_id=None, to_id="^all", channel=0, text="Hilltop: hello", station=MC)
    st.insert("messages", ts=now - 440, from_id="!a0000005", to_id="^all", channel=0, text="hi", pkt_id=7)
    st.flush()
    return st


def test_both_kinds_of_station_side_by_side(tmp_path):
    path = tmp_path / "mesh.db"
    st = _build(path)
    both = analytics.compute(path, "24h", analytics.ALL_STATIONS, describe)   # once crashed on the senderless rows
    assert both["kpis"]["packets"] == 4                                      # senderless MeshCore packets count
    assert all(n["id"] for n in both["nodes"])                               # ...but aren't listed as a radio
    relay = next(r for r in both["relays"] if r["byte"] == "be")
    assert [c["id"] for c in relay["candidates"]] == [RELAY]                 # never the MeshCore radio ending in be
    assert node_analytics._relay_candidates(st.db, 0xbe, HOME) == [RELAY]
    threads = {c["protocol"]: c for c in both["conversations"]}
    assert set(threads) == {"meshtastic", "meshcore"}                         # two meshes, two threads
    mc = analytics.compute(path, "24h", MC, describe)
    assert mc["kpis"]["packets"] == 3 and mc["kpis"]["coveredHours"] >= 1     # counters count as logging
    st.close()


def test_the_messages_tab_shows_a_meshcore_stations_messages_under_their_own_channel(tmp_path):
    st = _build(tmp_path / "mesh.db")
    m = next(r for r in mesh_mod.messages_for_this_radio(st, HOME) if r["station"] == MC)
    assert m["text"] == "Hilltop: hello" and m["channel"] is None and m["channelName"] == "MeshCore channel 0"
    st.close()


def test_station_comparison_is_for_meshtastic_stations(tmp_path):
    path = tmp_path / "mesh.db"
    _build(path).close()
    try:
        compare.compute(path, "24h", HOME, MC, describe)
        raise AssertionError("compared a MeshCore station")
    except ValueError as e:
        assert "Meshtastic" in str(e)
    assert MC not in [s["id"] for s in compare.matrix(path, "24h", describe).get("stations", [])]


def test_the_packet_browser_filters_by_network(tmp_path):
    import packetsearch
    path = tmp_path / "mesh.db"
    _build(path).close()
    ports = lambda net: sorted(r["portnum"] for r in packetsearch.search(path, "packets", {"network": net},  # noqa: E731
                                                                       station=analytics.ALL_STATIONS)["rows"])
    assert ports("meshcore") == ["MC_ACK", "MC_ADVERT", "MC_GRP_TXT"]
    assert ports("meshtastic") == ["TEXT_MESSAGE_APP"]
    assert len(ports("")) == 4
