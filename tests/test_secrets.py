"""Nothing secret is ever logged, and the release checker catches personal details."""
import json
import subprocess
import sys

from meshtastic.protobuf import localonly_pb2

import server
import sync
from conftest import ROOT


def test_logged_settings_have_no_keys_pins_or_passwords():
    cfg = localonly_pb2.LocalConfig()
    cfg.security.private_key = b"k" * 32
    cfg.security.admin_key.append(b"a" * 32)
    cfg.bluetooth.fixed_pin = 424242
    cfg.network.wifi_psk = "hunter22"
    cfg.lora.hop_limit = 3
    r = server.redacted_config(cfg)
    assert not r.security.private_key and not r.security.admin_key and not r.bluetooth.fixed_pin and not r.network.wifi_psk
    assert r.lora.hop_limit == 3  # everything else is kept
    mod = localonly_pb2.LocalModuleConfig()
    mod.mqtt.password = "secret"
    mod.mqtt.address = "broker.example.com"
    m = server.redacted_module_config(mod)
    assert not m.mqtt.password and m.mqtt.address == "broker.example.com"


def test_older_snapshots_are_scrubbed(tmp_path):
    store = server.Store(tmp_path / "mesh.db")
    detail = {"config": {"bluetooth": {"fixedPin": 424242, "mode": "FIXED_PIN"}, "network": {"wifiPsk": "x"}},
              "module_config": {"mqtt": {"password": "y", "address": "z"}}}
    store.insert("events", ts=1, kind="connected", detail=json.dumps(detail))
    server.scrub_logged_secrets(store)
    d = json.loads(store.query("SELECT detail FROM events")[0]["detail"])
    assert "fixedPin" not in d["config"]["bluetooth"] and d["config"]["bluetooth"]["mode"] == "FIXED_PIN"
    assert "wifiPsk" not in d["config"]["network"] and "password" not in d["module_config"]["mqtt"]


def test_synced_events_from_older_collectors_are_scrubbed():
    d = json.loads(sync._strip_secrets(json.dumps({"config": {"bluetooth": {"fixedPin": 1}}, "port": "COM7"})))
    assert d == {"config": {"bluetooth": {}}, "port": "COM7"}


def run_checker(*files):
    return subprocess.run([sys.executable, str(ROOT / "tools" / "release_check.py"), *map(str, files)],
                          capture_output=True, text=True)


def test_release_checker_catches_personal_details(tmp_path):
    f = tmp_path / "leak.md"
    # deliberate fake "leaks" for the checker to find (none real); release-ok keeps the checker off these lines
    f.write_text("radio at 10.20.30.40\nnode !1a2b3c4d\nmail someone@gmail.com\nhome -33.86512, 151.20991\n"  # release-ok
                 "call N0XYZ\nC:\\Users\\someone\\data\n", encoding="utf-8")  # release-ok
    r = run_checker(f)
    assert r.returncode == 1
    for what in ("private IP", "node id", "email", "coordinates", "callsign", "user path"):
        assert what in r.stdout, what


def test_release_checker_allows_documented_examples(tmp_path):
    f = tmp_path / "ok.md"
    f.write_text('broadcast !ffffffff, placeholder !1234abcd, host = "192.168.1.50", at [47.60620, -122.33210]\n'
                 "ssh lorakeet@lorakeet-station\nmail 123+someone@users.noreply.github.com\n", encoding="utf-8")
    assert run_checker(f).returncode == 0


def test_the_repository_itself_is_clean():
    r = run_checker()
    assert r.returncode == 0, r.stdout
