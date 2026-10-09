"""The made-up demo mesh (demo.py, `server.py --demo`, the setup page's "Explore a demo")."""
import json
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

import analytics
import demo
import drive
import server

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    path = tmp_path_factory.mktemp("demo") / "mesh.db"
    home = demo.build(path)
    return path, home


def _q(path, sql, *args):
    db = sqlite3.connect(path)
    try:
        return db.execute(sql, args).fetchall()
    finally:
        db.close()


def describe(nid):
    return {"name": nid, "short": None, "hw": None, "role": None, "isBase": False}


def test_every_view_has_something_to_show(built):
    path, home = built
    for table in ("packets", "rx_hops", "telemetry_full", "positions", "node_info", "messages", "links", "traceroutes"):
        assert _q(path, f"SELECT COUNT(*) FROM {table}")[0][0] > 0, table
    names = {r[0] for r in _q(path, "SELECT long_name FROM node_info")}
    assert {"Demo Home", "Hilltop Station", "Demo Car"} <= names
    stations = {r[0] for r in _q(path, "SELECT DISTINCT station FROM packets")}
    assert len(stations) == 3 and home in stations
    assert _q(path, "PRAGMA user_version")[0][0] == server.SCHEMA_VERSION
    # the newest data is minutes old, so "the last 24 hours" is full
    assert time.time() - _q(path, "SELECT MAX(ts) FROM packets")[0][0] < 3600


def test_synced_stations_look_synced_and_home_looks_logged_here(built):
    path, home = built
    assert _q(path, "SELECT COUNT(*) FROM packets WHERE station = ? AND received_via IS NOT NULL", home)[0][0] == 0
    other = _q(path, "SELECT DISTINCT received_via FROM packets WHERE station != ?", home)
    assert other == [("pair:demo",)]
    assert _q(path, "SELECT COUNT(*) FROM packets WHERE station != ? AND src_rowid IS NULL", home)[0][0] == 0


def test_it_is_the_same_mesh_every_time(built, tmp_path):
    _, home = built
    assert demo.build(tmp_path / "again.db") == home


def test_analytics_runs_on_it_and_shows_the_gap(built):
    path, home = built
    out = analytics.compute(path, "7d", home, describe)
    gap = [b for b in out["series"] if not b["covered"]]
    assert gap and all(b["total"] is None for b in gap)  # Demo Home's logging gap is a gap, not zeros
    assert out["kpis"]["packets"] > 1000 and out["kpis"]["messages"] > 50
    combined = analytics.compute(path, "7d", "*", describe)
    assert combined["since"] == out["since"]


def test_the_car_drove_somewhere(built):
    path, _ = built
    car = _q(path, "SELECT node FROM node_info WHERE long_name = 'Demo Car'")[0][0]
    out = drive.compute(path, "7d", car, describe, 250)
    assert out["cells"] and any(c["status"] == "heard" for c in out["cells"])


def test_stale_and_home_of(built, tmp_path):
    path, home = built
    assert not demo.stale(path) and demo.home_of(path) == home
    assert demo.stale(tmp_path / "missing.db") and demo.home_of(tmp_path / "missing.db") is None


def test_nothing_in_it_looks_personal():
    """The release checker on the demo module (the database itself is built at run time, never committed)."""
    r = subprocess.run([sys.executable, str(ROOT / "tools" / "release_check.py"), str(ROOT / "demo.py")],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


def test_the_demo_refuses_everything_that_touches_real_radios_or_settings(monkeypatch):
    import http.client
    import threading

    class _Mesh:
        connected, paused, port, local_id, iface, host = False, False, None, "!a0000001", None, None
        pos, station_ids = {}, set()

        def name(self, nid):
            return "Demo"

        def own_fix_now(self):
            return None

        def status(self):
            return {}

    monkeypatch.setattr(server, "DEMO", "!a0000001")
    for method, path in (("GET", "/api/radio"), ("GET", "/api/radio/share"), ("GET", "/api/hub"), ("GET", "/api/setup"),
                         ("POST", "/api/radio/station"), ("POST", "/api/radio/apply"), ("POST", "/api/hub/mode"),
                         ("POST", "/api/hub/join"), ("POST", "/api/setup")):
        assert server._demo_refuses(method, path), path
    assert not server._demo_refuses("GET", "/api/state") and not server._demo_refuses("POST", "/api/watch")
    written = []
    monkeypatch.setattr(server.cfgmod if hasattr(server, "cfgmod") else __import__("config"), "update_section",
                        lambda *a, **k: written.append(a))
    srv = server.ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(_Mesh(), None, None, None, None, None))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        port = srv.server_address[1]
        for method, path in (("GET", "/api/hub"), ("POST", "/api/hub/mode"), ("POST", "/api/radio/station")):
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            c.request(method, path, body=b'{"mode": "hub", "name": "x"}' if method == "POST" else None,
                      headers={"Content-Type": "application/json"})
            r = c.getresponse()
            assert r.status == 403 and json.loads(r.read())["demo"] is True
        assert written == []
    finally:
        srv.shutdown()
