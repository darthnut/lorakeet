"""The 2026-10-09 security review: each finding as the attack it describes, refused."""
import http.client
import json
from http.server import BaseHTTPRequestHandler
import os
import socket
import threading
import time
import types

import pytest

import login as login_mod
import pairing
import server
import sync as sync_mod

T0 = 1_700_000_000
HUB_RADIO, S1, S2 = "!b0000001", "!a0000001", "!a0000002"


def _row(n, station, **kw):
    return {"src_rowid": n, "station": station, "ts": T0 + n, "from_id": "!c0000001", **kw}


def _batch(station, table, rows):
    return {"protocol": 1, "station": station, "table": table, "rows": rows}


@pytest.fixture
def hub(tmp_path):
    store = server.Store(tmp_path / "mesh.db")
    store.claim_station(HUB_RADIO)
    yield sync_mod.Hub(store), store
    store.db.close()


# ---- 2: whatever arrives must fit its column (stored script in number columns)
@pytest.mark.parametrize("bad", [
    _row(1, S1, hops="<img src=x onerror=alert(1)>"),       # text in a number column
    _row(1, S1, snr=True),                                 # a boolean is not a number
    _row(1, S1, from_id="<script>"),                       # node columns hold node ids
    _row(1, S1, raw="{not json"),                          # JSON columns hold JSON
    _row(1, S1, summary="x" * 10000),                      # bounded text
    {**_row(1, S1), "ts": 0},                              # ts=0 would pose as "first seen" (remote command keys)
])
def test_values_must_fit_their_columns(hub, bad):
    h, _ = hub
    with pytest.raises(sync_mod.IngestError) as e:
        h.ingest(_batch(S1, "packets", [bad]), writer="shared")
    assert e.value.status == 400


def test_station_id_with_a_trailing_newline_is_refused(hub):
    h, _ = hub
    with pytest.raises(sync_mod.IngestError):
        h.ingest(_batch(S1 + "\n", "packets", [_row(1, S1 + "\n")]), writer="shared")


# ---- 4 + 8: who may write which station
def test_nobody_writes_as_the_hubs_own_radio_even_before_it_connects(tmp_path):
    store = server.Store(tmp_path / "mesh.db")
    store.claim_station(HUB_RADIO)
    store.db.close()
    again = server.Store(tmp_path / "mesh.db")                # after a restart, radio not connected yet
    assert again.station is None
    h = sync_mod.Hub(again)
    for writer in ("shared", "pair:p1", f"peer:somehub"):
        with pytest.raises(sync_mod.IngestError) as e:
            h.ingest(_batch(HUB_RADIO, "packets", [_row(1, HUB_RADIO)]), writer=writer)
        assert e.value.status == 409
    again.db.close()


def test_a_peer_only_writes_stations_it_brought(hub):
    h, _ = hub
    h.ingest(_batch(S1, "packets", [_row(1, S1)]), writer="pair:p1")          # S1 is a paired station here
    for st in (S1, S2):
        if st == S2:
            h.ingest(_batch(S2, "packets", [_row(1, S2)]), writer="shared")   # S2 a hand-configured one
        with pytest.raises(sync_mod.IngestError) as e:
            h.ingest(_batch(st, "packets", [_row(5000, st)]), writer="peer:evil")
        assert e.value.status == 409
    h.ingest(_batch("!e0000001", "packets", [_row(1, "!e0000001")]), writer="peer:friend")  # its own station: fine
    with pytest.raises(sync_mod.IngestError):
        h.ingest(_batch("!e0000001", "packets", [_row(2, "!e0000001")]), writer="peer:other")
    with pytest.raises(sync_mod.IngestError):                                   # a station's own log stays home
        h.ingest(_batch("!e0000001", "events", [{"src_rowid": 3, "station": "!e0000001", "ts": T0, "kind": "x"}]),
                 writer="peer:friend")


def test_the_shared_token_cant_overwrite_a_paired_station_but_a_new_pairing_can_take_over_a_hand_set_one(hub):
    h, _ = hub
    h.ingest(_batch(S1, "packets", [_row(1, S1)]), writer="pair:p1")
    with pytest.raises(sync_mod.IngestError):
        h.ingest(_batch(S1, "packets", [_row(2, S1)]), writer="shared")
    with pytest.raises(sync_mod.IngestError):                                   # another live pairing can't either
        h.ingest(_batch(S1, "packets", [_row(2, S1)]), writer="pair:p2")
    h.ingest(_batch(S2, "packets", [_row(1, S2)]), writer="shared")
    h.ingest(_batch(S2, "packets", [_row(2, S2)]), writer="pair:p3")           # pairing a hand-set station: fine
    h.is_revoked = lambda owner: owner == "pair:p1"
    h.ingest(_batch(S1, "packets", [_row(3, S1)]), writer="pair:p4")           # re-paired after a revoke: fine


# ---- 3: a planted reception copy doesn't make a private text public
def test_planted_copies_and_direct_messages_never_prove_public(hub):
    h, store = hub
    PUBLIC = 8
    store.insert("packets", station=S1, ts=T0, from_id="!c0000001", to_id="^all", portnum="TEXT_MESSAGE_APP", pkt_id=1,
                 raw=json.dumps({"decoded": {"text": "family plans"}}))
    store.insert("packets", station=S1, ts=T0, from_id="!c0000001", to_id=S1, portnum="TEXT_MESSAGE_APP", pkt_id=2, pki=1,
                 raw=json.dumps({"decoded": {"text": "just for you"}}))
    for pid in (1, 2):  # a peer plants public-looking copies under ITS station
        store.insert("rx_hops", station="!e0000001", ts=T0, from_id="!c0000001", pkt_id=pid, channel=PUBLIC, directed=0)
    store.insert("rx_hops", station=S1, ts=T0, from_id="!c0000001", pkt_id=2, channel=PUBLIC, directed=0)  # even own: a DM
    rows = [dict(r) for r in store.query("SELECT * FROM packets WHERE station = ?", S1)]
    assert sync_mod.public_keys(store, {PUBLIC}, rows) == set()
    shared = sync_mod.share_rows(store, "packets", rows, "default", {PUBLIC})
    assert all("text" not in json.loads(r["raw"])["decoded"] for r in shared)


# ---- 10: only listed packet types, only when proven public, keep their contents
def test_other_payloads_go_without_contents(hub):
    h, store = hub
    PUBLIC = 8
    def pkt(pid, port, dec):
        store.insert("packets", station=S1, ts=T0, from_id="!c0000001", to_id="^all", portnum=port, pkt_id=pid,
                     summary="47.61, -122.33", raw=json.dumps({"decoded": {"portnum": port, **dec}}))
    pkt(1, "RANGE_TEST_APP", {"text": "seq 12 near my house"})
    pkt(2, "WAYPOINT_APP", {"waypoint": {"name": "Sam's place"}})
    pkt(3, "TELEMETRY_APP", {"telemetry": {"deviceMetrics": {"batteryLevel": 90}}})  # proven public: kept
    pkt(4, "TELEMETRY_APP", {"telemetry": {"deviceMetrics": {"batteryLevel": 50}}})  # no public copy: withheld
    store.insert("rx_hops", station=S1, ts=T0, from_id="!c0000001", pkt_id=3, channel=PUBLIC, directed=0)
    rows = [dict(r) for r in store.query("SELECT * FROM packets WHERE station = ? ORDER BY pkt_id", S1)]
    out = {r["pkt_id"]: json.loads(r["raw"])["decoded"] for r in sync_mod.share_rows(store, "packets", rows, "default", {PUBLIC})}
    assert "text" not in out[1] and "waypoint" not in out[2] and "telemetry" not in out[4]
    assert out[3]["telemetry"]["deviceMetrics"]["batteryLevel"] == 90


# ---- 9: what a peer learns about a station
def test_peers_get_only_name_version_radio_and_a_rounded_location():
    rep = sync_mod.coarse_report({"name": "Garage", "version": "0.3.0", "radioHw": "T1", "location": [47.6062, -122.3321, 30],
                                  "platform": "linux aarch64", "uptimeS": 99, "diskFreeMB": 5, "tempC": 51.2,
                                  "throttled": "0x0", "software": "abc", "note": "behind the shed", "backlog": 7})
    assert rep == {"name": "Garage", "version": "0.3.0", "radioHw": "T1",
                   "location": [sync_mod.coarse(47.6062), sync_mod.coarse(-122.3321)]}


# ---- 5: parallel guesses can't slip past the lockout
def test_one_password_check_at_a_time_per_address_and_attempts_count_first(tmp_path, monkeypatch):
    lg = login_mod.Login(tmp_path / "login.json")
    lg.set_password("correct horse battery")
    gate = threading.Event()
    real = login_mod.Login._check
    monkeypatch.setattr(login_mod.Login, "_check", lambda self, pw: gate.wait(5) and real(self, pw))
    t = threading.Thread(target=lambda: lg.login("192.0.2.5", "wrong guess!!"))
    t.start()
    time.sleep(0.2)
    with pytest.raises(login_mod.LockedOut):                 # a second guess while the first is being checked
        lg.login("192.0.2.5", "another guess!")
    assert lg.fails["192.0.2.5"][0] == 1                     # counted before the check finished
    gate.set()
    t.join()


# ---- 1, 6, 7: the HTTP layer
class _Mesh:
    connected, paused, port, local_id, iface, host = True, False, "COM9", HUB_RADIO, None, None
    pos, station_ids = {}, set()

    def name(self, nid):
        return "Hub"

    def own_fix_now(self):
        return None

    def status(self):
        return {}


@pytest.fixture
def web(monkeypatch):
    srv = server.ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(_Mesh(), None, None, None, None, None))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]

    def req(method, path, host=None, body=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        c.putrequest(method, path, skip_host=True)
        c.putheader("Host", host or f"127.0.0.1:{port}")
        for k, v in {"Content-Type": "application/json", **(headers or {})}.items():
            c.putheader(k, v)
        data = json.dumps(body).encode() if body is not None else b""
        if "Content-Length" not in (headers or {}):
            c.putheader("Content-Length", str(len(data)))
        c.endheaders(data)
        r = c.getresponse()
        return r.status, r.read()

    yield req, port
    srv.shutdown()


def test_dns_rebinding_names_are_refused(web, monkeypatch):
    req, port = web
    assert req("GET", "/api/whoami")[0] == 200
    assert req("GET", "/api/whoami", host=f"localhost:{port}")[0] == 200
    assert req("GET", "/api/whoami", host=f"[::1]:{port}")[0] == 200
    for evil in (f"evil.example:{port}", "evil.example", f"127.0.0.1.evil.example:{port}"):
        assert req("GET", "/api/radio/share?index=1", host=evil)[0] == 421
        assert req("POST", "/api/send", host=evil, body={"text": "hi"}, headers={"Origin": f"http://{evil}"})[0] == 421
    monkeypatch.setitem(server.CFG["http"], "hostnames", ["desk-pc"])
    assert req("GET", "/api/whoami", host=f"desk-pc:{port}")[0] == 200


def test_a_negative_content_length_is_refused(web):
    req, _ = web
    st, body = req("POST", "/api/login", body=None, headers={"Content-Length": "-1"})
    assert st == 400


def test_connections_time_out_and_are_capped():
    h = server.make_handler(_Mesh(), None, None, None, None, None)
    assert h.timeout and h.timeout <= 120
    assert server.MAX_CONNECTIONS <= 256 and sync_mod.MAX_JSON <= 64 * 1024 * 1024


def test_a_refused_station_hears_why_even_mid_upload(web):
    """The hub refuses before reading the batch; it must still read (and drop) it, or closing with unread data resets
    the connection and the station reports "unreachable" instead of the refusal (Windows: WinError 10053)."""
    _, port = web
    body = os.urandom(3 * 1024 * 1024)
    for _ in range(3):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
        c.request("POST", "/api/ingest", body=body, headers={"Content-Type": "application/json", "Content-Encoding": "gzip"})
        r = c.getresponse()
        assert r.status == 404 and b"isn't a hub" in r.read()
        c.close()


def test_one_device_cant_take_every_connection(monkeypatch):
    srv = server.ExclusiveHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
    try:
        monkeypatch.setitem(server.CFG["sync"], "mode", "off")
        lan = "192.168.1.50"
        assert all(srv._admit(lan) for _ in range(server.MAX_PER_ADDRESS)) and not srv._admit(lan)
        assert not srv._admit("8.8.8.8")                                  # could never be served: closed at once
        monkeypatch.setattr(server, "MAX_CONNECTIONS", server.MAX_PER_ADDRESS)
        assert not srv._admit("169.254.1.1")                             # the shared pool is full...
        assert all(srv._admit("127.0.0.1") for _ in range(20))            # ...this computer still gets in
        srv._release(lan)
        assert srv._admit(lan)
    finally:
        srv.server_close()


def test_csv_cells_never_run_as_formulas():
    import packetsearch
    cell = packetsearch.csv_cell
    assert cell("=HYPERLINK(\"http://x\")") == "'=HYPERLINK(\"http://x\")" and cell("@SUM(A1)").startswith("'")
    assert cell("+1+cmd|' /C calc'!A0").startswith("'") and cell("-2+3").startswith("'")
    assert cell("-12.5") == "-12.5" and cell("Fern") == "Fern" and cell(-3) == -3 and cell(None) is None


def test_pages_cant_be_framed_and_view_only_devices_see_no_paths(web, monkeypatch):
    req, port = web
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    c.request("GET", "/api/whoami")
    r = c.getresponse()
    r.read()
    assert r.getheader("X-Frame-Options") == "DENY" and r.getheader("X-Content-Type-Options") == "nosniff"


def test_settings_rewrites_keep_the_old_copy_private(tmp_path, monkeypatch):
    import config
    p = tmp_path / "lorakeet.toml"
    p.write_text('[station]\nname = "A"\n', encoding="utf-8")
    monkeypatch.setenv("LORAKEET_CONFIG", str(p))
    config.update_station("B", [])
    assert 'name = "B"' in p.read_text(encoding="utf-8") and (tmp_path / "lorakeet.toml.bak").exists()
    if os.name != "nt":
        assert (tmp_path / "lorakeet.toml.bak").stat().st_mode & 0o077 == 0


def test_view_only_devices_see_no_paths(monkeypatch):
    class Storage:
        def status(self):
            return {"dbBytes": 1, "dbPath": "/srv/secret-folder/mesh.db", "dests": ["/mnt/secret-folder"], "lastBackup": {"error": "/srv/secret-folder"}}
    monkeypatch.setattr(server, "client_access", lambda ip: "view")
    monkeypatch.setitem(server.CFG["http"], "lan", "view")
    srv = server.ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(_Mesh(), None, Storage(), None, None, None))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
        c.request("GET", "/api/storage")
        body = c.getresponse().read().decode()
        assert '"dbBytes": 1' in body and "secret-folder" not in body
        d = json.loads(body)                     # the page's backup status still works, with labels for paths
        assert d["dests"] == ["backup folder 1"] and d["lastBackup"]["error"] == "failed"
    finally:
        srv.shutdown()
