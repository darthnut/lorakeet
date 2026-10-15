"""Traceroutes and messages from a Meshtastic radio only ever go to Meshtastic radios: a hub holds MeshCore stations'
rows too, and the library would turn any other id into some node number."""
import time
import types

import pytest

import alerts
import mesh as mesh_mod
import store as store_mod


def test_the_target_is_never_a_meshcore_radio(tmp_path):
    st = store_mod.Store(tmp_path / "mesh.db")
    st.claim_station("!a0000001")
    now = time.time()
    # a MeshCore station's first hearings, synced to this hub: newer, farther and never traced, so it would win on score
    st.insert("packets", ts=now - 60, from_id="mc:a0000000000b", to_id=None, portnum="MC_ADVERT", hops=5,
              station="mc:a0000000000c")
    st.insert("packets", ts=now - 3600, from_id="!a0000005", to_id="^all", portnum="TEXT_MESSAGE_APP", hops=2)
    st.flush()
    a = alerts.Alerts(st, types.SimpleNamespace(local_id="!a0000001"), tmp_path / "mesh.db", None)
    target, why = a._pick_target(a.settings())
    assert target == "!a0000005" and "never traced" in why
    st.close()


def test_traceroute_refuses_an_id_it_cant_send_to(tmp_path):
    st = store_mod.Store(tmp_path / "mesh.db")
    m = mesh_mod.Mesh(st, None)
    m.connected, m.iface, m.local_id = True, object(), "!a0000001"
    with pytest.raises(ValueError, match="Meshtastic"):
        m.traceroute("mc:a0000000000b")
    assert st.query("SELECT COUNT(*) AS n FROM traceroutes")[0]["n"] == 0   # nothing logged, nothing sent
    st.close()


def test_a_dm_to_a_meshcore_radio_is_refused(tmp_path):
    st = store_mod.Store(tmp_path / "mesh.db")
    m = mesh_mod.Mesh(st, None)
    m.connected, m.iface, m.local_id = True, object(), "!a0000001"
    for to in ("mc:a0000017c2be", "hello world"):
        with pytest.raises(ValueError, match="Meshtastic"):
            m.send_text("hi", to=to)
    assert st.query("SELECT COUNT(*) AS n FROM messages")[0]["n"] == 0
    st.close()
