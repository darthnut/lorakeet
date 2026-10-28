"""New-radio alerts: one each normally, but a station that just started logging or moved (its Wi-Fi changed to another
named network) rolls the radios it hears for the first time into one running alert with no notification, and a burst
of newcomers at an established station becomes one alert (one notification) that keeps growing for the hour. Found
when a station's first night at a new house raised 134 alerts, and a regional MeshCore flood 41 more."""
import json
import time
import types

import alerts
import store as store_mod

LOCAL, OTHER = "!a0000001", "!a0000002"


def mesh():
    return types.SimpleNamespace(local_id=LOCAL, describe=lambda i: {"name": f"name-{i[-4:]}"},
                                 event=lambda *a, **k: None, broadcast=lambda *a: None)


def pk(st, ts, frm, pkt, station=LOCAL):
    st.insert("packets", ts=ts, from_id=frm, to_id="^all", portnum="POSITION_APP", pkt_id=pkt, station=station, raw="{}")


def setup(tmp_path, monkeypatch, history_days=30):
    st = store_mod.Store(tmp_path / "mesh.db")
    st.claim_station(LOCAL)
    if history_days:
        pk(st, time.time() - history_days * 86400, "!c0000000", 1)
    toasts = []
    monkeypatch.setattr(alerts, "notify", lambda *x: toasts.append(x))
    a = alerts.Alerts(st, mesh(), tmp_path / "mesh.db", None)
    return st, a, toasts


def newcomers(st, a, ids, station=LOCAL):
    # this pass only: every packet earlier passes logged is before new_since, however fast the machine (a 1 ms window
    # failed on CI, where two passes ran within it and the earlier newcomers counted again)
    last = st.query("SELECT MAX(ts) AS t FROM packets")[0]["t"] or 0
    t = max(time.time(), last + 0.01)
    a.state["new_since"] = t - 0.001
    for i, nid in enumerate(ids):
        pk(st, t + i * 1e-4, nid, 100 + i, station)
    st.flush()
    a._check_new_nodes(a.settings())
    st.flush()


def rows(st, kind):
    return st.query("SELECT rowid, node, title, detail FROM alerts WHERE kind = ? ORDER BY rowid", kind)


def ids(n, start=0):
    return [f"!d{start + i:07x}" for i in range(n)]


def test_an_established_station_announces_each_newcomer(tmp_path, monkeypatch):
    st, a, _ = setup(tmp_path, monkeypatch)
    newcomers(st, a, ids(2))
    assert len(rows(st, "new-node")) == 2 and not rows(st, "new-nodes")
    st.close()


def test_a_new_station_rolls_its_first_days_into_one_quiet_alert(tmp_path, monkeypatch):
    st, a, toasts = setup(tmp_path, monkeypatch, history_days=0)
    newcomers(st, a, ids(6))
    newcomers(st, a, ids(4, 100))
    assert not rows(st, "new-node")
    (r,) = rows(st, "new-nodes")
    assert r["node"] == LOCAL and r["title"].startswith("10 new radios heard by") and "started logging" in r["detail"]
    assert not toasts                                            # the quiet period: no notifications at all
    st.close()


def test_a_moved_station_is_quiet_too(tmp_path, monkeypatch):
    st, a, toasts = setup(tmp_path, monkeypatch)
    for ts, net in ((time.time() - 20 * 86400, "HomeNet"), (time.time() - 7200, "no network"), (time.time() - 3600, "NewHouse")):
        st.insert("events", ts=ts, kind="network", detail=json.dumps({"network": net}), station=LOCAL)
    newcomers(st, a, ids(2))
    (r,) = rows(st, "new-nodes")
    assert "moved" in r["detail"] and r["title"].startswith("2 new radios") and not rows(st, "new-node") and not toasts
    st.close()


def test_a_burst_becomes_one_alert_that_grows_then_closes(tmp_path, monkeypatch):
    st, a, toasts = setup(tmp_path, monkeypatch)
    newcomers(st, a, ids(6))
    (r,) = rows(st, "new-nodes")
    assert r["title"].startswith("6 new radios") and "burst" in r["detail"] and len(toasts) == 1
    b = alerts.Alerts(st, mesh(), tmp_path / "mesh.db", None)    # restarted: carries on with the same alert
    newcomers(st, b, ids(2, 50))
    (r2,) = rows(st, "new-nodes")
    assert r2["rowid"] == r["rowid"] and r2["title"].startswith("8 new radios") and len(toasts) == 1
    b.state["newDigest"][LOCAL]["last"] -= alerts.NEW_BURST_WINDOW_S + 1    # an hour with nothing new
    newcomers(st, b, ids(1, 80))
    assert len(rows(st, "new-node")) == 1 and len(rows(st, "new-nodes")) == 1
    st.close()


def test_stations_are_judged_separately(tmp_path, monkeypatch):
    st, a, _ = setup(tmp_path, monkeypatch)
    pk(st, time.time() - 3600, "!c0000001", 2, station=OTHER)      # OTHER started an hour ago
    st.flush()
    newcomers(st, a, ids(3), station=OTHER)
    newcomers(st, a, ids(1, 40))
    assert [r["node"] for r in rows(st, "new-nodes")] == [OTHER] and len(rows(st, "new-node")) == 1
    st.close()
