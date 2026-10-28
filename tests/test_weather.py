"""Weather at the stations (weather.py): only a grid square's centre is ever sent, nearby stations share one request,
the first fetch backfills to when logging began and later ones only refresh, failures are recorded without raising; a
link whose signal follows the temperature is recognised; and the weaker-signal check doesn't flag a link that only
dipped because of a heat wave (the same drop with no weather to explain it is flagged)."""
import io
import json
import math
import time
import urllib.parse

import analytics
import anomalies
import store as store_mod
import weather
from demo import PORTLAND
from synth import describe

LOCAL, NEAR, FAR = "!a0000001", "!a0000002", "!a0000003"
SIG = "!c0000001"
LAT, LON = PORTLAND


def temp_at(t, heat_from=None):
    """10 +- 5 C over the day, 15 C warmer from heat_from on."""
    return 10 + 5 * math.sin(2 * math.pi * (t % 86400) / 86400) + (15 if heat_from and t >= heat_from else 0)


class FakeMeteo:
    def __init__(self, heat_from=None, fail=False):
        self.asked, self.heat_from, self.fail = [], heat_from, fail

    def __call__(self, req, timeout=None):
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(req.full_url).query)
        self.asked.append(q)
        if self.fail:
            raise OSError("network down")
        now = int(time.time() // 3600) * 3600
        hours = list(range(now - int(q["past_days"][0]) * 86400, now + 86400, 3600))
        h = {"time": hours}
        for name in weather.VARS:
            h[name] = [temp_at(t, self.heat_from) if name == "temperature_2m" else 50 if name == "relative_humidity_2m" else 0
                       for t in hours]
        return io.BytesIO(json.dumps({"elevation": 50, "hourly": h}).encode())


def test_squares_share_requests_and_only_the_centre_is_sent(tmp_path, monkeypatch):
    monkeypatch.setattr(analytics, "STATION_LOCATIONS", {
        LOCAL: (LAT + 0.0123, LON + 0.0111, None), NEAR: (LAT + 0.0301, LON - 0.0212, None),   # ~3 km apart
        FAR: (LAT + 0.5, LON + 0.6, None)})
    st = store_mod.Store(tmp_path / "mesh.db")
    st.claim_station(LOCAL)
    st.insert("packets", ts=time.time() - 10 * 86400, from_id=SIG, pkt_id=1, portnum="POSITION_APP", station=LOCAL, raw="{}")
    st.close()
    meteo = FakeMeteo()
    w = weather.Weather(tmp_path / "mesh.db", opener=meteo)
    w.tick()
    assert len(meteo.asked) == 2                                   # LOCAL and NEAR share a square
    for q in meteo.asked:
        for k in ("latitude", "longitude"):
            assert len(q[k][0].split(".")[1]) == 1                 # one decimal: the square's centre, nothing finer
        assert set(q) == {"latitude", "longitude", "hourly", "past_days", "forecast_days", "timeformat", "timezone"}
    assert {q["past_days"][0] for q in meteo.asked} == {"12"}      # backfilled to the first logged packet (plus a day)
    w.tick()
    assert len(meteo.asked) == 2                                   # nothing again within the hour
    w.tick(now=time.time() + weather.REFRESH_S + 5)
    assert len(meteo.asked) == 4 and meteo.asked[-1]["past_days"] == [str(weather.RECENT_DAYS)]
    sq = {s["cell"]: s for s in weather.squares(tmp_path / "mesh.db")}
    assert weather.cell_of(LAT + 0.0123, LON + 0.0111) in sq and all(s["lastError"] is None for s in sq.values())
    rows = weather.series(tmp_path / "mesh.db", weather.cell_of(LAT, LON), time.time() - 86400, time.time() + 86400)
    assert rows and max(rows) <= time.time()                       # forecast hours aren't kept


def test_a_failed_fetch_is_recorded_not_raised(tmp_path, monkeypatch):
    monkeypatch.setattr(analytics, "STATION_LOCATIONS", {LOCAL: (LAT, LON, None)})
    w = weather.Weather(tmp_path / "mesh.db", opener=FakeMeteo(fail=True))
    w.tick()
    (sq,) = weather.squares(tmp_path / "mesh.db")
    assert "network down" in sq["lastError"]


def test_fit_finds_a_link_that_follows_the_temperature():
    t0 = int(time.time() // 3600) * 3600 - 10 * 86400
    wx = {t0 + i * 3600: {"temp_c": temp_at(t0 + i * 3600), "rh": 50} for i in range(240)}
    obs = [(t + 60, 5 - 0.3 * w["temp_c"] + (i % 3) * 0.25) for i, (t, w) in enumerate(wx.items())]
    f = weather.fit(obs, wx)
    assert f["follows"] == "temp_c" and f["temp_c"]["r"] < -0.9 and abs(f["temp_c"]["slope"] + 0.3) < 0.05
    assert "rh" not in f                                            # humidity never varied: not judged
    flat = [(t, 5 + (i % 3) * 0.25) for i, (t, _) in enumerate(obs)]
    assert weather.fit(flat, wx)["follows"] is None


def _signal_log(tmp_path, monkeypatch, with_weather):
    """A radio heard directly every 20 min whose signal falls 0.5 dB per degree; a heat wave for the last day."""
    monkeypatch.setattr(analytics, "STATION_LOCATIONS", {LOCAL: (LAT, LON, None)})
    now = time.time()
    heat = now - anomalies.RECENT_S
    st = store_mod.Store(tmp_path / "mesh.db")
    st.claim_station(LOCAL)
    t = now - 15 * 86400
    while t < now:
        st.insert("telemetry_full", ts=t, node=LOCAL, kind="localStats", data="{}", station=LOCAL)
        st.insert("links", ts=t, a=SIG, b=LOCAL, snr=5 - 0.5 * (temp_at(t, heat) - 10) + (int(t) % 3) * 0.25,
                  source="direct", station=LOCAL)
        t += 1200
    st.insert("packets", ts=now - 15 * 86400, from_id=SIG, pkt_id=1, portnum="POSITION_APP", station=LOCAL, raw="{}")
    st.close()
    if with_weather:
        weather.Weather(tmp_path / "mesh.db", opener=FakeMeteo(heat_from=heat)).tick()
    return tmp_path / "mesh.db", now


def test_a_heat_wave_dip_is_not_a_weaker_signal(tmp_path, monkeypatch):
    path, now = _signal_log(tmp_path, monkeypatch, with_weather=True)
    assert not [a for a in anomalies.detect(path, LOCAL, describe, now=now) if a["kind"] == "signal"]


def test_the_same_dip_without_weather_is_flagged(tmp_path, monkeypatch):
    path, now = _signal_log(tmp_path, monkeypatch, with_weather=False)
    found = [a for a in anomalies.detect(path, LOCAL, describe, now=now) if a["kind"] == "signal"]
    assert [a["severity"] for a in found] == ["warn"]
