"""Noise at the stations (noise.py): the noise floor from the radio's own reports (the firmware's "not measured yet"
value skipped), failed receptions from counters that reset at each boot, restarts from uptime going backwards, a
lasting step and a noisy spell; and the anomaly checks built on them: a station noisier than usual, a radio that keeps
restarting, and a weaker link blamed on the station when the station's own noise rose."""
import json
import time

import anomalies
import noise
import store as store_mod
from synth import describe

LOCAL = "!a0000001"
SIG = "!c0000001"


def stats(st, ts, nf, bad, rx, up):
    st.insert("telemetry_full", ts=ts, node=LOCAL, kind="localStats", station=LOCAL,
              data=json.dumps({"noiseFloor": nf, "numPacketsRxBad": bad, "numPacketsRx": rx, "uptimeSeconds": up}))


def build(tmp_path, now, noisy_from=None, restarts_from=None, link_drop=False):
    """15 days of reports every 15 min at -90 dBm; optionally +10 dB from noisy_from, and a reboot every 2 hours from
    restarts_from. A radio heard directly at +5 dB (down 10 dB with link_drop when the noise rises)."""
    st = store_mod.Store(tmp_path / "mesh.db")
    st.claim_station(LOCAL)
    t, up, bad, rx, k = now - 15 * 86400, 0, 0, 0, 0
    while t < now:
        loud = bool(noisy_from and t >= noisy_from)
        if restarts_from and t >= restarts_from and k % 8 == 0:
            up, bad, rx = 0, 0, 0                                   # rebooted: uptime and counters start over
        nf = -120 if up < 1800 and k < 2 else -90 + (10 if loud else 0) + (k % 3)
        stats(st, t, nf, bad, rx, up)
        st.insert("links", ts=t + 5, a=SIG, b=LOCAL, snr=5 - (10 if loud and link_drop else 0) + (k % 4) * 0.25,
                  source="direct", station=LOCAL)
        st.insert("packets", ts=t + 5, from_id=SIG, pkt_id=k + 1, portnum="POSITION_APP", hops=0, station=LOCAL, raw="{}")
        t, up, k = t + 900, up + 900, k + 1
        bad, rx = bad + (3 if loud else 1), rx + 20
    st.close()
    return tmp_path / "mesh.db"


def test_report_reads_noise_failures_steps_and_restarts(tmp_path):
    now = time.time()
    path = build(tmp_path, now, noisy_from=now - 5 * 86400, restarts_from=now - 86400)
    (s,) = noise.report(path, "30d", LOCAL, describe)["stations"]
    assert s["noise"]["lo"] >= -90 and s["noise"]["hi"] <= -78            # the -120 placeholders are skipped
    (step,) = s["steps"]
    assert 9 <= step["delta"] <= 11 and abs(step["ts"] - (now - 5 * 86400)) <= 2 * 86400
    # (steps are dated by day: up to two days off, by the time of day the test runs)
    assert s["restarts"]["lastDay"] >= 10 and s["restarts"]["inRange"] >= s["restarts"]["lastDay"]
    f = s["failed"]
    assert f["bad"] > 0 and f["received"] > 0 and 4 < f["pct"] < 14       # never negative across the reboots


def test_a_noisy_spell(tmp_path):
    now = time.time()
    path = build(tmp_path, now, noisy_from=now - 3 * 3600)
    (s,) = noise.report(path, "7d", LOCAL, describe)["stations"]
    (spell,) = s["spells"]
    assert spell["start"] >= now - 3 * 3600 - 900 and spell["above"] >= 9


def test_anomalies_noisier_and_restarting(tmp_path):
    now = time.time()
    path = build(tmp_path, now, noisy_from=now - 86400 + 1800, restarts_from=now - 86400 + 1800)
    found = {a["kind"]: a for a in anomalies.detect(path, LOCAL, describe, now=now) if a["node"]["id"] == LOCAL}
    assert found["noise"]["severity"] == "warn" and "usual -89 dBm" in found["noise"]["detail"]
    assert found["restarts"]["severity"] == "warn"


def test_a_quiet_station_and_a_steady_radio_raise_nothing(tmp_path):
    now = time.time()
    path = build(tmp_path, now)
    assert not [a for a in anomalies.detect(path, LOCAL, describe, now=now) if a["kind"] in ("noise", "restarts", "signal")]


def test_a_weaker_link_at_a_noisier_station_points_at_the_station(tmp_path):
    now = time.time()
    path = build(tmp_path, now, noisy_from=now - 86400 + 1800, link_drop=True)
    (sig,) = [a for a in anomalies.detect(path, LOCAL, describe, now=now) if a["kind"] == "signal"]
    assert "noise floor rose 10 dB" in sig["detail"] and "rather than the link" in sig["detail"]
