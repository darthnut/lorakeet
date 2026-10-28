"""Radios behaving unlike themselves (anomalies.py), each judged against its own last two weeks: a steady radio gone
quiet, one sending or relaying far less, one heard much weaker; and the ones that must NOT be flagged: a steady radio,
a tracker that comes and goes, a visitor that suddenly relays a lot, and a link's ordinary daily swing. Plus the alert
loop raising each warning once and resolving it."""
import time
import types
from datetime import datetime

import alerts
import anomalies
import linkhistory
import store as store_mod
from synth import describe

LOCAL = "!a0000001"
STEADY = "!c0000001"    # every 20 min, all along
QUIET = "!c0000002"     # every 20 min until 8 hours ago
DROP = "!c0000003"      # every 20 min, then once an hour for the last day
TRACKER = "!c0000004"   # heard on only three days, early on
SIGNAL = "!c0000005"    # heard directly at +5 dB, then -6 dB for the last day
RELAY = "!d00000aa"     # relays a far radio's packets: 3 an hour, then one every 4 hours
VISITOR = "!d00000bb"   # never relayed anything, then 6 an hour for the last day
FAR = "!c0000006"


def build(tmp_path, now):
    path = tmp_path / "mesh.db"
    st = store_mod.Store(path)
    st.claim_station(LOCAL)
    start = now - 15 * 86400
    st.insert("node_info", ts=start, node=RELAY, long_name="Relay", station=LOCAL)      # makes the relay bytes resolvable
    st.insert("node_info", ts=start, node=VISITOR, long_name="Visitor", station=LOCAL)
    for nid in (RELAY, VISITOR):
        st.insert("links", ts=start, a=nid, b=LOCAL, snr=-8, source="direct", station=LOCAL)
    recent0 = now - anomalies.RECENT_S
    pid = 1
    t = start
    while t < now:
        pid += 10
        st.insert("telemetry_full", ts=t, node=LOCAL, kind="localStats", data="{}", station=LOCAL)   # logging all along
        k = int((t - start) // 1200)
        sent = [STEADY]
        if t < now - 8 * 3600:
            sent.append(QUIET)
        if t < recent0 or k % 3 == 0:
            sent.append(DROP)
        if 2 * 86400 <= t - start < 5 * 86400 and k % 3 == 0:
            sent.append(TRACKER)
        for i, nid in enumerate(sent):
            st.insert("packets", ts=t + i, from_id=nid, pkt_id=pid + i, portnum="POSITION_APP", hops=0, station=LOCAL, raw="{}")
        jitter = (k % 4) * 0.25
        st.insert("links", ts=t + 5, a=SIGNAL, b=LOCAL, snr=(5 if t < recent0 else -6) + jitter, source="direct", station=LOCAL)
        if t < recent0 or k % 12 == 0:
            st.insert("rx_hops", ts=t + 7, from_id=FAR, pkt_id=pid + 7, hops=1, relay=0xaa, snr=-9, station=LOCAL)
        if t >= recent0:
            for j in range(2):
                st.insert("rx_hops", ts=t + 8 + j, from_id=FAR, pkt_id=pid + 8 + j, hops=1, relay=0xbb, snr=-9, station=LOCAL)
        t += 1200
    st.close()
    return path


def test_each_kind_is_found_and_nothing_else(tmp_path):
    now = time.time()
    found = {(a["kind"], a["node"]["id"]): a for a in anomalies.detect(build(tmp_path, now), LOCAL, describe, now=now)}
    assert found[("quiet", QUIET)]["severity"] == "warn" and "silent for 8." in found[("quiet", QUIET)]["detail"]
    assert found[("traffic", DROP)]["severity"] == "warn"
    assert found[("relay", RELAY)]["severity"] == "warn"
    sig = found[("signal", SIGNAL)]
    assert sig["severity"] == "warn" and sig["key"] == f"signal:{SIGNAL}:{LOCAL}"
    assert set(found) == {("quiet", QUIET), ("traffic", DROP), ("relay", RELAY), ("signal", SIGNAL)}   # STEADY, TRACKER, VISITOR: nothing
    assert all(a["key"].startswith(a["kind"] + ":") for a in found.values())


def test_too_little_logging_judges_nobody(tmp_path):
    now = time.time()
    path = build(tmp_path, now)
    assert anomalies.detect(path, LOCAL, describe, now=now - 13 * 86400) == []    # a day and a half of history: no "usual" yet


def _daily(days=10, dip=-3.0, step_at=None, step=0.0):
    """Hourly readings at +2 dB with an afternoon dip (13:00-17:00) and optionally a step."""
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    obs = []
    for d in range(days, 0, -1):
        for h in range(24):
            t = today - d * 86400 + h * 3600 + 600
            s = 2.0 + (dip if 13 <= datetime.fromtimestamp(t).hour <= 17 else 0) + (step if step_at and t >= step_at else 0)
            obs.append((t, s + (h % 2) * 0.25))
    return obs


def test_daily_pattern_finds_the_afternoon_dip():
    p = linkhistory.daily_pattern(_daily())
    assert p and 14 <= p["weakest"] <= 16 and p["swing"] >= 2
    assert p["offsets"][3] > p["offsets"][15]
    assert linkhistory.daily_pattern(_daily(dip=0)) is None      # a flat link has no pattern worth showing


def test_deseasoned_readings_keep_a_real_step_and_lose_the_swing():
    obs = _daily()
    flat = linkhistory.deseasoned(obs, linkhistory.daily_pattern(obs))
    spread = lambda v: max(v) - min(v)  # noqa: E731
    assert spread([s for _, s in flat]) < spread([s for _, s in obs]) - 1.5
    mid = obs[len(obs) // 2][0]
    stepped = _daily(step_at=mid, step=-8)
    steps = linkhistory.find_steps(linkhistory.deseasoned(stepped, linkhistory.daily_pattern(stepped)))
    assert len(steps) == 1 and -9 <= steps[0]["delta"] <= -7 and abs(steps[0]["ts"] - mid) <= 86400


def test_the_alert_loop_raises_each_warning_once_and_resolves_it(tmp_path, monkeypatch):
    st = store_mod.Store(tmp_path / "mesh.db")
    mesh = types.SimpleNamespace(local_id=LOCAL, describe=lambda i: {"name": i}, event=lambda *a, **k: None,
                                 broadcast=lambda *a: None)
    warn = {"kind": "quiet", "severity": "warn", "node": {"id": QUIET, "name": "Hilltop"}, "title": "Unusually quiet",
            "detail": "Silent for 8 h.", "evidence": [], "key": f"quiet:{QUIET}"}
    info = dict(warn, kind="traffic", severity="info", key=f"traffic:{QUIET}")
    now_found = [warn, info]
    monkeypatch.setattr(anomalies, "detect", lambda *a, **k: list(now_found))
    a = alerts.Alerts(st, mesh, tmp_path / "mesh.db", None)
    a._check_anomalies(a.settings())
    a._check_anomalies(a.settings())
    st.flush()
    rows = st.query("SELECT kind, node, title, resolved_ts FROM alerts WHERE kind = 'anomaly'")
    assert rows == [{"kind": "anomaly", "node": QUIET, "title": "Hilltop: unusually quiet", "resolved_ts": None}]   # once; info isn't an alert
    b = alerts.Alerts(st, mesh, tmp_path / "mesh.db", None)      # restarted: remembers it's open
    b._check_anomalies(b.settings())
    st.flush()
    assert len(st.query("SELECT 1 FROM alerts WHERE kind = 'anomaly'")) == 1
    now_found.clear()                                            # it's heard again
    b._check_anomalies(b.settings())
    st.flush()
    assert st.query("SELECT resolved_ts FROM alerts WHERE kind = 'anomaly'")[0]["resolved_ts"] is not None
    st.close()
