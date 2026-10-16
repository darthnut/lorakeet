"""The network graph and replay from MeshCore paths (topology.py): each reception names the repeaters it came through
by key prefix; a prefix becomes a radio only when one known radio has it (repeaters preferred), never by guess."""
import time

import store as store_mod
import topology

MC = "mc:c0000099d0d0"                                   # the station
REP, REP2, REP3 = "mc:a0000017c2be", "mc:d0000011aa01", "mc:d0000022bb02"   # repeaters; REP2/REP3 share "d0"
CHAT, LOOKALIKE = "mc:b00000aa11ee", "mc:a0000088ff00"   # LOOKALIKE: a chat radio sharing REP's "a0"


def _build(path):
    st = store_mod.Store(path)
    st.claim_station(MC)
    now = time.time()
    for node, role in ((REP, "repeater"), (REP2, "repeater"), (REP3, "repeater"), (CHAT, "chat"), (LOOKALIKE, "chat")):
        st.insert("node_info", ts=now - 900, node=node, long_name=node[-4:], role=role, public_key=node[3:] + "00" * 26)
    # a station logs each packet's first hearing too (the analytics' time window starts at the first packet)
    st.insert("packets", ts=now - 900, from_id=CHAT, portnum="MC_ADVERT", hops=1, pkt_id=1)
    rx = [(CHAT, "a0", 1, "ADVERT"),       # chat -> REP -> station
          (CHAT, "", 0, "ADVERT"),          # heard direct
          (None, "a0,d0", 2, "GRP_TXT"),    # no sender; second hop ambiguous
          (None, "17", 1, "TRACE")]         # a trace's path is SNRs, not prefixes
    for i, (frm, p, hops, name) in enumerate(rx):
        st.insert("mc_rx", ts=now - 300 + i, snr=5.0, rssi=-90, route="flood", path=p, hops=hops, payload_name=name,
                  packet_hash=f"{i:016x}", from_id=frm, frame="00")
    st.flush()
    return st


def test_paths_become_links_only_where_the_prefix_is_certain(tmp_path):
    st = _build(tmp_path / "mesh.db")
    resolve = topology.mc_prefix_resolver(st.db)
    assert resolve("a0") == REP          # the repeater, though a chat radio shares the prefix
    assert resolve("d0") is None         # two repeaters: never guessed
    assert resolve("ee") is None         # nobody
    t = topology.compute(tmp_path / "mesh.db", "24h", MC, lambda i: {"name": i})
    edges = {(e["a"], e["b"]): e for e in t["edges"]}
    assert set(edges[tuple(sorted((CHAT, REP)))]["sources"]) == {"path"}
    assert edges[tuple(sorted((REP, MC)))]["snr"] == 5.0                              # the last hop's SNR
    assert edges[tuple(sorted((CHAT, MC)))]["measured"]                              # heard direct
    assert not any(REP2 in k or REP3 in k for k in edges)                            # the ambiguous "d0"
    st.close()


def test_the_replay_follows_the_chain(tmp_path):
    st = _build(tmp_path / "mesh.db")
    ev = [e for e in topology.replay(tmp_path / "mesh.db", "24h", MC, lambda i: {"name": i})["events"] if e["kind"] == "mc"]
    assert len(ev) == 4
    assert ev[0]["from"] == CHAT and ev[0]["path"] == [REP] and ev[0]["group"] == "Node info"
    assert ev[2]["from"] is None and ev[2]["path"] == [REP, None] and ev[2]["prefixes"] == ["a0", "d0"]
    assert "path" not in ev[3]                                                       # the trace
    st.close()
