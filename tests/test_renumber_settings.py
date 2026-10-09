"""Settings that name radios by number keep working after firmware 2.8 renumbers a radio (nodeids.py aliases)."""
import alerts
import nodeids
import remote
import sync

OLD, NEW, OTHER = "!a0000001", "!b0000001", "!c0000001"
KEY = "a2V5LW9mLXRoZS1yYWRpby0wMDAwMDAwMDAwMDAwMDA="  # made-up; 2.8 keeps a radio's key


def test_watched_radios_and_lists_follow_the_new_number():
    assert nodeids.follow([OLD, OTHER, NEW], {OLD: NEW}) == [NEW, OTHER]  # old -> new, no duplicate


def test_a_station_token_answers_to_the_new_number():
    tokens = sync.follow_tokens({OLD: "station-token-0123456789"}, {OLD: NEW})
    assert sync.authorize("Bearer station-token-0123456789", NEW, "shared-token-0123456789", tokens, False) == NEW
    assert sync.authorize("Bearer shared-token-0123456789", NEW, "shared-token-0123456789", tokens, False) is None


class Mesh:
    local_id = "!d0000001"

    def __init__(self):
        self.sent, self.events = [], []

    def event(self, kind, **kw):
        self.events.append(kw)

    def send_text(self, text, to=None, channel=0):
        self.sent.append(to)


def test_an_allowed_radio_stays_allowed_after_renumbering(monkeypatch):
    mesh = Mesh()
    r = remote.Remote(mesh, [OLD], lambda n: KEY, lambda n: None, lambda: "ok", lambda: None, aliases=lambda: {OLD: NEW})
    monkeypatch.setattr(remote.threading, "Thread", lambda target, args, **kw: type("T", (), {"start": lambda s: target(*args)})())
    r.handle({"fromId": NEW, "toId": Mesh.local_id, "pkiEncrypted": True, "publicKey": KEY, "id": 1}, "lk status")
    assert mesh.sent == [NEW]
    r.handle({"fromId": OTHER, "toId": Mesh.local_id, "pkiEncrypted": True, "publicKey": KEY, "id": 2}, "lk status")
    assert mesh.sent == [NEW]  # an unrelated radio still isn't


def test_watched_list_in_alerts_uses_the_new_number(monkeypatch):
    a = alerts.Alerts.__new__(alerts.Alerts)
    a.db_path = "unused"
    monkeypatch.setattr(nodeids, "info", lambda p: {"aliases": {OLD: NEW}, "v28": set(), "keyed": set()})
    assert a._watched({"watched": [OLD]}) == [NEW]


def test_the_base_station_is_followed_to_its_new_number(monkeypatch):
    import server
    monkeypatch.setattr(server, "BASE_ID", OLD)
    monkeypatch.setattr(server, "BASE_NAME", OLD)
    monkeypatch.setattr(server, "BASE_BYTE", 0x01)
    monkeypatch.setattr(nodeids, "info", lambda p: {"aliases": {OLD: "!b00000c3"}, "v28": set(), "keyed": set()})
    server.follow_base()
    assert server.BASE_ID == "!b00000c3" and server.BASE_BYTE == 0xc3 and server.BASE_NAME == "!b00000c3"
