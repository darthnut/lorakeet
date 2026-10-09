"""Commands over the mesh: who may send one, and what the replies say."""
import time

import config
import pytest
import remote

LOCAL, ME, STRANGER = "!b0000002", "!a0000001", "!c0000003"
KEY = "a2V5LW9mLXRoZS1kZXNrLXJhZGlvLTAwMDAwMDAwMDA="  # made-up


def pkt(**kw):
    p = {"fromId": ME, "toId": LOCAL, "pkiEncrypted": True, "publicKey": KEY, "id": 1}
    p.update(kw)
    return p


def ok(p, pinned=KEY, flag=None, allow=(ME,)):
    return remote.authorize(p, LOCAL, set(allow), pinned, flag)[0]


def test_only_pki_direct_messages_from_allowed_radios_with_their_recorded_key():
    assert ok(pkt())
    assert not ok(pkt(toId="^all"))                        # a channel message, even a private channel
    assert not ok(pkt(pkiEncrypted=False))                 # not proven to be from the sender
    assert not ok(pkt(fromId=STRANGER))                    # not on the allow list
    assert not ok(pkt(publicKey="b3RoZXIta2V5"))           # a different key than the one on record
    assert not ok(pkt(), pinned=None)                      # no key on record yet
    assert not ok(pkt(), flag={"kind": "compromised"})     # a key anyone could hold


def test_parsing():
    assert remote.parse("LK Status") == "status" and remote.parse("lk") == ""
    assert remote.parse("lkstatus") is None and remote.parse("hello lk status") is None


def test_replies():
    now = 1_000_000.0
    s = remote.status_text("Car", 7300, "Pixel", {"lastOk": now - 20, "backlog": {"packets": 3}},
                           {"tempC": 46.2, "throttled": "0x50000"}, {"lat": 1.0, "lon": 2.0, "time": now - 12}, now)
    assert s == "Car: up 2.0 h · Pixel · sync 20 s ago, backlog 3 · 46°C, under-voltage earlier · GPS fix 12 s old"
    assert len(s.encode()) <= 200
    fix = {"lat": 47.606204, "lon": -122.3321, "alt": 30.4, "time": now - 5}  # the documented example location
    assert remote.gps_text(fix, now) == "47.60620,-122.33210 · 30 m · fix 5 s old"
    assert remote.gps_text(None) == "No GPS fix."
    assert remote.power("0x0") == "power OK" and remote.power("0x1") == "UNDER-VOLTAGE now"


class FakeMesh:
    local_id = LOCAL

    def __init__(self):
        self.events, self.sent = [], []

    def event(self, kind, **kw):
        self.events.append(kw)

    def send_text(self, text, to=None, channel=0):
        self.sent.append((to, text))


def test_answers_once_per_packet_and_rate_limits(monkeypatch):
    mesh = FakeMesh()
    r = remote.Remote(mesh, [ME], lambda n: KEY, lambda n: None, lambda: "all good", lambda: None)
    monkeypatch.setattr(remote.threading, "Thread", lambda target, args, **kw: type("T", (), {"start": lambda s: target(*args)})())
    r.handle(pkt(), "lk status")
    r.handle(pkt(), "lk status")              # the same packet again (another copy): ignored
    r.handle(pkt(id=2), "lk gps")             # within 10 s: rate limited
    r.handle(pkt(id=3, fromId=STRANGER), "lk status")
    assert mesh.sent == [(ME, "all good")]
    assert [e["accepted"] for e in mesh.events] == [True, False, False]


def test_config_needs_an_allow_list(monkeypatch):
    c = config._merge(config.DEFAULTS, {"remote": {"enabled": True}})
    with pytest.raises(ValueError):
        config._validate(c)
