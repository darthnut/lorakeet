"""A sent message the radio gave up on (max retransmit), then heard rebroadcast: "relayed (late)".

Found 2026-11-24 with the Android collector: a slow relay rebroadcast a channel message 10 s after the sending
radio's last retry, so it reported max retransmit although three stations logged the message. Within 90 s of
sending, hearing our own packet rebroadcast now upgrades the status; a confirmed delivery is never changed.
"""
import json
import types

import pytest

import fwlog
import mesh as mesh_mod
import store as store_mod

LOCAL = "!a0000001"


class FakeIface:
    """Just what send_text touches: sendData (remembers onResponse), localNode's lora hop limit and channels."""
    def __init__(self):
        self.localNode = types.SimpleNamespace(localConfig=types.SimpleNamespace(lora=types.SimpleNamespace(hop_limit=3)),
                                               channels=[])
        self.nodes, self.next_id, self.on_response = {}, 4242, None

    def sendData(self, data, destinationId=None, onResponse=None, **kw):
        self.on_response = onResponse
        return types.SimpleNamespace(id=self.next_id)


@pytest.fixture
def m(tmp_path, monkeypatch):
    monkeypatch.setattr(mesh_mod.threading, "Timer", lambda *a, **k: types.SimpleNamespace(start=lambda: None))
    st = store_mod.Store(tmp_path / "mesh.db")
    st.station = LOCAL
    x = mesh_mod.Mesh(st, None)
    x.connected, x.iface, x.local_id = True, FakeIface(), LOCAL
    x.broadcast = lambda *a, **k: None
    yield x
    st.close()


def rebroadcast(pkt_id, hops=1, frm="a0000001"):
    """Our radio's log line for hearing a packet: from us, after `hops` relays."""
    return fwlog.parse_rx(f"Lora RX (id=0x{pkt_id:08x} fr=0x{frm} to=0xffffffff, WantAck=1, HopLim={4 - hops} Ch=0x8 "
                          f"len=33 rxSNR=6.25 rxRSSI=-20 hopStart=4 relay=0x22)")


def status(m):
    return m.store.query("SELECT status FROM messages WHERE outgoing = 1")[0]["status"]


def gave_up(m):
    m.iface.on_response({"decoded": {"routing": {"errorReason": "MAX_RETRANSMIT"}}, "fromId": LOCAL})


def test_a_rebroadcast_heard_after_max_retransmit_is_relayed_late(m):
    m.send_text("testing 0000", channel=0)
    gave_up(m)
    assert status(m) == "failed: max retransmit"
    m._store_header(rebroadcast(4242), None)
    assert status(m) == "relayed (late)"
    ev = m.store.query("SELECT detail FROM events WHERE kind = 'late_relay'")
    assert json.loads(ev[0]["detail"]) == {"pkt_id": 4242, "previous": "failed: max retransmit", "relay": 0x22}


def test_what_doesnt_upgrade(m, monkeypatch):
    m.send_text("one 0000", to="!a0000002")
    m.iface.on_response({"decoded": {"routing": {"errorReason": "NONE"}}, "fromId": "!a0000002"})
    m._store_header(rebroadcast(4242), None)
    assert status(m) == "delivered"                           # a confirmed delivery stays
    m.store.execute("DELETE FROM messages")

    m.iface.next_id = 4343
    m.send_text("two 0000")
    gave_up(m)
    m._store_header(rebroadcast(4343, hops=0), None)          # not a rebroadcast: our own transmission
    m._store_header(rebroadcast(4343, frm="a0000009"), None)  # someone else's packet with the same id
    assert status(m) == "failed: max retransmit"
    real = mesh_mod.time.time
    monkeypatch.setattr(mesh_mod.time, "time", lambda: real() + mesh_mod.LATE_RELAY_S + 1)
    m._store_header(rebroadcast(4343), None)                  # too late to count
    assert status(m) == "failed: max retransmit"
    assert not m.store.query("SELECT 1 FROM events WHERE kind = 'late_relay'")
