"""Malformed input is turned away or tolerated, never a dropped connection: a bad number in a query is a 400; a
collector's event whose detail is JSON but not an object doesn't break the stations timeline; a firmware log line
that won't parse is counted (so the mining alarm can notice a format change) and not mined."""
import http.client
import json
import threading
import time
import types

import mesh as mesh_mod
import runtime
import store as store_mod
import web as web_mod
from test_live_feed import _Mesh


def get(st, path):
    srv = web_mod.ThreadingHTTPServer(("127.0.0.1", 0), web_mod.make_handler(_Mesh(), st, None, None))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        c = http.client.HTTPConnection(f"127.0.0.1:{srv.server_address[1]}", timeout=10)
        c.request("GET", path)
        r = c.getresponse()
        return r.status, json.loads(r.read())
    finally:
        srv.shutdown()


def test_a_bad_number_is_a_400(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, "DATA_DIR", tmp_path)
    st = store_mod.Store(tmp_path / "mesh.db")
    assert get(st, "/api/packets?limit=abc")[0] == 400
    assert get(st, "/api/stations/timeline?hours=lots")[0] == 400
    st.close()


def test_an_event_detail_that_isnt_an_object(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, "DATA_DIR", tmp_path)
    st = store_mod.Store(tmp_path / "mesh.db")
    st.insert("events", ts=time.time() - 60, kind="network", detail="[1]", station="!b0000002")
    st.flush()
    status, body = get(st, "/api/stations/timeline?hours=24")
    assert status == 200 and body["stations"]["!b0000002"][0]["kind"] == "network"
    st.close()


def test_a_log_line_that_wont_parse_is_counted_not_mined(tmp_path, monkeypatch):
    st = store_mod.Store(tmp_path / "mesh.db")
    m = mesh_mod.Mesh(st, None)

    def broken(_):
        raise ValueError("a format we don't know")
    monkeypatch.setattr(mesh_mod, "parse_rx", broken)
    m._on_log_record(types.SimpleNamespace(level=0, source=None, message="Lora RX (id=0x zz)", time=None), None)
    assert m.log_counts["lines"] == 1 and m.log_counts["mined"] == 0
    st.close()
