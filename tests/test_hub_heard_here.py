"""After a hub restarts, "heard here" is seeded from this Lorakeet's own radios only: a radio only a collector heard
is that station's reception (rx_remote), not one our radio heard at the collector's SNR."""
import time

import mesh as mesh_mod
import store as store_mod

HOME, PI = "!a0000001", "!b0000002"


def test_a_collectors_receptions_arent_heard_here(tmp_path):
    st = store_mod.Store(tmp_path / "mesh.db")
    st.claim_station(HOME)
    now = time.time()
    st.insert("packets", ts=now - 60, from_id="!c0000001", to_id="^all", portnum="POSITION_APP", snr=4.0, station=HOME)
    st.insert("packets", ts=now - 30, from_id="!c0000002", to_id="^all", portnum="POSITION_APP", snr=-9.0, station=PI)
    st.insert("packets", ts=now - 90, from_id="!c0000003", to_id="^all", portnum="POSITION_APP", snr=1.0)  # pre-station rows
    st.flush()
    m = mesh_mod.Mesh(st, None)
    assert set(m.rx) == {"!c0000001", "!c0000003"}
    st.close()
