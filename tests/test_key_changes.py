"""'Public key changed' counts only keys heard on the air, dated when each was first heard."""
import json
import time

import insights
import store as store_mod
from synth import describe

LOCAL, CAR = "!a0000001", "!a0000002"
STABLE, CHANGED = "!c0000001", "!c0000002"
OLD, NEW = "T0xEa2V5" + "A" * 36, "TmV3a2V5" + "B" * 36


def nodeinfo(store, ts, nid, key, station):
    store.insert("packets", ts=ts, from_id=nid, pkt_id=int(ts) % 100000, portnum="NODEINFO_APP", station=station,
                 raw=json.dumps({"decoded": {"portnum": "NODEINFO_APP", "user": {"id": nid, "publicKey": key}}}))
    store.insert("node_info", ts=ts, node=nid, public_key=key, station=station)


def test_a_stations_stored_list_is_not_a_key_change(tmp_path):
    path = tmp_path / "mesh.db"
    store = store_mod.Store(path)
    t = time.time() - 6 * 3600
    store.insert("packets", ts=t - 60, from_id=LOCAL, pkt_id=1, portnum="TELEMETRY_APP", station=LOCAL)
    nodeinfo(store, t, STABLE, NEW, LOCAL)                       # heard on the air with its (only) key
    store.insert("node_info", ts=t + 3600, node=STABLE, public_key=OLD, station=CAR)  # a car's radio remembered an old one
    nodeinfo(store, t, CHANGED, OLD, LOCAL)                      # this one really changed: old key, then a new one
    nodeinfo(store, t + 7200, CHANGED, NEW + "x", LOCAL)
    nodeinfo(store, t + 9000, CHANGED, NEW + "x", CAR)
    store.close()
    found = {f["node"]["id"]: f for f in insights.health(path, "24h", LOCAL, describe)["findings"] if f["kind"] == "key-changed"}
    assert STABLE not in found
    assert CHANGED in found and time.strftime("%Y-%m-%d %H:%M", time.localtime(t + 7200)) in found[CHANGED]["detail"]
