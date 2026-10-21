"""Packets damaged on the air (corruption.py): a header that can't be real, a "radio" that's really a damaged copy of a
busy radio's packet, and a busy radio's packet that won't decrypt with a public key. And what must NOT be flagged:
header-only frames, and one-offs nowhere near a busy radio's id."""
import json
import time

import alerts
import analytics
import corruption
import insights
import packetsearch
import store as store_mod
import topology
from synth import describe as plain

LOCAL = "!a0000001"
ORIGINAL = "!c0000057"     # busy radio: 25 decrypted positions, 4 hops, 40 bytes, relayed by 0x0d
PHANTOM = "!00000057"      # ORIGINAL with bits 0xc0 flipped in the first byte, and in the hop start (4 -> 2)
NOISE = "!a0000b52"        # one reception whose header says 6 hops left of 5: certain, nowhere near anyone
HEADER_ONLY = "!c0000056"  # one bit from ORIGINAL, but a 16-byte frame with no payload: a real oddity, not flagged
STRANGER = "!f00000a3"     # one undecryptable packet, far from every busy radio: unknown, not flagged
NEWBIE = "!b0000077"       # a real newcomer on a private channel: undecryptable at first, then a second packet


def rx(st, ts, frm, pkt, hs, hl, length=40, relay=13, channel=8):
    st.insert("rx_hops", ts=ts, from_id=frm, pkt_id=pkt, hop_start=hs, hop_limit=hl, hops=hs - hl, length=length,
              relay=relay, channel=channel, snr=-5.0, rssi=-90, to_id="^all", directed=0, encrypted=1, station=LOCAL)


def pk(st, ts, frm, pkt, port="POSITION_APP", channel=0):
    st.insert("packets", ts=ts, from_id=frm, to_id="^all", portnum=port, channel=channel, snr=-5.0, rssi=-90, hops=1,
              relay=13, pkt_id=pkt, pki=0, station=LOCAL,
              raw=json.dumps({"fromId": frm, "id": pkt, "transportMechanism": "TRANSPORT_LORA"}))


def build(tmp_path):
    path = tmp_path / "mesh.db"
    st = store_mod.Store(path)
    st.claim_station(LOCAL)
    t = time.time() - 3 * 3600
    for i in range(25):
        rx(st, t + i * 240, ORIGINAL, 1000 + i, 4, 3)
        pk(st, t + i * 240, ORIGINAL, 1000 + i)
    st.insert("node_info", ts=t, node=ORIGINAL, long_name="Busy Original", hw_model="RAK4631", station=LOCAL)
    rx(st, t + 3000, PHANTOM, 777, 2, 3)               # the damaged copy, as the relay re-sent it
    rx(st, t + 3001, PHANTOM, 777, 2, 2, relay=0x81)   # and one radio further on
    pk(st, t + 3000, PHANTOM, 777, port="ENCRYPTED", channel=8)
    rx(st, t + 3500, NOISE, 31, 5, 6, length=37, relay=0xc1, channel=60)
    rx(st, t + 3600, HEADER_ONLY, 41, 7, 0, length=16)
    pk(st, t + 3600, HEADER_ONLY, 41, port="ENCRYPTED", channel=8)
    rx(st, t + 3700, STRANGER, 51, 3, 2)
    pk(st, t + 3700, STRANGER, 51, port="ENCRYPTED", channel=8)
    rx(st, t + 4000, ORIGINAL, 2000, 4, 3)             # the busy radio's own packet, contents damaged
    pk(st, t + 4000, ORIGINAL, 2000, port="ENCRYPTED", channel=8)
    st.close()
    return path


def test_what_is_flagged(tmp_path):
    r = corruption.compute(build(tmp_path))
    s = r["senders"]
    assert set(s) == {PHANTOM, NOISE}
    assert s[PHANTOM]["confidence"] == "certain" and s[PHANTOM]["original"] == ORIGINAL and s[PHANTOM]["bits"] == 2
    cmp = {c["field"]: c for c in s[PHANTOM]["compare"]}
    assert cmp["sender"]["xor"] == "c0000000" and cmp["hop start"]["xor"] == "c0"   # the same two bits, twice
    assert "!c0000057" in s[PHANTOM]["why"] and "can't be real" in s[PHANTOM]["why"]
    assert s[NOISE]["confidence"] == "certain" and "original" not in s[NOISE]
    p = r["packets"]
    assert p[(ORIGINAL, 2000)]["confidence"] == "possible"
    assert (HEADER_ONLY, 41) not in p and (STRANGER, 51) not in p and (ORIGINAL, 1000) not in p


def test_likely_without_an_impossible_header(tmp_path):
    path = tmp_path / "mesh.db"
    st = store_mod.Store(path)
    t = time.time() - 3600
    for i in range(25):
        rx(st, t + i, ORIGINAL, i, 4, 3)
        pk(st, t + i, ORIGINAL, i)
    rx(st, t + 100, PHANTOM, 9, 4, 3)                  # the damage hit only the sender field
    pk(st, t + 100, PHANTOM, 9, port="ENCRYPTED", channel=8)
    st.close()
    f = corruption.compute(path)["senders"][PHANTOM]
    assert f["confidence"] == "likely" and corruption.phantom(f)
    assert "can't be real" not in f["why"]


def describer(path):
    flags = corruption.compute(path)["senders"]
    return lambda nid: dict(plain(nid), corrupt=corruption.badge(flags.get(nid)))


def test_counts_graph_and_health_leave_phantoms_out(tmp_path):
    path = build(tmp_path)
    d = describer(path)
    a = analytics.compute(path, "24h", LOCAL, d)
    ids = {n["id"] for n in a["nodes"]}
    assert PHANTOM in ids                                        # still listed, flagged
    assert a["kpis"]["damagedSenders"] == 1 and a["kpis"]["nodes"] == len(ids) - 1
    assert PHANTOM not in {n["id"] for n in a["newNodes"]}
    topo = topology.compute(path, "24h", LOCAL, d)
    assert PHANTOM not in {n["id"] for n in topo["nodes"]}
    found = insights.health(path, "24h", LOCAL, d)["findings"]
    mine = [f for f in found if (f.get("node") or {}).get("id") == PHANTOM]
    assert [f["kind"] for f in mine] == ["damaged-sender"]       # not also a hop-limit or "new radio" finding
    agg = [f for f in found if f["kind"] == "damaged-packets"]
    assert len(agg) == 1 and agg[0]["detail"].startswith("1 packet from Synthetic 0057")


def test_packet_browser_filter(tmp_path):
    path = build(tmp_path)
    bad = corruption.compute(path)["packets"]
    rows = packetsearch.search(path, "packets", {"corrupt": "any", "corruptPackets": bad}, station="*")["rows"]
    assert {(r["from_id"], r["pkt_id"]) for r in rows} == {(PHANTOM, 777), (ORIGINAL, 2000)}
    rows = packetsearch.search(path, "rx_hops", {"corrupt": "certain", "corruptPackets": bad}, station="*")["rows"]
    assert {r["from_id"] for r in rows} == {PHANTOM, NOISE}


class FakeMesh:
    local_id = LOCAL

    def describe(self, nid):
        return {"name": nid}

    def event(self, *a, **k):
        pass

    def broadcast(self, *a):
        pass


def test_new_radio_alerts_skip_phantoms_and_wait_on_one_offs(tmp_path, monkeypatch):
    path = tmp_path / "mesh.db"
    st = store_mod.Store(path)
    st.claim_station(LOCAL)
    a = alerts.Alerts(st, FakeMesh(), path, None)
    monkeypatch.setattr(alerts, "notify", lambda *x: None)
    s = a.settings()
    t = time.time() - 600
    for i in range(25):
        pk(st, t - 7200 + i, ORIGINAL, i)
        rx(st, t - 7200 + i, ORIGINAL, i, 4, 3)
    a.state["new_since"] = t - 60
    pk(st, t, PHANTOM, 777, port="ENCRYPTED", channel=8)
    rx(st, t, PHANTOM, 777, 2, 3)
    pk(st, t, NEWBIE, 900, port="ENCRYPTED", channel=0xb6)
    st.flush()
    a._check_new_nodes(s)
    assert st.query("SELECT COUNT(*) AS n FROM alerts WHERE kind = 'new-node'")[0]["n"] == 0   # phantom: never; newbie: waits
    pk(st, t + 30, NEWBIE, 901, port="ENCRYPTED", channel=0xb6)  # a second packet: a real radio after all
    st.flush()
    a._check_new_nodes(s)
    assert [r["node"] for r in st.query("SELECT node FROM alerts WHERE kind = 'new-node'")] == [NEWBIE]
    st.close()


def test_a_clean_newcomer_alerts_at_once(tmp_path, monkeypatch):
    path = tmp_path / "mesh.db"
    st = store_mod.Store(path)
    a = alerts.Alerts(st, FakeMesh(), path, None)
    monkeypatch.setattr(alerts, "notify", lambda *x: None)
    a.state["new_since"] = time.time() - 60
    pk(st, time.time(), NEWBIE, 1)
    st.flush()
    a._check_new_nodes(a.settings())
    assert [r["node"] for r in st.query("SELECT node FROM alerts WHERE kind = 'new-node'")] == [NEWBIE]
    st.close()


def test_mesh_describe_carries_the_flag(tmp_path, monkeypatch):
    import mesh as mesh_mod
    import runtime
    build(tmp_path)
    monkeypatch.setattr(runtime, "DATA_DIR", tmp_path)
    corruption._cache.clear()
    assert mesh_mod.corrupt_flag(PHANTOM)["original"] == ORIGINAL
    assert mesh_mod.corrupt_flag(ORIGINAL) is None
