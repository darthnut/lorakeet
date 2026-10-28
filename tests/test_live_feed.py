"""The map's live feed (/api/packets): one entry per packet (its first copy), newest first, with every station that
heard it, and a station hearing another station's own radio left out. It picks the newest entries first and only
then looks up arrival and "heard by" for them (it took 5.7 s once five stations were logging)."""
import http.client
import json
import threading
import time

import runtime
import store as store_mod
import web as web_mod

HOME, PI = "!a0000001", "!b0000002"
SENDERS = ["!c0000001", "!c0000002", "!c0000003"]


class _Mesh:
    connected, paused, port, local_id, iface, host = True, False, "COM9", HOME, None, None
    pos = {}
    station_ids = set()

    def name(self, nid):
        return nid

    def describe(self, nid):
        return {"name": nid}


def pk(st, ts, frm, pkt_id, station, transport="TRANSPORT_LORA"):
    st.insert("packets", ts=ts, from_id=frm, to_id="^all", pkt_id=pkt_id, portnum="POSITION_APP", station=station,
              raw=json.dumps({"fromId": frm, "id": pkt_id, "transportMechanism": transport, "rxSnr": 5.0}))


def test_feed(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, "DATA_DIR", tmp_path)
    st = store_mod.Store(tmp_path / "mesh.db")
    now = time.time()
    for i in range(30):                                   # 30 packets, each heard by HOME, every third also by PI
        t = now - 3600 + i * 60
        pk(st, t, SENDERS[i % 3], 1000 + i, HOME)
        if i % 3 == 0:
            pk(st, t + 2, SENDERS[i % 3], 1000 + i, PI)
    pk(st, now - 30, PI, 5000, HOME)                      # HOME hearing PI's own radio: station-to-station, left out
    pk(st, now - 4 * 86400, SENDERS[0], 9000, HOME)       # older than the feed's 3 days
    st.flush()
    srv = web_mod.ThreadingHTTPServer(("127.0.0.1", 0), web_mod.make_handler(_Mesh(), st, None, None))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        c = http.client.HTTPConnection(f"127.0.0.1:{srv.server_address[1]}", timeout=10)
        c.request("GET", "/api/packets?limit=10")
        rows = json.loads(c.getresponse().read())
    finally:
        srv.shutdown()
        st.close()
    assert [r["pkt_id"] for r in rows] == [1029 - i for i in range(10)]      # newest first, one per packet
    by = {r["pkt_id"]: r for r in rows}
    assert sorted(by[1029]["stations"].split(",")) == [HOME]
    assert sorted(by[1027]["stations"].split(",")) == [HOME, PI]             # heard by both, listed once
    assert by[1027]["station"] == HOME                                       # the first copy
    assert all(r["arrival"] == "lora" for r in rows) and 5000 not in by


def test_packets_naming_no_sender_reach_the_feed(tmp_path, monkeypatch):
    # most MeshCore packets name no sender (from_id NULL): each is its own entry, not dropped or merged
    monkeypatch.setattr(runtime, "DATA_DIR", tmp_path)
    st = store_mod.Store(tmp_path / "mesh.db")
    now = time.time()
    for i, frm in enumerate((None, None, SENDERS[0])):
        pk(st, now - 60 + i, frm, 7000 + i, HOME)
    st.flush()
    srv = web_mod.ThreadingHTTPServer(("127.0.0.1", 0), web_mod.make_handler(_Mesh(), st, None, None))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        c = http.client.HTTPConnection(f"127.0.0.1:{srv.server_address[1]}", timeout=10)
        c.request("GET", "/api/packets?limit=10")
        rows = json.loads(c.getresponse().read())
    finally:
        srv.shutdown()
        st.close()
    assert [r["pkt_id"] for r in rows] == [7002, 7001, 7000]
    assert all(r["stations"] == HOME for r in rows)
