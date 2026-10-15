"""MeshCore on the first-run setup page: config.write_setup with protocol "meshcore", and GET /api/setup saying
whether the optional meshcore library is there (looked for, never imported)."""
import http.client
import importlib.util
import json
import threading
import tomllib

import pytest

import config
import web as web_mod


@pytest.fixture
def setup_path(tmp_path, monkeypatch):
    path = tmp_path / "lorakeet.toml"
    monkeypatch.setenv("LORAKEET_CONFIG", str(path))
    return path


def test_setup_writes_a_meshcore_radio_that_loads(setup_path):
    config.write_setup({"protocol": "meshcore", "port": "COM12", "stationName": "Test"})
    text = setup_path.read_text(encoding="utf-8")
    assert tomllib.loads(text)["radio"] == {"protocol": "meshcore", "port": "COM12"}
    assert "MeshCore" in text  # the radio section's note says what it is
    c = config.load()
    assert c["radio"]["protocol"] == "meshcore" and c["radio"]["port"] == "COM12" and c["radio"]["host"] == ""


def test_setup_refuses_meshcore_without_a_port_or_with_a_host(setup_path):
    for answers in ({"protocol": "meshcore"}, {"protocol": "meshcore", "port": " "},
                    {"protocol": "meshcore", "host": "192.168.1.50"},
                    {"protocol": "meshcore", "port": "COM12", "host": "192.168.1.50"}):
        with pytest.raises(ValueError):
            config.write_setup(answers)
        assert not setup_path.exists()
    with pytest.raises(ValueError):
        config.write_setup({"protocol": "reticulum", "port": "COM12"})
    assert not setup_path.exists()


def test_meshtastic_setup_is_unchanged(setup_path):
    config.write_setup({"protocol": "meshtastic", "port": "COM3"})
    assert tomllib.loads(setup_path.read_text(encoding="utf-8"))["radio"] == {"port": "COM3"}  # no protocol line


class _Mesh:
    connected, local_id = False, None

    def status(self):
        return {"connected": False}

    def name(self, nid):
        return nid

    def own_fix_now(self):
        return None


@pytest.mark.parametrize("installed", [True, False])
def test_setup_info_reports_whether_meshcore_is_installed(monkeypatch, installed):
    real = importlib.util.find_spec
    monkeypatch.setattr(importlib.util, "find_spec",
                        lambda name, *a: (object() if installed else None) if name == "meshcore" else real(name, *a))
    monkeypatch.setattr(web_mod, "serial_ports", lambda: [])
    info = web_mod.setup_info(_Mesh())
    assert info["meshcoreAvailable"] is installed
    assert "requirements-meshcore.txt" in info["meshcoreInstall"] and "pip install" in info["meshcoreInstall"]
    assert "pythonw" not in info["meshcoreInstall"].lower()


def test_setup_endpoint_reports_meshcore(monkeypatch):
    monkeypatch.setattr(web_mod, "serial_ports", lambda: [])
    srv = web_mod.ThreadingHTTPServer(("127.0.0.1", 0), web_mod.make_handler(_Mesh(), None, None, None, None, None))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        c = http.client.HTTPConnection(f"127.0.0.1:{srv.server_address[1]}", timeout=10)
        c.request("GET", "/api/setup")
        r = c.getresponse()
        d = json.loads(r.read())
    finally:
        srv.shutdown()
    assert r.status == 200 and d["meshcoreAvailable"] is (importlib.util.find_spec("meshcore") is not None)
