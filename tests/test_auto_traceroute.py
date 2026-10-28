"""Auto-traceroute's busy-channel guard reads our radio's once-a-minute deviceMetrics: localStats carries channel
utilization only every ~15 min, so a guard reading it saw nothing most of the time and sent into a busy channel."""
import json
import time
import types

import alerts
import store as store_mod

LOCAL = "!a0000001"


def guard(tmp_path, kind, cu):
    st = store_mod.Store(tmp_path / "mesh.db")
    st.claim_station(LOCAL)
    st.insert("telemetry_full", ts=time.time() - 60, node=LOCAL, kind=kind, data=json.dumps({"channelUtilization": cu}),
              station=LOCAL)
    st.flush()
    m = types.SimpleNamespace(local_id=LOCAL, describe=lambda i: {"name": i}, event=lambda *a, **k: None,
                              broadcast=lambda *a: None)
    a = alerts.Alerts(st, m, tmp_path / "mesh.db", None)
    a._pick_target = lambda s: (None, "picked nothing")
    s = {**a.settings(), "autoTraceroute": True, "autoTracerouteMin": 0, "autoTracerouteMaxChUtil": 25}
    a._auto_traceroute(s)
    st.close()
    return a.last_auto["reason"]


def test_a_busy_channel_skips_the_traceroute(tmp_path):
    assert guard(tmp_path, "deviceMetrics", 40.0).startswith("skipped: channel busy")


def test_a_quiet_channel_goes_ahead(tmp_path):
    assert guard(tmp_path, "deviceMetrics", 5.0) == "picked nothing"
