"""Pairing stations with a hub: codes, the hub's registry (tokens stored only as hashes, claimed by the first station
that uses them, revocable), the connection test, and the Stations page's endpoints, against a real hub handler."""
import gzip
import http.client
import json
import threading

import pytest

import pairing
import server
import sync as sync_mod


def test_codes_round_trip_and_damage_is_explained():
    code = pairing.make_code(["http://192.0.2.10:5190", "http://198.51.100.9:5190"], "t" * 32, "Home")
    c = pairing.parse_code(code[:30] + "\n" + code[30:])          # pasted with a line break
    assert c == {"hubs": ["http://192.0.2.10:5190", "http://198.51.100.9:5190"], "token": "t" * 32, "name": "Home", "kind": "station", "hubId": ""}
    for bad in ("", "hello", "lk1-", code[:-12]):
        with pytest.raises(ValueError):
            pairing.parse_code(bad)


def test_registry_keeps_only_hashes_and_binds_to_the_first_station(tmp_path):
    reg = pairing.Pairings(tmp_path / "stations.json")
    e, token = reg.create("Garage Pi")
    assert token not in (tmp_path / "stations.json").read_text()
    assert reg.check("wrong-token-xxxxxxxxxxxx", "!a0000001") is None
    assert reg.check(token, "!a0000001", bind=False) == "ok" and reg.list()[0]["station"] is None  # a hello doesn't claim
    assert reg.check(token, "!a0000001") == "ok" and reg.list()[0]["station"] == "!a0000001"
    assert reg.check(token, "!a0000002") == "other-station"           # a copied code is no use to another radio
    assert pairing.Pairings(tmp_path / "stations.json").check(token, "!a0000001") == "ok"  # survives a restart
    reg.revoke(e["id"])
    assert reg.check(token, "!a0000001") == "revoked"
    reg.forget(station="!a0000001")
    assert reg.list() == []


def test_allow_flags():
    assert pairing.allow_flags(pairing.allow_networks(True, False)) == {"lan": True, "tailscale": False}
    assert pairing.allow_flags(pairing.allow_networks(False, True)) == {"lan": False, "tailscale": True}


class _Mesh:
    connected, paused, port, local_id, iface, host = True, False, "COM9", "!a0000009", None, None
    pos = {}
    station_ids = set()

    def name(self, nid):
        return "Hub radio"

    def own_fix_now(self):
        return None

    def ingested(self, *a):
        pass


@pytest.fixture
def hub(tmp_path, monkeypatch):
    cfgfile = tmp_path / "lorakeet.toml"
    cfgfile.write_text('[sync]\nmode = "hub"\n', encoding="utf-8")
    monkeypatch.setenv("LORAKEET_CONFIG", str(cfgfile))
    monkeypatch.setattr(server, "DATA_DIR", tmp_path)
    monkeypatch.setitem(server.CFG["sync"], "mode", "hub")
    monkeypatch.setitem(server.CFG["sync"], "token", "")
    monkeypatch.setitem(server.CFG["sync"], "station_tokens", [])
    monkeypatch.setitem(server.CFG["sync"], "allow", pairing.allow_networks(True, False))
    store = server.Store(tmp_path / "mesh.db")
    store.station = _Mesh.local_id
    mesh = _Mesh()
    srv = server.ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(
        mesh, store, None, None, sync_mod.Hub(store, on_rows=mesh.ingested), None))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    host = f"127.0.0.1:{srv.server_address[1]}"
    monkeypatch.setattr(pairing, "hub_addresses", lambda port: [{"url": f"http://{host}", "kind": "lan", "ip": "127.0.0.1"}])

    def req(method, path, body=None, headers=None):
        c = http.client.HTTPConnection(host, timeout=10)
        c.request(method, path, body=body if isinstance(body, bytes) else json.dumps(body) if body is not None else None,
                  headers={"Content-Type": "application/json", **(headers or {})})
        r = c.getresponse()
        return r.status, json.loads(r.read())

    yield req, host, cfgfile
    srv.shutdown()
    store.db.close()


def _report(req, token, station):
    body = gzip.compress(json.dumps({"protocol": 1, "station": station, "meta": {"name": "Garage"}}).encode())
    return req("POST", "/api/ingest", body, {"Content-Encoding": "gzip", "Authorization": f"Bearer {token}",
                                             "X-Lorakeet-Station": station})


def test_pair_test_send_claim_and_revoke(hub):
    req, host, _ = hub
    st, d = req("POST", "/api/hub/pair", {"label": "Garage Pi"})
    assert st == 200 and d["code"].startswith("lk1-")
    c = pairing.parse_code(d["code"])
    result, _ = pairing.test_hubs(c["hubs"], c["token"], "!a0000001")
    assert result["ok"] and result["hubName"] == "Hub radio"
    assert _report(req, c["token"], "!a0000001") == (200, {"ok": True})        # claims the pairing
    assert _report(req, c["token"], "!a0000002")[0] == 403                      # another radio can't reuse it
    info = req("GET", "/api/hub")[1]
    row = next(s for s in info["stations"] if s["id"] == "!a0000001")
    assert row["pairing"]["label"] == "Garage Pi" and row["name"] == "Garage"
    req("POST", "/api/hub/station", {"action": "rename", "station": "!a0000001", "name": "Workshop"})
    assert next(s for s in req("GET", "/api/hub")[1]["stations"] if s["id"] == "!a0000001")["name"] == "Workshop"
    req("POST", "/api/hub/station", {"action": "revoke", "pairing": row["pairing"]["id"]})
    assert _report(req, c["token"], "!a0000001")[0] == 401
    bad = pairing.hello(f"http://{host}", c["token"], "!a0000001")
    assert not bad["ok"] and "revoked" in bad["hint"]


def test_unreachable_hub_is_explained():
    r = pairing.hello("http://127.0.0.1:9", "t" * 32, timeout=2)               # nothing listens on port 9
    assert not r["ok"] and "Can't reach the hub" in r["hint"]


def test_hub_mode_and_join_write_the_settings(hub, tmp_path):
    req, host, cfgfile = hub
    st, d = req("POST", "/api/hub/mode", {"mode": "hub", "lan": True, "tailscale": False})
    assert st == 200 and d["allow"] == {"lan": True, "tailscale": False}
    assert req("POST", "/api/hub/mode", {"mode": "hub", "lan": False, "tailscale": False})[0] == 400
    code = req("POST", "/api/hub/pair", {"label": "Me"})[1]["code"]
    st, d = req("POST", "/api/hub/join", {"code": code})                        # this test hub joins itself
    assert st == 200 and d["saved"] and 'mode = "collector"' in cfgfile.read_text(encoding="utf-8")
    assert req("POST", "/api/hub/join", {"code": pairing.make_code(["http://127.0.0.1:9"], "x" * 32)})[0] == 409


def test_a_peer_cant_take_another_peers_hub_id(tmp_path):
    reg = pairing.Pairings(tmp_path / "stations.json")
    a, _ = reg.create("Sam", kind="peer")
    b, _ = reg.create("Alex", kind="peer")
    assert reg.bind(a["id"], hub="hub-sam")
    assert not reg.bind(b["id"], hub="hub-sam")          # Alex claiming to be Sam's hub
    assert reg.bind(b["id"], hub="hub-alex")
    reg.revoke(a["id"])
    c, _ = reg.create("Sam again", kind="peer")
    assert reg.bind(c["id"], hub="hub-sam")              # revoked (a leaked code): Sam can pair again with a new one


def test_the_station_template_is_a_valid_config_before_pairing():
    """setup-station.sh copies it; a collector without a token is refused, so the service would never start and
    `server.py --join` (which loads the config first) couldn't run either."""
    import tomllib
    from pathlib import Path
    import config
    text = (Path(__file__).resolve().parent.parent / "deploy" / "station.example.toml").read_text(encoding="utf-8")
    config._validate(config._merge(config.DEFAULTS, tomllib.loads(text)))


def test_join_reads_the_code_from_stdin(tmp_path):
    import subprocess
    import sys
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    toml = tmp_path / "lorakeet.toml"
    toml.write_text(f'[storage]\ndata_dir = "{(tmp_path / "data").as_posix()}"\n', encoding="utf-8")
    r = subprocess.run([sys.executable, str(root / "server.py"), "--join", "-"], input="lk1-not-a-real-code\n",
                       capture_output=True, text=True, cwd=root, env={**__import__("os").environ, "LORAKEET_CONFIG": str(toml)},
                       timeout=60)
    assert r.returncode != 0 and "Not joined" in (r.stdout + r.stderr)
