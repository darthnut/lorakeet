"""The hub's "station isn't syncing" alert: once per silence (not again after every restart), under the name this
hub gave the station, and never for a station whose pairing was revoked."""
import json
import time
import types

import alerts
import store as store_mod
from config import CFG

OLD, NEW = "!a0000002", "!a0000003"


def _alerts(st, path, revoked=()):
    mesh = types.SimpleNamespace(local_id="!a0000001", describe=lambda i: {"name": i}, event=lambda *a, **k: None,
                                 broadcast=lambda *a: None)
    a = alerts.Alerts(st, mesh, path, None)
    a.pairings = types.SimpleNamespace(is_revoked=lambda owner: owner in revoked)
    return a


def test_a_silent_station_is_raised_once_by_its_name_and_never_when_revoked(tmp_path, monkeypatch):
    monkeypatch.setitem(CFG["sync"], "mode", "hub")
    st = store_mod.Store(tmp_path / "mesh.db")
    old = time.time() - 3 * 3600
    for k, v in (("sync_hub", {OLD: {"last": old}, NEW: {"last": old}}),
                 ("station_owners", {OLD: "pair:gone", NEW: "pair:live"}),
                 ("station_overrides", {NEW: {"name": "Garage Pi"}})):
        st.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", k, json.dumps(v))
    a = _alerts(st, tmp_path / "mesh.db", revoked={"pair:gone"})
    a._check_stations(a.settings())
    st.flush()
    raised = st.query("SELECT node, title FROM alerts WHERE kind = 'station-silent'")
    assert raised == [{"node": NEW, "title": "Station Garage Pi isn't syncing"}]    # the revoked one: nothing
    b = _alerts(st, tmp_path / "mesh.db", revoked={"pair:gone"})                                           # the hub restarted
    b._check_stations(b.settings())
    st.flush()
    assert len(st.query("SELECT 1 FROM alerts WHERE kind = 'station-silent'")) == 1  # not raised again
    st.close()
