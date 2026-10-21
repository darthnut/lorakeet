"""A radio's stand-in user for a node it has no NodeInfo for ("Meshtastic a90d", hardware UNSET, no key) is not an
identity: it isn't logged, doesn't rename the node, and old rows of it are left out of the identity history.
And a weak key shared by many radios is called the firmware problem it is, not a cloned device."""
import base64
import hashlib
import time

import insights
import keyflags
import mesh as mesh_mod
import node_analytics
import radioid
import store as store_mod
import weak_keys
from synth import describe

NODE = "!0000a90d"
REAL = {"id": NODE, "longName": "Hilltop Base", "shortName": "a90d", "hwModel": "RAK4631",
        "publicKey": base64.b64encode(b"k" * 32).decode()}
STAND_IN = {"id": NODE, "longName": "Meshtastic a90d", "shortName": "a90d", "hwModel": "UNSET"}


def test_what_counts_as_a_stand_in():
    assert radioid.is_placeholder_user(NODE, STAND_IN)
    assert radioid.is_placeholder_user(NODE, {"longName": "Meshtastic a90d", "shortName": "a90d"})   # UNSET omitted
    assert not radioid.is_placeholder_user(NODE, REAL)
    # a real radio nobody renamed still names its hardware, or carries a key
    assert not radioid.is_placeholder_user(NODE, {**STAND_IN, "hwModel": "HELTEC_V4"})
    assert not radioid.is_placeholder_user(NODE, {**STAND_IN, "publicKey": REAL["publicKey"]})
    assert not radioid.is_placeholder_user(NODE, {**STAND_IN, "longName": "Bob's attic"})
    assert not radioid.is_placeholder_user(NODE, None)


def test_a_stand_in_is_not_logged_and_does_not_rename(tmp_path):
    st = store_mod.Store(tmp_path / "mesh.db")
    m = mesh_mod.Mesh(st, None)
    m._track_identity(NODE, REAL)
    m._track_identity(NODE, STAND_IN)            # a station's fresh radio held only the stand-in
    st.flush()
    assert [r["long_name"] for r in st.query("SELECT long_name FROM node_info WHERE node = ?", NODE)] == ["Hilltop Base"]
    # the live node DB holding the stand-in doesn't hide the logged name, nor make an unheard node look announced
    m._nodes = lambda: {NODE: {"num": 0x0000a90d, "user": STAND_IN},
                        "!b0000001": {"num": 0xb0000001, "user": {"longName": "Meshtastic 0001", "shortName": "0001"}}}
    assert m.describe(NODE)["name"] == "Hilltop Base" and m.describe(NODE)["hw"] == "RAK4631"
    assert m.name(NODE) == "Hilltop Base"
    assert m.describe("!b0000001")["announced"] is False
    st.close()


def test_logged_stand_ins_are_left_out_of_history(tmp_path):
    path = tmp_path / "mesh.db"
    st = store_mod.Store(path)
    t = time.time() - 3600
    st.insert("packets", ts=t, from_id=NODE, pkt_id=1, portnum="TELEMETRY_APP", station="!a0000001")
    st.insert("node_info", ts=t, node=NODE, long_name="Hilltop Base", short_name="a90d", hw_model="RAK4631",
              role="CLIENT", public_key=REAL["publicKey"], station="!a0000001")
    # what an older collector synced: the stand-in, newer than the real identity
    st.insert("node_info", ts=t + 60, node=NODE, long_name="Meshtastic a90d", short_name="a90d", hw_model="UNSET",
              role="CLIENT", station="!a0000001")
    st.close()
    r = node_analytics.compute_node(path, NODE, "24h", "!a0000001", describe)
    assert [i["long_name"] for i in r["identity"]] == ["Hilltop Base"]
    st = store_mod.Store(path)
    m = mesh_mod.Mesh(st, None)                    # names loaded at start-up skip it too
    assert m.known[NODE]["long_name"] == "Hilltop Base"
    st.close()


def test_a_weak_key_on_many_radios_is_not_called_cloned(tmp_path, monkeypatch):
    monkeypatch.setattr(weak_keys, "LOW_ENTROPY_SHA256", {hashlib.sha256(b"w" * 32).hexdigest()})
    weak = base64.b64encode(b"w" * 32).decode()
    path = tmp_path / "mesh.db"
    st = store_mod.Store(path)
    t = time.time() - 3600
    st.insert("packets", ts=t, from_id="!a0000001", pkt_id=1, portnum="TELEMETRY_APP", station="!a0000001")
    for i, nid in enumerate(("!c0000001", "!c0000002", "!c0000003")):
        st.insert("node_info", ts=t + i, node=nid, long_name=f"Radio {i}", hw_model="RAK4631", public_key=weak,
                  station="!a0000001")
    st.close()
    dup = [f for f in insights.health(path, "24h", "!a0000001", describe)["findings"] if f["kind"] == "duplicate-key"]
    assert len(dup) == 1 and dup[0]["title"] == "Several radios have the same weak key"
    assert "not a cloned device" in dup[0]["detail"] and "firmware" in dup[0]["detail"]
    flag = keyflags.classify({"!c0000001": weak, "!c0000002": weak}, {})["!c0000001"]
    assert "firmware problem" in keyflags.text(flag)
