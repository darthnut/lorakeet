"""The Radio page's Advanced section (radio_advanced.py): every setting the radio reported, validated, written with
the checklist and names in ONE transaction; secrets write-only, keys locked, managed mode only switched off."""
import json
import types

import pytest
from meshtastic.protobuf import config_pb2

import radio_advanced as ra
import radio_setup as rs
from test_radio_setup import FakeNode

L = config_pb2.Config.LoRaConfig


@pytest.fixture
def node():
    n = FakeNode()
    for s in ("network", "bluetooth", "display", "power"):
        getattr(n.localConfig, s).SetInParent()
    for s in ("mqtt", "telemetry", "tak"):
        getattr(n.moduleConfig, s).SetInParent()
    n.localConfig.network.wifi_psk = "hunter2-secret"
    n.localConfig.bluetooth.fixed_pin = 123456
    n.localConfig.security.private_key = b"\x01" * 32
    n.localConfig.security.public_key = b"\x02" * 32
    n.moduleConfig.mqtt.password = "mqtt-secret"
    n.localConfig.network.ipv4_config.ip = ra._ip_in("192.168.1.50")
    return n


def field(sch, path):
    return next(f for s in sch["sections"] for f in s["fields"] if f["path"] == path)


def test_schema_shows_every_reported_section_and_never_a_secret(node):
    sch = ra.schema(node)
    ids = [s["id"] for s in sch["sections"]]
    assert "config.lora" in ids and "module.mqtt" in ids and "module.telemetry" in ids
    assert "module.serial" not in ids                          # not reported by this radio: not shown
    assert next(s for s in sch["sections"] if s["id"] == "module.tak")["writable"] is False   # the library can't write it
    text = json.dumps(sch)
    for secret in ("hunter2-secret", "mqtt-secret", "123456"):
        assert secret not in text
    assert field(sch, "config.network.wifi_psk") == {**field(sch, "config.network.wifi_psk"), "set": True, "secret": True}
    assert field(sch, "config.security.private_key")["type"] == "locked" and "value" not in field(sch, "config.security.private_key")
    assert field(sch, "config.lora.region")["value"] == "US" and "EU_868" in field(sch, "config.lora.region")["options"]
    assert field(sch, "config.network.ipv4_config.ip")["value"] == "192.168.1.50"
    assert field(sch, "config.lora.tx_enabled")["risk"]         # warnings travel with the field


def test_plan_validates_types_ranges_choices_and_drops_no_ops(node):
    p = ra.plan(node, {"config.lora.hop_limit": 3, "config.lora.tx_power": 20, "config.lora.region": "EU_868",
                       "config.display.flip_screen": True, "config.network.ipv4_config.gateway": "169.254.0.1"})
    assert set(p) == {"config.lora.tx_power", "config.lora.region", "config.display.flip_screen",
                      "config.network.ipv4_config.gateway"}          # hop limit 3 is already 3
    for bad in ({"config.lora.region": "MARS"}, {"config.lora.hop_limit": -1}, {"config.lora.hop_limit": 2.5},
                {"config.display.flip_screen": "yes"}, {"config.lora.frequency_offset": float("nan")},
                {"config.network.ipv4_config.ip": "999.1.1.1"}, {"config.device.tzdef": "x" * 80},
                {"config.nope.x": 1}, {"module.serial.enabled": True}, {"module.tak.role": "Medic"},
                {"config.network.wifi_psk": "short"}, {"config.bluetooth.fixed_pin": 12},
                {"config.security.private_key": "AAAA"}, {"config.security.is_managed": True},
                {"config.lora.ignore_incoming": [1]}):
        with pytest.raises(ra.AdvancedError):
            ra.plan(node, bad)
    assert ra.plan(node, {"config.security.is_managed": False}) == {}    # already off: nothing to do


def test_one_batch_one_transaction_and_secrets_never_echoed(node):
    planned = ra.plan(node, {"config.lora.tx_power": 17, "module.telemetry.device_update_interval": 1800,
                             "config.network.wifi_psk": "a-new-password", "config.display.flip_screen": True})
    sections = rs.apply(types.SimpleNamespace(localNode=node), {"hop_limit": 4}, ("Garage", "GRG"), planned)
    assert node.calls[0] == "begin" and node.calls[-1] == "commit" and node.calls.count("begin") == 1
    assert sorted(c for c in node.calls if c.startswith("write")) == ["write display", "write lora", "write network", "write telemetry"]
    assert "owner Garage/GRG" in node.calls and sections.count("lora") == 1
    assert node.localConfig.lora.tx_power == 17 and node.localConfig.lora.hop_limit == 4
    assert node.localConfig.network.wifi_psk == "a-new-password"
    shown = ra.public_view(planned)
    assert shown["config.network.wifi_psk"] == "(changed)" and "a-new-password" not in json.dumps(shown)
    assert ra.risky(planned) == {"config.lora.tx_power": ra.RISKS["config.lora.tx_power"]}


def test_checklist_items_name_the_same_settings():
    for k in rs.FIELDS:
        sec, fld = rs.field_path(k).split(".")[1:]
        assert fld in getattr(config_pb2.Config, {"lora": "LoRaConfig", "device": "DeviceConfig", "position": "PositionConfig",
                                                   "security": "SecurityConfig"}[sec]).DESCRIPTOR.fields_by_name


def test_the_server_takes_one_confirmed_batch(node, monkeypatch, tmp_path):
    """Through the real handler: risky changes need confirmRisky, a bad value is refused before any backup, and a
    confirmed batch (checklist-style path + module + secret + names) is one backup and one transaction."""
    import http.client
    import threading
    from pathlib import Path
    import web as web_mod
    import web_radio

    backups = []
    monkeypatch.setattr(rs, "backup", lambda *a, **k: backups.append(a) or Path(tmp_path / "bk.yaml"))
    monkeypatch.setattr(web_radio.threading, "Timer", lambda *a, **k: types.SimpleNamespace(start=lambda: None))

    class Mesh:
        connected, paused, port, local_id, host = True, False, "COM9", "!a0000001", None
        pos, station_ids, events = {}, set(), []
        iface = types.SimpleNamespace(localNode=node, metadata=types.SimpleNamespace(firmware_version="2.7.26"),
                                      getMyNodeInfo=lambda: {"user": {"longName": "Desk", "shortName": "DSK"}})

        def name(self, nid):
            return "Desk"

        def own_fix_now(self):
            return None

        def status(self):
            return {}

        def event(self, kind, **d):
            self.events.append((kind, d))

        def _drop(self, i):
            pass

    mesh = Mesh()
    srv = web_mod.ThreadingHTTPServer(("127.0.0.1", 0), web_mod.make_handler(mesh, None, None, None, None, None))
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    def post(body):
        c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
        c.request("POST", "/api/radio/apply", json.dumps(body), {"Content-Type": "application/json"})
        r = c.getresponse()
        return r.status, json.loads(r.read())
    try:
        batch = {"config.lora.hop_limit": 5, "module.telemetry.device_update_interval": 1800,
                 "config.network.wifi_psk": "a-new-password", "config.display.flip_screen": True}
        st, d = post({"advanced": batch})
        assert st == 409 and set(d["needsConfirm"]) == {"config.lora.hop_limit"} and not backups
        st, d = post({"advanced": {"config.lora.hop_limit": 99}, "confirmRisky": True})
        assert st == 400 and "between" in d["error"] and not backups
        st, d = post({"advanced": batch, "names": {"long": "Garage", "short": "GRG"}, "confirmRisky": True})
        assert st == 200 and len(backups) == 1
        assert node.calls.count("begin") == 1 and node.calls.count("commit") == 1
        assert d["expectedAdvanced"]["config.network.wifi_psk"] == "(changed)"
        assert "a-new-password" not in json.dumps(d) and "a-new-password" not in json.dumps(mesh.events)
        assert node.localConfig.lora.hop_limit == 5 and node.moduleConfig.telemetry.device_update_interval == 1800
    finally:
        srv.shutdown()


def test_undo_last_save_puts_back_exactly_what_it_changed(node, monkeypatch, tmp_path):
    """The newest save's settings go back to the backup taken before it (defaults when the backup omits them);
    a replaced password goes back too, never shown; settings the save didn't touch are left alone."""
    import http.client
    import threading
    from pathlib import Path
    import runtime
    import store as store_mod
    import web as web_mod
    import web_radio

    store = store_mod.Store(tmp_path / "mesh.db")
    store.claim_station("!a0000001")
    bkdir = tmp_path / "radio-backups"
    bkdir.mkdir()
    (bkdir / "a0000001-20261009-161117-before-settings.yaml").write_text(
        "config:\n  device:\n    role: CLIENT_MUTE\n  network:\n    wifiPsk: old-password-1\n  lora:\n    hopLimit: 3\n",
        encoding="utf-8")
    store.insert("events", ts=1.7e9, kind="radio_settings", detail=json.dumps({
        "changes": {"hop_limit": 5}, "advanced": {"config.device.buzzer_mode": "DISABLED", "config.network.wifi_psk": "(changed)"},
        "names": False, "backup": "a0000001-20261009-161117-before-settings.yaml"}))
    node.localConfig.device.buzzer_mode = config_pb2.Config.DeviceConfig.BuzzerMode.Value("DISABLED")
    node.localConfig.lora.hop_limit = 5
    node.localConfig.display.flip_screen = True        # changed elsewhere since: undo leaves it alone
    monkeypatch.setattr(runtime, "RADIO_BACKUPS", bkdir)
    labels = []   # what each write's backup is called: before-undo for an undo, before-redo for undoing an undo
    monkeypatch.setattr(rs, "backup", lambda *a, **k: labels.append(a[3]) or Path(bkdir / "new.yaml"))
    monkeypatch.setattr(web_radio.threading, "Timer", lambda *a, **k: types.SimpleNamespace(start=lambda: None))

    class Mesh:
        connected, paused, port, local_id, host = True, False, "COM9", "!a0000001", None
        pos, station_ids, events = {}, set(), []
        iface = types.SimpleNamespace(localNode=node, metadata=types.SimpleNamespace(firmware_version="2.7.26"),
                                      getMyNodeInfo=lambda: {"user": {"longName": "Desk", "shortName": "DSK"}})

        def name(self, nid):
            return "Desk"

        def own_fix_now(self):
            return None

        def status(self):
            return {}

        def event(self, kind, **d):
            self.events.append((kind, d))

        def _drop(self, i):
            pass

    mesh = Mesh()
    srv = web_mod.ThreadingHTTPServer(("127.0.0.1", 0), web_mod.make_handler(mesh, store, None, None, None, None))
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    def call(method, path, body=None):
        c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
        c.request(method, path, json.dumps(body) if body is not None else None, {"Content-Type": "application/json"})
        r = c.getresponse()
        return r.status, r.read().decode()
    try:
        st, text = call("GET", "/api/radio/undo")
        d = json.loads(text)
        assert st == 200 and d["available"] and "old-password-1" not in text and d["redo"] is False
        got = {i["path"]: i.get("to") for i in d["items"]}
        assert got == {"config.device.buzzer_mode": "ALL_ENABLED", "config.network.wifi_psk": None, "config.lora.hop_limit": 3}
        st, text = call("POST", "/api/radio/undo", {"confirmRisky": True})
        assert st == 200 and "old-password-1" not in text and "old-password-1" not in json.dumps(mesh.events)
        assert node.localConfig.device.buzzer_mode == 0 and node.localConfig.lora.hop_limit == 3
        assert node.localConfig.network.wifi_psk == "old-password-1"      # put back, never shown
        assert node.localConfig.display.flip_screen is True                # not part of that save
        assert mesh.events[-1][1]["undoOf"] == "a0000001-20261009-161117-before-settings.yaml"
        assert labels == ["before-undo"]
    finally:
        srv.shutdown()


def test_undoing_an_undo_is_a_redo():
    import web_radio
    assert web_radio._was_undo("a0000001-20261012-223855-before-undo.yaml")
    assert not web_radio._was_undo("a0000001-20261012-223757-before-settings.yaml")
    assert not web_radio._was_undo("a0000001-20261012-223951-before-redo.yaml")   # undoing a redo is an undo again
    assert not web_radio._was_undo(None)
