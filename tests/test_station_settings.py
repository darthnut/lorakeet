"""This station's name and location: editing lorakeet.toml in place, the "far from the radios it hears" check, and
the Radio page's endpoints for them (this computer only)."""
import http.client
import json
import threading

import pytest

import config
import radio_setup
import server
import web as web_mod

SEATTLE = (47.6062, -122.3321)  # the documented example location


def test_update_station_keeps_everything_else(tmp_path, monkeypatch):
    p = tmp_path / "lorakeet.toml"
    p.write_text('# my notes\n[http]\nport = 5199  # keep me\n\n[station]\nname = "Old"\nlocation = [1.0, 2.0]\n'
                 'note = "roof"\n\n[map]\ntiles = "esri"\n', encoding="utf-8")
    monkeypatch.setenv("LORAKEET_CONFIG", str(p))
    config.update_station("Home", [SEATTLE[0], SEATTLE[1]])
    text = p.read_text(encoding="utf-8")
    assert "# my notes" in text and "port = 5199  # keep me" in text and 'note = "roof"' in text and 'tiles = "esri"' in text
    c = config.load()
    assert c["station"]["name"] == "Home" and c["station"]["location"] == [47.6062, -122.3321]
    assert (tmp_path / "lorakeet.toml.bak").read_text(encoding="utf-8").count('name = "Old"') == 1
    config.update_station("Home", [])                      # clearing the location removes the line
    assert config.load()["station"]["location"] == [] and "location" not in p.read_text(encoding="utf-8")


def test_update_station_adds_the_section_and_refuses_what_validation_refuses(tmp_path, monkeypatch):
    p = tmp_path / "lorakeet.toml"
    p.write_text("[http]\nport = 5199\n", encoding="utf-8")
    monkeypatch.setenv("LORAKEET_CONFIG", str(p))
    config.update_station("Car", [])
    assert config.load()["station"]["name"] == "Car"
    p.write_text("[station]\nmobile = true\n", encoding="utf-8")
    with pytest.raises(ValueError):                          # a moving station takes its position from GPS
        config.update_station("Car", [SEATTLE[0], SEATTLE[1]])
    assert "location" not in p.read_text(encoding="utf-8")  # and the file is left as it was


def test_a_location_far_from_the_radios_heard_is_flagged():
    near = [(SEATTLE[0] + d, SEATTLE[1] - d) for d in (0.01, 0.05, 0.1, 0.2)]
    assert radio_setup.location_check(SEATTLE, near) is None
    europe = (SEATTLE[0] + 4, SEATTLE[1] + 120)                # the kind of place an internet-connection guess lands
    c = radio_setup.location_check(europe, near)
    assert c and c["distanceKm"] > 5000 and c["radios"] == 4
    assert radio_setup.location_check(europe, near[:2]) is None  # too few radios to judge
    assert radio_setup.location_check(europe, near + [(SEATTLE[0] + 4, SEATTLE[1] + 120)] * 1) is not None  # one outlier doesn't move the median


class _Mesh:
    connected, local_id, port, host, iface = False, None, None, None, None
    pos = {f"!a000000{i}": {"lat": SEATTLE[0] + i / 100, "lon": SEATTLE[1]} for i in range(1, 5)}

    def own_fix_now(self):
        return None

    def name(self, nid):
        return nid

    def event(self, kind, **detail):
        self.events = getattr(self, "events", []) + [(kind, detail)]


@pytest.fixture
def local_server(tmp_path, monkeypatch):
    p = tmp_path / "lorakeet.toml"
    p.write_text("[http]\nport = 5199\n", encoding="utf-8")
    monkeypatch.setenv("LORAKEET_CONFIG", str(p))
    monkeypatch.delenv("LORAKEET_SUPERVISED", raising=False)
    monkeypatch.delenv("INVOCATION_ID", raising=False)
    srv = web_mod.ThreadingHTTPServer(("127.0.0.1", 0), web_mod.make_handler(_Mesh(), None, None, None, None, None))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    host = f"127.0.0.1:{srv.server_address[1]}"

    def req(method, path, body=None):
        c = http.client.HTTPConnection(host, timeout=10)
        c.request(method, path, body=json.dumps(body) if body is not None else None,
                  headers={"Content-Type": "application/json", "Origin": f"http://{host}"})
        r = c.getresponse()
        return r.status, json.loads(r.read())

    yield req, p
    srv.shutdown()


def test_station_endpoints(local_server):
    req, p = local_server
    far = (SEATTLE[0] + 4, SEATTLE[1] + 120)
    st, d = req("GET", f"/api/radio/location-check?lat={far[0]}&lon={far[1]}")
    assert st == 200 and d["check"]["distanceKm"] > 5000
    assert req("GET", f"/api/radio/location-check?lat={SEATTLE[0]}&lon={SEATTLE[1]}")[1]["check"] is None
    st, d = req("POST", "/api/radio/station", {"name": "Home", "location": [SEATTLE[0], SEATTLE[1]]})
    assert st == 200 and d["station"]["pendingRestart"] and d["station"]["location"] == [47.6062, -122.3321]
    assert 'name = "Home"' in p.read_text(encoding="utf-8")
    assert req("POST", "/api/radio/station", {"name": "x", "location": [95, 0]})[0] == 400
    st, d = req("POST", "/api/radio/restart", {})
    assert st == 409 and "background runner" in d["error"]   # not supervised: says how instead of exiting


def test_update_sections_writes_all_or_nothing(tmp_path, monkeypatch):
    p = tmp_path / "lorakeet.toml"
    p.write_text('# mine\n[http]\nport = 5199  # keep me\n\n[map]\nzoom = 9\n', encoding="utf-8")
    monkeypatch.setenv("LORAKEET_CONFIG", str(p))
    config.update_sections({"map": {"center": [SEATTLE[0], SEATTLE[1]], "zoom": 12}, "backup": {"time": "04:15"},
                            "http": {"hostnames": ["desk-pc"]}})
    c = config.file_settings()
    assert c["map"]["center"] == [47.6062, -122.3321] and c["map"]["zoom"] == 12 and c["backup"]["time"] == "04:15"
    assert c["http"]["hostnames"] == ["desk-pc"] and "# mine" in p.read_text(encoding="utf-8")
    before = p.read_text(encoding="utf-8")
    with pytest.raises(ValueError):  # one bad value refuses the whole batch
        config.update_sections({"map": {"zoom": 3}, "backup": {"time": "25:00"}})
    assert p.read_text(encoding="utf-8") == before
    config.update_sections({"map": {"center": None}})  # None removes the key: back to the default
    assert config.file_settings()["map"]["center"] == [] and "center" not in p.read_text(encoding="utf-8")


def test_preferences_endpoints(local_server, monkeypatch):
    req, p = local_server
    monkeypatch.setitem(server.CFG, "_path", str(p))
    st, d = req("GET", "/api/preferences")
    assert st == 200 and d["saved"]["http"]["port"] == 5199 and "sync" not in d["saved"]
    assert "token" not in json.dumps(d)  # never the sync token
    st, d = req("POST", "/api/preferences", {"changes": {"map": {"zoom": 13, "tiles": "esri"}, "base": {"id": "!a0000001"}}})
    assert st == 200 and d["saved"]["map"] == {"center": [], "zoom": 13, "tiles": "esri"} and d["saved"]["base"]["id"] == "!a0000001"
    assert d["running"]["map"]["zoom"] != 13 or d["running"]["map"]["tiles"] != "esri"  # running = until a restart
    st, d = req("POST", "/api/preferences", {"changes": {"map": {"tiles": "carto"}}})
    assert st == 400 and "map.tiles" in d["error"] and "lorakeet.toml:" not in d["error"]
    for bad in ({"sync": {"token": "x" * 20}}, {"storage": {"data_dir": "/tmp"}}, {"map": {"nope": 1}}, {"map": 5}):
        assert req("POST", "/api/preferences", {"changes": bad})[0] == 400  # only what the page may change
    assert 'tiles = "esri"' in p.read_text(encoding="utf-8") and "token" not in p.read_text(encoding="utf-8")
    monkeypatch.setitem(server.CFG, "_path", None)
    assert req("POST", "/api/preferences", {"changes": {"map": {"zoom": 4}}})[0] == 409  # not set up yet: the setup page
