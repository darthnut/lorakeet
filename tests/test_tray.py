"""The tray icon's status text and image, and pausing logging (the radio is let go until resumed)."""
import http.client
import json
import threading
import types

import pytest

import server
import tray


def test_status_text():
    assert tray.status_text(None) == "Lorakeet is starting…"
    assert "paused" in tray.status_text({"paused": True, "connected": False})
    assert tray.status_text({"connected": True, "name": "Desk", "port": "COM7"}) == "Logging from Desk on COM7"
    assert "Waiting for a radio" in tray.status_text({"connected": False, "paused": False})


def test_paused_icon_is_grey():
    pytest.importorskip("PIL")
    on, off = tray.icon_image(False), tray.icon_image(True)
    assert on.size == off.size == (64, 64)
    r, g, b, _ = off.getpixel((32, 32))
    assert r == g == b                                   # grey
    assert on.getpixel((32, 32))[:3] != off.getpixel((32, 32))[:3]


class _Mesh:
    connected, paused, port, local_id, iface = True, False, "COM9", "!a0000001", object()

    def __init__(self):
        self.events, self.dropped = [], []

    def _drop(self, iface):
        self.dropped.append(iface)
        self.connected, self.iface = False, None

    def event(self, kind, **_):
        self.events.append(kind)

    def broadcast(self, *_):
        pass

    def status(self):
        return {}

    def name(self, nid):
        return "Desk"

    set_paused = server.Mesh.set_paused


def test_pausing_lets_go_of_the_radio_and_is_logged():
    m = _Mesh()
    m.set_paused(True)
    assert m.paused and m.dropped and m.events == ["logging_paused"]
    m.set_paused(True)                                   # already paused: nothing more happens
    assert m.events == ["logging_paused"]
    m.set_paused(False)
    assert not m.paused and m.events == ["logging_paused", "logging_resumed"]


def test_pause_endpoint_and_brief(monkeypatch):
    m = _Mesh()
    srv = server.ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(m, None, None, None, None, None))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    host = f"127.0.0.1:{srv.server_address[1]}"

    def req(method, path, body=None):
        c = http.client.HTTPConnection(host, timeout=10)
        c.request(method, path, body=json.dumps(body) if body is not None else None,
                  headers={"Content-Type": "application/json"})
        r = c.getresponse()
        return r.status, json.loads(r.read())

    try:
        assert req("GET", "/api/brief")[1] == {"version": server.VERSION, "connected": True, "paused": False,
                                              "port": "COM9", "id": "!a0000001", "name": "Desk"}
        assert req("POST", "/api/logging", {"paused": True}) == (200, {"paused": True})
        assert req("GET", "/api/brief")[1]["paused"] and req("GET", "/api/whoami")[1]["paused"]
        assert req("POST", "/api/logging", {"paused": False}) == (200, {"paused": False})
    finally:
        srv.shutdown()


def test_restart_is_in_the_tray_menu_when_the_supervisor_offers_it():
    pytest.importorskip("pystray")
    labels = lambda t: [i.text if isinstance(i.text, str) else i.text(i) for i in t.menu().items]  # noqa: E731
    assert "Restart Lorakeet" in labels(tray.Tray(5190, on_quit=lambda: None, on_restart=lambda: None))
    assert "Restart Lorakeet" not in labels(tray.Tray(5190, on_quit=lambda: None))


def test_restart_endpoint_explains_when_it_cant(monkeypatch):
    monkeypatch.delenv("LORAKEET_SUPERVISED", raising=False)
    monkeypatch.delenv("INVOCATION_ID", raising=False)
    m = _Mesh()
    srv = server.ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(m, None, None, None, None, None))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        c = http.client.HTTPConnection(f"127.0.0.1:{srv.server_address[1]}", timeout=10)
        c.request("POST", "/api/restart", "{}", {"Content-Type": "application/json"})
        r = c.getresponse()
        assert r.status == 409 and "background runner" in json.loads(r.read())["error"]
    finally:
        srv.shutdown()


def test_pausing_while_it_connects_lets_go_of_the_radio():
    import threading as th

    class FakeIface:
        closed = False

        def __init__(self, *a, **k):
            pass

        def connect(self):
            m.paused = True  # the tray's Pause arrives while the radio is still connecting

        def waitForConfig(self):  # noqa: N802
            pass

        def close(self):
            FakeIface.closed = True

    class M:
        _connect, _close_quietly = server.Mesh._connect, staticmethod(server.Mesh._close_quietly)
        paused, iface, lock, last_connect_error = False, None, th.RLock(), None

        def _pick_port(self):
            return "COM99"

        def _on_log_record(self, *a):
            pass

    m = M()
    m._connect(FakeIface)
    for _ in range(50):  # closed on a side thread (close joins the reader thread: never under the lock)
        if FakeIface.closed:
            break
        __import__("time").sleep(0.02)
    assert FakeIface.closed and m.iface is None
