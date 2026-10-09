"""Password login for other LAN devices: the hash file, rate limits, sessions, and what each kind of device may do."""
import http.client
import json
import threading
import types

import pytest

import login as login_mod
import server

PW = "correct horse battery"


def test_password_is_only_stored_as_a_salted_hash(tmp_path):
    lg = login_mod.Login(tmp_path / "login.json")
    assert not lg.enabled
    with pytest.raises(ValueError):
        lg.set_password("short")
    lg.set_password(PW)
    text = (tmp_path / "login.json").read_text()
    assert PW not in text and "scrypt" in text
    again = login_mod.Login(tmp_path / "login.json")  # survives a restart
    assert again.enabled and again.login("192.0.2.5", PW) and not again.login("192.0.2.5", "nope nope nope")


def test_sessions_last_30_days_from_last_use_and_a_new_password_ends_them(tmp_path):
    lg = login_mod.Login(tmp_path / "login.json")
    lg.set_password(PW)
    t0 = 1_000_000_000
    tok = lg.login("192.0.2.5", PW, now=t0)
    assert tok not in (tmp_path / "login.json").read_text()      # only its hash is kept
    assert lg.session(tok, now=t0 + 29 * 86400)                  # used on day 29...
    assert lg.session(tok, now=t0 + 58 * 86400)                  # ...so it lasts to day 59
    assert not lg.session(tok, now=t0 + 90 * 86400)
    assert not lg.session("made-up", now=t0) and not lg.session(None, now=t0)
    tok2 = lg.login("192.0.2.5", PW, now=t0)
    lg.set_password("another long password")
    assert not lg.session(tok2, now=t0)
    tok3 = lg.login("192.0.2.5", "another long password", now=t0)
    lg.logout(tok3)
    assert not lg.session(tok3, now=t0)


def test_wrong_passwords_lock_that_address_out_for_longer_each_time(tmp_path):
    lg = login_mod.Login(tmp_path / "login.json")
    lg.set_password(PW)
    t = 1_000_000_000
    for _ in range(login_mod.FREE_TRIES):
        assert lg.login("192.0.2.9", "wrong guess!", now=t) is None
    assert lg.wait_s("192.0.2.9", now=t) == 0                  # five free tries
    assert lg.login("192.0.2.9", "wrong guess!", now=t) is None
    assert lg.wait_s("192.0.2.9", now=t) == login_mod.LOCKOUT_S
    with pytest.raises(login_mod.LockedOut):
        lg.login("192.0.2.9", PW, now=t + 1)                   # even the right password waits
    assert lg.wait_s("192.0.2.7", now=t) == 0                  # other devices aren't affected
    lg.login("192.0.2.9", "wrong guess!", now=t + 31)
    assert lg.wait_s("192.0.2.9", now=t + 31) == 2 * login_mod.LOCKOUT_S
    assert lg.login("192.0.2.9", PW, now=t + 200)              # after the wait, it works
    assert lg.wait_s("192.0.2.9", now=t + 200) == 0


def test_many_failures_from_everywhere_slow_everyone(tmp_path):
    lg = login_mod.Login(tmp_path / "login.json")
    lg.set_password(PW)
    t = 1_000_000_000
    for i in range(login_mod.GLOBAL_PER_MIN):
        lg.login(f"198.51.100.{i}", "wrong guess!", now=t)
    assert lg.wait_s("203.0.113.1", now=t + 1) > 0
    assert lg.wait_s("203.0.113.1", now=t + 61) == 0


def test_access_rules():
    a = login_mod.access
    assert a("full", "off", False) == "full"                     # this computer, always
    assert a(None, "view", True) is None                         # beyond the LAN, never
    assert a("view", "off", True) is None                        # lan off: this computer only
    assert a("view", "view", False) == "view" and a("view", "view", True) == "full"
    assert a("view", "login", False) == "login" and a("view", "login", True) == "full"


class _Alerts:
    def update_settings(self, body):
        return {"ok": True}


@pytest.fixture
def lan_server(tmp_path, monkeypatch):
    """A real handler, with every client treated as another device on the LAN."""
    monkeypatch.setattr(server, "client_access", lambda ip: "view")
    lg = login_mod.Login(tmp_path / "login.json")
    lg.set_password(PW)
    srv = server.ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(types.SimpleNamespace(local_id=None, paused=False), None, None, _Alerts(), None, lg))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    host = f"127.0.0.1:{srv.server_address[1]}"

    def req(method, path, body=None, cookie=None, origin=True):
        c = http.client.HTTPConnection(host, timeout=10)
        h = {"Content-Type": "application/json"}
        if origin:
            h["Origin"] = f"http://{host}"
        if cookie:
            h["Cookie"] = cookie
        c.request(method, path, body=json.dumps(body) if body is not None else None, headers=h)
        r = c.getresponse()
        data = r.read()
        return r.status, dict(r.getheaders()), (json.loads(data) if r.getheader("Content-Type") == "application/json" else data)

    yield req
    srv.shutdown()


def test_lan_login_mode_shows_nothing_until_logged_in(lan_server, monkeypatch):
    monkeypatch.setitem(server.CFG["http"], "lan", "login")
    req = lan_server
    st, h, _ = req("GET", "/analytics.html")
    assert st == 302 and h["Location"] == "/login.html"
    assert req("GET", "/api/state")[0] == 401
    assert req("GET", "/login.html")[0] == 200 and req("GET", "/favicon.svg")[0] == 200
    assert req("GET", "/api/whoami")[2]["access"] == "login"
    assert req("POST", "/api/login", {"password": "not it at all"})[0] == 401
    st, h, _ = req("POST", "/api/login", {"password": PW}, origin=False)
    assert st == 403                                             # no Origin from another device: refused
    st, h, _ = req("POST", "/api/login", {"password": PW})
    assert st == 200 and "HttpOnly" in h["Set-Cookie"] and "SameSite=Strict" in h["Set-Cookie"]
    cookie = h["Set-Cookie"].split(";")[0]
    w = req("GET", "/api/whoami", cookie=cookie)[2]
    assert w["access"] == "full" and w["login"]["loggedIn"]
    assert req("GET", "/analytics.html", cookie=cookie)[0] == 200
    assert req("POST", "/api/settings", {}, cookie=cookie)[0] == 200
    assert req("POST", "/api/settings", {}, cookie=cookie, origin=False)[0] == 403
    assert req("POST", "/api/setup", {}, cookie=cookie)[0] == 403  # setup shows paths: this computer only
    assert req("GET", "/api/setup", cookie=cookie)[0] == 403
    req("POST", "/api/logout", {}, cookie=cookie)
    assert req("GET", "/api/whoami", cookie=cookie)[2]["access"] == "login"


def test_lan_view_mode_is_read_only_until_logged_in(lan_server, monkeypatch):
    monkeypatch.setitem(server.CFG["http"], "lan", "view")
    req = lan_server
    w = req("GET", "/api/whoami")[2]
    assert w["access"] == "view" and w["readOnly"] and w["login"]["enabled"]
    assert req("GET", "/analytics.html")[0] == 200
    assert req("POST", "/api/settings", {})[0] == 403
    cookie = req("POST", "/api/login", {"password": PW})[1]["Set-Cookie"].split(";")[0]
    assert req("POST", "/api/settings", {}, cookie=cookie)[0] == 200


def test_lan_off_refuses_other_devices_even_logged_in(lan_server, monkeypatch):
    monkeypatch.setitem(server.CFG["http"], "lan", "off")
    assert lan_server("GET", "/api/whoami")[0] == 403
    assert lan_server("POST", "/api/login", {"password": PW})[0] == 403


def test_radio_page_is_this_computer_only_even_logged_in(lan_server, monkeypatch):
    monkeypatch.setitem(server.CFG["http"], "lan", "view")
    req = lan_server
    cookie = req("POST", "/api/login", {"password": PW})[1]["Set-Cookie"].split(";")[0]
    for path in ("/api/radio", "/api/radio/share?index=1", "/api/radio/logging"):
        assert req("GET", path, cookie=cookie)[0] == 403            # backups and channel keys stay on this computer
    assert req("POST", "/api/radio/apply", {"changes": {}}, cookie=cookie)[0] == 403
    assert req("POST", "/api/radio/channel", {"action": "create", "name": "x"}, cookie=cookie)[0] == 403
    assert req("POST", "/api/radio/station", {"name": "x"}, cookie=cookie)[0] == 403
    assert req("POST", "/api/radio/restart", {}, cookie=cookie)[0] == 403
    assert req("GET", "/api/radio/location-check?lat=1&lon=2", cookie=cookie)[0] == 403
