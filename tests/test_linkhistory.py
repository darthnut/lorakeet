"""Link history (linkhistory.py): a link's signal per day, the step where it changed (and the station event logged
there), a steady link that must not get a step, an inferred relay link, and the before/after comparison."""
import json
import time
from datetime import datetime, timedelta

import linkhistory
import store as store_mod
from synth import describe

LOCAL = "!a0000001"
STEPPED = "!c0000001"   # heard directly: -16 dB for 5 days, then -4 dB (a better antenna)
STEADY = "!c0000002"    # heard directly at +6 dB all along
RELAY = "!d00000aa"     # relays a far radio's packets to the station (relay byte 0xaa)
FAR = "!c0000003"       # only ever heard through RELAY
GONE = "!c0000004"      # heard only in the first days


def build(tmp_path):
    path = tmp_path / "mesh.db"
    st = store_mod.Store(path)
    st.claim_station(LOCAL)
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    start = today - timedelta(days=10)
    switch = (start + timedelta(days=5)).timestamp()
    for d in range(11):
        for h in range(0, 24, 2):
            t = (start + timedelta(days=d, hours=h)).timestamp()
            if t > time.time():
                break
            st.insert("telemetry_full", ts=t, node=LOCAL, kind="localStats", data="{}", station=LOCAL)   # logging
            jitter = (h % 3) * 0.5
            st.insert("links", ts=t, a=STEPPED, b=LOCAL, snr=(-16 if t < switch else -4) + jitter, source="direct", station=LOCAL)
            st.insert("links", ts=t + 60, a=STEADY, b=LOCAL, snr=6 + jitter, source="direct", station=LOCAL)
            st.insert("packets", ts=t, from_id=STEPPED, pkt_id=int(t), portnum="POSITION_APP", hops=0, station=LOCAL, raw="{}")
            st.insert("rx_hops", ts=t + 120, from_id=FAR, pkt_id=int(t) + 1, hops=1, relay=0xaa, snr=-9 + jitter, station=LOCAL)
            if d < 3:
                st.insert("links", ts=t + 30, a=GONE, b=LOCAL, snr=-12, source="direct", station=LOCAL)
                st.insert("packets", ts=t + 30, from_id=GONE, pkt_id=int(t) + 2, portnum="TELEMETRY_APP", hops=0, station=LOCAL, raw="{}")
    st.insert("node_info", ts=start.timestamp(), node=RELAY, long_name="Relay", station=LOCAL)   # makes 0xaa resolvable
    st.insert("links", ts=start.timestamp(), a=RELAY, b=LOCAL, snr=-8, source="direct", station=LOCAL)
    for net, t in (("HomeWifi", start.timestamp() + 600), ("HomeWifi", switch - 7200), ("no network", switch - 60),
                   ("OtherWifi", switch + 300)):
        st.insert("events", ts=t, kind="network", detail=json.dumps({"network": net}), station=LOCAL)
    st.close()
    return path, switch


def test_history_steps_and_events(tmp_path):
    path, switch = build(tmp_path)
    h = linkhistory.history(path, "30d", LOCAL, describe)
    assert h["bucket"] == "day"
    by = {l["a"]: l for l in h["links"]}
    s = by[STEPPED]
    assert len(s["steps"]) == 1
    step = s["steps"][0]
    assert abs(step["ts"] - switch) <= 6 * 3600 and 11 <= step["delta"] <= 13
    assert [e["kind"] for e in step["events"]] == ["network"] and "HomeWifi to OtherWifi" in step["events"][0]["text"]
    assert by[STEADY]["steps"] == [] and 6 <= by[STEADY]["med"] <= 7
    assert h["links"][0]["a"] == STEPPED                     # the biggest change sorts first
    days = [b for b in s["series"] if b.get("n")]
    assert days[0]["med"] < -14 and days[-1]["med"] > -5     # per-day medians before and after
    assert [e["text"] for e in h["events"] if e["kind"] == "network"] == ["Wi-Fi changed from HomeWifi to OtherWifi (the station probably moved)"]


def test_relayed_last_hop_is_an_inferred_link(tmp_path):
    path, _ = build(tmp_path)
    by = {(l["a"], l["b"]): l for l in linkhistory.history(path, "30d", LOCAL, describe)["links"]}
    r = by[(RELAY, LOCAL)]
    assert r["sources"].get("relay") and not by.get((FAR, LOCAL))    # the far radio isn't a link of its own
    assert r["measured"] is True                                      # it was also heard directly once


def test_compare_before_and_after(tmp_path):
    path, switch = build(tmp_path)
    c = linkhistory.compare(path, switch, 5, LOCAL, describe)
    by = {l["a"]: l for l in c["links"]}
    assert by[STEPPED]["before"]["med"] < -14 and by[STEPPED]["after"]["med"] > -5 and by[STEPPED]["delta"] > 10
    assert by[GONE]["status"] == "lost" and by[GONE]["after"] is None
    assert by[STEADY]["status"] is None and abs(by[STEADY]["delta"]) < 1
    assert [r["id"] for r in c["radios"]["onlyBefore"]] == [GONE]
    assert by[STEPPED]["perDayBefore"] and by[STEPPED]["perDayBefore"] > 10   # 12 readings a logged day
    assert any(e["kind"] == "network" for e in c["events"])


def test_no_step_without_enough_on_both_sides():
    t = time.time() - 86400 * 3
    obs = [(t + i * 3600, -16.0) for i in range(30)] + [(t + 31 * 3600, -2.0)]   # one stray reading isn't a step
    assert linkhistory.find_steps(obs) == []


def test_a_relay_byte_never_means_a_radio_not_yet_heard(tmp_path):
    path, _ = build(tmp_path)
    st = store_mod.Store(path)
    early = time.time() - 40 * 86400                       # a month before RELAY was first heard
    for i in range(10):
        st.insert("rx_hops", ts=early + i * 600, from_id=FAR, pkt_id=99000 + i, hops=1, relay=0xaa, snr=-19.0, station=LOCAL)
    st.close()
    r = {(l["a"], l["b"]): l for l in linkhistory.history(path, "all", LOCAL, describe)["links"]}[(RELAY, LOCAL)]
    assert r["first"] > early + 86400                       # those old readings weren't pinned on it
