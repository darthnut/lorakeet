"""The alarm for reception logging that stops silently (e.g. a firmware update rewording the debug log)."""
import json
import time

import alerts
import server

LOCAL = "!a0000001"


class FakeMesh:
    local_id = LOCAL
    log_counts = {"lines": 0, "mined": 0}

    def __init__(self):
        self.events = []

    def event(self, kind, **kw):
        self.events.append(kind)

    def broadcast(self, *a):
        pass


def run(tmp_path, monkeypatch, mined_per_min):
    store = server.Store(tmp_path / "mesh.db")
    mesh = FakeMesh()
    mesh.log_counts = {"lines": 0, "mined": 0}
    a = alerts.Alerts(store, mesh, tmp_path / "mesh.db", None)
    monkeypatch.setattr(alerts, "notify", lambda *x: None)
    now = [time.time() - 3600]
    monkeypatch.setattr(alerts.time, "time", lambda: now[0])
    for minute in range(40):  # 40 minutes: debug lines flowing, packets arriving over LoRa
        mesh.log_counts["lines"] += 30
        mesh.log_counts["mined"] += mined_per_min
        store.insert("packets", ts=now[0], from_id="!c0000001", station=LOCAL, pkt_id=minute,
                     raw=json.dumps({"transportMechanism": "TRANSPORT_LORA"}))
        a._check_mining()
        now[0] += 60
    return [r["title"] for r in store.query("SELECT title FROM alerts")]


def test_alerts_when_nothing_is_recognised(tmp_path, monkeypatch):
    assert run(tmp_path, monkeypatch, mined_per_min=0) == ["Reception logging has stopped"]  # once, not every minute


def test_quiet_when_receptions_are_recognised(tmp_path, monkeypatch):
    assert run(tmp_path, monkeypatch, mined_per_min=2) == []


def test_no_debug_log_is_not_a_failure():
    assert not alerts.mining_stalled(lines=0, mined=0, lora_packets=40)   # e.g. a radio on Wi-Fi
    assert not alerts.mining_stalled(lines=500, mined=0, lora_packets=0)  # nothing on the air to log
