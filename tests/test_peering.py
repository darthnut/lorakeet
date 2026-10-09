"""Peering: one hub sends another what its stations logged, filtered by what that peer may see, without duplicates
or echoes. Hub A is a store sending with sync.PeerSender; hub B is a real server handler receiving."""
import json
import threading

import pytest

import pairing
import server
import sync as sync_mod

A_RADIO, B_RADIO = "!a0000001", "!b0000001"
PUBLIC, PRIVATE = 8, 124  # channel fingerprints: LongFast's, and a private channel's
A_HUB = "a-hub-install-id"
T0 = 1_700_000_000  # realistic times: the hub refuses timestamps before 2017


def _store(path, station):
    s = server.Store(path)
    s.station = station
    return s


def _seed_a(a):
    """Hub A's own radio heard: a public text, a private-channel text, a DM, a position; plus its own events."""
    def text(pid, to, channel_hash, words):
        a.insert("packets", ts=T0 + pid, from_id="!c0000001", to_id=to, portnum="TEXT_MESSAGE_APP", pkt_id=pid,
                 summary=words, raw=json.dumps({"decoded": {"portnum": "TEXT_MESSAGE_APP", "text": words, "payload": "eA=="}}))
        a.insert("rx_hops", ts=T0 + pid, from_id="!c0000001", pkt_id=pid, channel=channel_hash, directed=int(to != "^all"))
        a.insert("messages", ts=T0 + pid, from_id="!c0000001", to_id=to, text=words, pkt_id=pid)
    text(1, "^all", PUBLIC, "hello everyone")
    text(2, "^all", PRIVATE, "family plans")
    text(3, A_RADIO, 0, "just for you")
    a.insert("positions", ts=T0 + 4, node="!c0000002", lat=47.6062, lon=-122.3321, precision_bits=32)
    a.insert("positions", ts=T0 + 6, node=A_RADIO, lat=47.6062, lon=-122.3321, source="own")  # the station's own GPS track
    a.insert("packets", ts=T0 + 7, from_id="!c0000002", to_id="^all", portnum="POSITION_APP", pkt_id=7, summary="47.6062, -122.3321",
             raw=json.dumps({"decoded": {"position": {"latitudeI": 476062000, "longitudeI": -1223321000, "precisionBits": 32}}}))
    a.insert("events", ts=T0 + 5, kind="connected", detail="{}")


class _Mesh:
    connected, paused, port, local_id, iface, host = True, False, "COM9", B_RADIO, None, None
    pos, station_ids = {}, set()

    def name(self, nid):
        return "Hub B"

    def own_fix_now(self):
        return None

    def ingested(self, *a):
        pass

    def event(self, kind, **kw):
        pass

    def describe(self, nid):
        return {"name": nid}


@pytest.fixture
def hubs(tmp_path, monkeypatch):
    bdir = tmp_path / "b"
    bdir.mkdir()
    monkeypatch.setattr(server, "DATA_DIR", bdir)
    monkeypatch.setitem(server.CFG["sync"], "mode", "hub")
    monkeypatch.setitem(server.CFG["sync"], "token", "")
    monkeypatch.setitem(server.CFG["sync"], "station_tokens", [])
    monkeypatch.setitem(server.CFG["sync"], "allow", pairing.allow_networks(True, False))
    b = _store(bdir / "mesh.db", B_RADIO)
    srv = server.ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(_Mesh(), b, None, None, sync_mod.Hub(b), None))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}"
    monkeypatch.setattr(pairing, "hub_addresses", lambda port: [{"url": url, "kind": "lan", "ip": "127.0.0.1", "primary": True}])
    a = _store(tmp_path / "a.db", A_RADIO)
    _seed_a(a)
    # B makes a peer code (the page's "Let another hub send here"); A adds it
    import http.client
    c = http.client.HTTPConnection(url.removeprefix("http://"), timeout=10)
    c.request("POST", "/api/hub/peer-code", json.dumps({"label": "A's hub"}), {"Content-Type": "application/json"})
    code = pairing.parse_code(json.loads(c.getresponse().read())["code"])
    assert code["kind"] == "peer"

    def sender(share="default", forward=False, store=a, my_hub=A_HUB, exact=False):
        peer = {"id": "p1", "url": url, "token": code["token"], "share": share, "forward": forward, "hubId": code["hubId"],
                "exact": exact}
        return sync_mod.PeerSender(store, peer, my_hub, lambda: {PUBLIC},
                                   lambda st: {"name": "Station A", "location": [47.6062, -122.3321]} if st == A_RADIO else None)

    def call(method, path, body=None):
        c = http.client.HTTPConnection(url.removeprefix("http://"), timeout=10)
        c.request(method, path, json.dumps(body) if body is not None else None, {"Content-Type": "application/json"})
        r = c.getresponse()
        return r.status, json.loads(r.read())

    b.call = call
    yield a, b, sender, code
    srv.shutdown()
    a.db.close()
    b.db.close()


def _b(b, sql, *args):
    return [dict(r) for r in b.query(sql, *args)]


def test_default_share_withholds_private_text_and_the_station_log(hubs):
    a, b, sender, _ = hubs
    assert sender().sync_once() > 0
    pk = {r["pkt_id"]: r for r in _b(b, "SELECT * FROM packets WHERE station = ?", A_RADIO)}
    assert set(pk) == {1, 2, 3, 7}                                      # every reception arrives
    assert json.loads(pk[1]["raw"])["decoded"]["text"] == "hello everyone"
    for pid in (2, 3):                                                 # private channel and DM: no words
        d = json.loads(pk[pid]["raw"])["decoded"]
        assert "text" not in d and "payload" not in d and d["textWithheld"] and pk[pid]["summary"] == "(text not shared)"
    assert [m["text"] for m in _b(b, "SELECT text FROM messages WHERE station = ?", A_RADIO)] == ["hello everyone"]
    assert len(_b(b, "SELECT * FROM rx_hops WHERE station = ?", A_RADIO)) == 3
    assert _b(b, "SELECT * FROM events WHERE station = ?", A_RADIO) == []     # A's own operational log stays home
    st = json.loads(_b(b, "SELECT value FROM settings WHERE key='sync_hub'")[0]["value"])
    assert st[A_RADIO]["via"] == A_HUB
    assert json.loads(_b(b, "SELECT value FROM settings WHERE key='station_meta'")[0]["value"])[A_RADIO]["name"] == "Station A"


def test_resending_never_duplicates(hubs):
    a, b, sender, _ = hubs
    s = sender()
    s.sync_once()
    a.execute("DELETE FROM settings WHERE key='peer_progress'")       # forget the bookmark: everything goes again
    s.sync_once()
    assert len(_b(b, "SELECT * FROM packets WHERE station = ?", A_RADIO)) == 4
    assert len(_b(b, "SELECT * FROM rx_hops WHERE station = ?", A_RADIO)) == 3


def test_receptions_only_and_everything(hubs):
    a, b, sender, _ = hubs
    sender(share="receptions").sync_once()
    assert _b(b, "SELECT * FROM messages WHERE station = ?", A_RADIO) == []
    assert all("text" not in json.loads(r["raw"])["decoded"] for r in _b(b, "SELECT raw FROM packets WHERE station = ?", A_RADIO))
    rows = [{"portnum": "TEXT_MESSAGE_APP", "from_id": "x", "pkt_id": 9, "raw": json.dumps({"decoded": {"text": "hi"}})}]
    assert sync_mod.share_rows(a, "packets", rows, "everything", {PUBLIC}) == rows


def test_no_echo_and_forwarding_is_optional(hubs):
    a, b, sender, code = hubs
    sender().sync_once()                      # B now holds A's station, recorded as having come via A
    back = sync_mod.PeerSender(b, {"id": "p2", "url": "", "token": "", "share": "default", "forward": True, "hubId": A_HUB},
                               "b-hub", lambda: {PUBLIC}, lambda st: None)
    assert A_RADIO not in back.stations()     # never sent back where it came from, even with forwarding on
    to_c = {"id": "p3", "url": "", "token": "", "share": "default", "hubId": "c-hub"}
    assert A_RADIO not in sync_mod.PeerSender(b, {**to_c, "forward": False}, "b-hub", set, lambda st: None).stations()
    assert A_RADIO in sync_mod.PeerSender(b, {**to_c, "forward": True}, "b-hub", set, lambda st: None).stations()


def test_a_peer_code_is_bound_to_the_first_hub(hubs):
    a, b, sender, code = hubs
    sender().sync_once()
    other = sender(my_hub="someone-else")
    with pytest.raises(RuntimeError, match="403"):
        other.sync_once()


def test_every_location_a_peer_gets_is_rounded_to_a_few_km(hubs):
    a, b, sender, _ = hubs
    sender(share="everything").sync_once()                 # even "everything" rounds locations
    for r in _b(b, "SELECT lat, lon, precision_bits FROM positions WHERE station = ?", A_RADIO):
        assert (r["lat"], r["lon"]) == (sync_mod.coarse(47.6062), sync_mod.coarse(-122.3321)) and r["precision_bits"] <= 14
    pos = _b(b, "SELECT raw, summary FROM packets WHERE station = ? AND pkt_id = 7", A_RADIO)[0]
    p = json.loads(pos["raw"])["decoded"]["position"]
    assert p["latitudeI"] == round(sync_mod.coarse(47.6062) * 1e7) and p["precisionBits"] == 14
    assert "47.6062" not in pos["summary"] and "-122.3321" not in pos["summary"]
    meta = json.loads(_b(b, "SELECT value FROM settings WHERE key='station_meta'")[0]["value"])[A_RADIO]
    assert meta["location"] == [sync_mod.coarse(47.6062), sync_mod.coarse(-122.3321)]
    assert abs(sync_mod.coarse(47.6062) - 47.6062) <= sync_mod.PEER_GRID_DEG / 2      # moved at most half a cell


def test_deleting_what_a_peer_sent_keeps_everything_else(hubs):
    a, b, sender, code = hubs
    sender().sync_once()                                              # A's station arrives at B via the peer
    b.insert("packets", ts=T0 + 50, from_id="!c0000009", to_id="^all", portnum="TEXT_MESSAGE_APP", pkt_id=50)  # B's own log
    other = sync_mod.Hub(b)                                           # and a station B got from somewhere else
    other.ingest({"protocol": 1, "station": "!d0000001", "table": "packets",
                  "rows": [{"src_rowid": 1, "station": "!d0000001", "ts": T0 + 60, "from_id": "!c0000001"}]}, writer="shared")
    pid = next(e["id"] for e in json.loads((server.DATA_DIR / "stations.json").read_text()) if e["kind"] == "peer")
    st, pv = b.call("POST", "/api/hub/peer-forget", {"pairing": pid, "dryRun": True})
    assert st == 200 and [x["id"] for x in pv["stations"]] == [A_RADIO] and pv["rows"] > 0 and not pv["revoked"]
    assert _b(b, "SELECT COUNT(*) AS n FROM packets WHERE station = ?", A_RADIO)[0]["n"] > 0   # a preview deletes nothing
    st, r = b.call("POST", "/api/hub/peer-forget", {"pairing": pid})
    assert st == 200 and r["deleted"] == pv["rows"] and r["deletedStations"] == 1
    for t in ("packets", "rx_hops", "messages", "positions"):
        assert _b(b, f"SELECT COUNT(*) AS n FROM {t} WHERE station = ?", A_RADIO)[0]["n"] == 0
    assert _b(b, "SELECT COUNT(*) AS n FROM packets WHERE pkt_id = 50")[0]["n"] == 1             # B's own: kept
    assert _b(b, "SELECT COUNT(*) AS n FROM packets WHERE station = '!d0000001'")[0]["n"] == 1   # another source: kept
    for key in ("station_meta", "sync_hub", "station_owners"):
        row = _b(b, "SELECT value FROM settings WHERE key = ?", key)
        assert not row or A_RADIO not in json.loads(row[0]["value"])
    with pytest.raises(RuntimeError, match="401"):                   # and it can't just send it again
        sender().sync_once()


def test_each_row_records_how_it_arrived_and_the_sender_cant_say_otherwise(hubs):
    a, b, sender, _ = hubs
    a.execute("UPDATE packets SET received_via = 'pair:forged'")   # whatever the sending hub's own rows say
    sender().sync_once()
    vias = {r["received_via"] for r in _b(b, "SELECT received_via FROM packets WHERE station = ?", A_RADIO)}
    assert vias == {f"peer:{A_HUB}"}
    b.insert("packets", ts=T0 + 70, from_id="!c0000009", to_id="^all", portnum="TEXT_MESSAGE_APP", pkt_id=70)
    assert _b(b, "SELECT received_via FROM packets WHERE pkt_id = 70")[0]["received_via"] is None   # logged here


def test_deleting_a_peer_goes_by_route_even_on_a_station_with_other_sources(hubs):
    a, b, sender, code = hubs
    sender().sync_once()
    # the same station also has a row that came another way, and an older synced row from before the stamp existed
    b.insert("packets", station=A_RADIO, ts=T0 + 80, from_id="!c0000001", pkt_id=80, src_rowid=900, received_via="pair:other")
    b.insert("packets", station=A_RADIO, ts=T0 + 81, from_id="!c0000001", pkt_id=81, src_rowid=901)
    h = sync_mod.Hub(b)
    deleted = h.delete_from(f"peer:{A_HUB}")
    left = {r["pkt_id"] for r in _b(b, "SELECT pkt_id FROM packets WHERE station = ?", A_RADIO)}
    assert left == {80} and deleted[A_RADIO] > 0          # its rows and the older unstamped one go; the other route's stays


def test_exact_locations_only_when_chosen(hubs):
    a, b, sender, _ = hubs
    sender(exact=True).sync_once()
    assert {(r["lat"], r["lon"]) for r in _b(b, "SELECT lat, lon FROM positions WHERE station = ?", A_RADIO)} == {(47.6062, -122.3321)}
    meta = json.loads(_b(b, "SELECT value FROM settings WHERE key='station_meta'")[0]["value"])[A_RADIO]
    assert meta["location"] == [47.6062, -122.3321]


def test_a_rounded_position_doesnt_keep_its_exact_bytes():
    """The packet's own payload (base64 protobuf) holds the exact coordinates: a peer without exact locations
    mustn't get it, while the decoded, rounded fields stay."""
    from meshtastic.protobuf import mesh_pb2
    import base64
    pos = mesh_pb2.Position(latitude_i=455152000, longitude_i=-1226784000)
    raw = {"from": 1, "decoded": {"portnum": "POSITION_APP", "payload": base64.b64encode(pos.SerializeToString()).decode(),
                                  "position": {"latitudeI": 455152000, "longitudeI": -1226784000,
                                               "latitude": 45.5152, "longitude": -122.6784}}}
    row = {"raw": json.dumps(raw), "summary": "45.5152, -122.6784"}
    out = json.loads(sync_mod._coarse_row("packets", row)["raw"])
    assert "payload" not in out["decoded"] and out["decoded"]["position"]["latitude"] == 45.51
    assert "45.5152" not in json.dumps(out) and "455152000" not in json.dumps(out)


def test_each_station_is_judged_by_its_own_channels(tmp_path):
    """A station's private channel can share its one-byte fingerprint with a public preset (LongFast's is 8): that
    station's traffic on 8 proves nothing. A station that never reported its channels proves nothing either."""
    import server
    st = server.Store(tmp_path / "mesh.db")
    st.claim_station("!a0000001")
    st.db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('radio_channels', ?)",
                  (json.dumps({"channels": [{"index": 0, "hash": 8, "encrypted": True, "publicKey": True}]}),))
    st.db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('station_meta', ?)", (json.dumps({
        "!a0000002": {"publicHashes": [8], "privateHashes": []},
        "!a0000003": {"publicHashes": [], "privateHashes": [8]}}),))
    st.db.commit()
    hashes = server.public_channel_hashes(st, {"!a0000001"})
    assert 8 in hashes("!a0000001") and 8 in hashes("!a0000002")
    assert 8 not in hashes("!a0000003") and hashes("!a0000004") == set()
    rows = []
    for i, s in enumerate(("!a0000001", "!a0000002", "!a0000003", "!a0000004")):
        st.insert("rx_hops", station=s, ts=T0 + i, from_id="!c0000001", to_id="^all", pkt_id=100 + i, channel=8, directed=0)
        rows.append({"station": s, "from_id": "!c0000001", "to_id": "^all", "pkt_id": 100 + i})
    assert sync_mod.public_keys(st, hashes, rows) == {("!c0000001", 100), ("!c0000001", 101)}
    assert sync_mod.clean_report({"publicHashes": [8, 300], "privateHashes": [1, 2]}) == {"privateHashes": [1, 2]}


def test_a_station_the_peer_wont_take_stays_pending_and_says_why(hubs):
    a, b, sender, _ = hubs
    sync_mod.Hub(b).claim(A_RADIO, "pair:someone-else")      # B already has A's station from another source
    s = sender()
    s.sync_once()
    assert A_RADIO in s.status["refused"] and "another source" in s.status["refused"][A_RADIO]
    assert not (s._progress().get(A_RADIO) or {}).get("packets")   # nothing marked as sent
    assert s.backlog() > 0


def test_an_earlier_home_radio_is_no_peers_but_can_become_a_station(hubs):
    _, b, _, _ = hubs
    b.insert("packets", station="!a00000ee", ts=T0, from_id="!c0000001", pkt_id=1)   # logged here by an older radio
    h = sync_mod.Hub(b)
    with pytest.raises(sync_mod.IngestError, match="logged that radio itself") as e:
        h.claim("!a00000ee", f"peer:{A_HUB}")
    assert e.value.status == 409 and "own radio" not in str(e.value)    # the peer keeps those rows pending
    h.claim("!a00000ee", "pair:x")   # the radio moved to a Pi: paired as a station, it's welcome


def test_pausing_sending_keeps_the_place_and_catches_up(hubs):
    a, b, sender, _ = hubs
    s = sender()
    s.peer["paused"] = True
    s.tick()
    assert s.status["paused"] and not _b(b, "SELECT 1 FROM packets WHERE station = ?", A_RADIO)
    s.peer["paused"] = False
    s.tick()
    assert not s.status["paused"] and s.status["lastError"] is None
    assert _b(b, "SELECT COUNT(*) AS n FROM packets WHERE station = ?", A_RADIO)[0]["n"] > 0


def test_pausing_receiving_declines_without_losing_anything(hubs):
    a, b, sender, _ = hubs
    _, hub = b.call("GET", "/api/hub")
    pid = next(e["id"] for e in hub["pairings"] if e["kind"] == "peer")
    s = sender()
    s.sync_once()                                   # bound and sending
    a.insert("packets", station=A_RADIO, ts=T0 + 90, from_id="!c0000001", to_id="^all", pkt_id=90)
    assert b.call("POST", "/api/hub/station", {"action": "pause", "pairing": pid})[0] == 200
    with pytest.raises(RuntimeError, match="423"):
        s.sync_once()                               # declined: not acknowledged, so it stays pending
    assert not _b(b, "SELECT 1 FROM packets WHERE pkt_id = 90")
    assert b.call("POST", "/api/hub/station", {"action": "resume", "pairing": pid})[0] == 200
    s.sync_once()
    assert _b(b, "SELECT 1 FROM packets WHERE pkt_id = 90")
