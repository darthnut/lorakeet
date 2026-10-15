"""MeshCore packet anatomy (meshcore_anatomy.py): every field read from the logged bytes, every hearing listed, and
channel messages decrypted with the keys anyone can know (Public's, a hashtag's), proven by the MAC. The test
encrypts with pycryptodome (the meshcore library's own crypto), Lorakeet decrypts with cryptography: two
implementations that must agree."""
import hashlib
import json
import struct
import time
import types

import pytest

import meshcore_anatomy as ma
import meshcore_packet
import store as store_mod
import web
from meshcore_frames import KEY_A, SEATTLE, advert_payload, packet

Crypto = pytest.importorskip("Crypto.Cipher.AES")
from Crypto.Hash import HMAC, SHA256  # noqa: E402

RADIO = {"radio_freq": 910.525, "radio_bw": 62.5, "radio_sf": 7, "radio_cr": 5}
ME = "mc:c0000099d0d0"


def channel_message(secret, text, ts=1791000000):
    """A GRP_TXT payload as a MeshCore radio sends it: hash, MAC, AES-128-ECB of timestamp, flags, text."""
    plain = struct.pack("<I", ts) + bytes([0]) + text.encode()
    plain += bytes(-len(plain) % 16)
    ct = Crypto.new(secret, Crypto.MODE_ECB).encrypt(plain)
    mac = HMAC.new(secret, digestmod=SHA256).update(ct).digest()[:2]
    return bytes([SHA256.new(secret).digest()[0]]) + mac + ct


def _row(frame, portnum, rowid=1):
    pkt = meshcore_packet.parse(frame)
    return {"rowid": rowid, "ts": time.time(), "portnum": portnum, "station": ME,
            "raw": {"meshcore": {k: v for k, v in pkt.items() if k != "payload"}, "frame": frame.hex(), "advert": None}}


def _text(a):
    return next(f["value"] for f in a["crypto"]["plain"]["fields"] if f["name"] == "text")


def test_an_advert_byte_by_byte():
    frame = packet(meshcore_packet.ADVERT, advert_payload(KEY_A), path=("b0",))
    rx = [{"ts": 1.0, "station": ME, "snr": 7.5, "rssi": -80, "route": "flood", "path": "b0", "hops": 1},
          {"ts": 2.0, "station": ME, "snr": -3.0, "rssi": -110, "route": "flood", "path": "a0,b0", "hops": 2}]
    a = ma.build(_row(frame, "MC_ADVERT"), rx, RADIO, ["mc:b00000aa11ee"], lambda i: {"name": "Bench"})
    assert a["checks"] == [{"ok": True, "text": f"packet hash {a['hash']['value']} matches the one logged at reception"}]
    names = {f["name"]: f["value"] for f in a["frame"]["fields"]}
    assert names["header"] == f"0x{frame[0]:02x}" and names["repeater 1"] == "b0 (Bench)"   # the prefix, named
    pf = {f["name"]: f["value"] for f in a["payload"]["fields"]}
    assert pf["latitude"] == f"{SEATTLE[0]:.6f}" and pf["name"] == "Hilltop Rpt"
    assert [h["path"][-1]["who"][0]["name"] for h in a["hearings"]] == ["Bench", "Bench"] and len(a["hearings"]) == 2
    r = a["radio"]
    assert r["freqMHz"] == 910.525 and r["sf"] == 7 and r["bwKHz"] == 62.5 and r["preambleSymbols"] == 16
    assert not r["presetAssumed"] and r["syncWord"] == "0x12" and r["onAirBytes"] == len(frame)


def test_public_and_hashtag_channel_messages_are_read_and_others_arent():
    pub = packet(5, channel_message(ma.PUBLIC_SECRET, "Alice: hello from the Public channel"))
    a = ma.build(_row(pub, "MC_GRP_TXT"), [], RADIO, [], lambda i: {"name": i})
    assert a["crypto"]["channel"] == "Public" and _text(a) == "Alice: hello from the Public channel"
    assert {"ok": True, "text": "MAC verified with the Public key"} in a["checks"]

    tag = hashlib.sha256(b"#portland").digest()[:16]           # the library's set_channel rule for "#..." names
    msg = packet(5, channel_message(tag, "Bob: anyone on #portland?"))
    a = ma.build(_row(msg, "MC_GRP_TXT"), [], RADIO, [], lambda i: {"name": i})
    assert "plain" not in a["crypto"] and a["crypto"]["canTryHashtag"]   # no name given: stays ciphertext
    a = ma.build(_row(msg, "MC_GRP_TXT"), [], RADIO, [], lambda i: {"name": i}, hashtag="#portland")
    assert a["crypto"]["channel"] == "#portland" and _text(a) == "Bob: anyone on #portland?"
    a = ma.build(_row(msg, "MC_GRP_TXT"), [], RADIO, [], lambda i: {"name": i}, hashtag="seattle")
    assert "plain" not in a["crypto"] and any("#seattle isn't" in n for n in a["notes"])

    private = packet(5, channel_message(bytes(range(16)), "Carol: private"))
    a = ma.build(_row(private, "MC_GRP_TXT"), [], RADIO, [], lambda i: {"name": i})
    assert "plain" not in a["crypto"] and "private channel" in a["crypto"]["note"]


def test_the_endpoint_serves_a_meshcore_packet(tmp_path):
    st = store_mod.Store(tmp_path / "mesh.db")
    st.claim_station(ME)
    frame = packet(5, channel_message(ma.PUBLIC_SECRET, "Alice: hi"))
    row = _row(frame, "MC_GRP_TXT")
    st.insert("packets", ts=row["ts"], from_id=None, portnum="MC_GRP_TXT", pkt_id=1, raw=json.dumps(row["raw"]))
    st.insert("mc_rx", ts=row["ts"], snr=6.0, rssi=-90, route="flood", path="", hops=0,
              packet_hash=row["raw"]["meshcore"]["hash"], frame=frame.hex())
    st.insert("events", ts=row["ts"], kind="connected", detail=json.dumps({"self_info": RADIO}))
    st.flush()
    rowid = st.query("SELECT rowid FROM packets")[0]["rowid"]
    a = web.packet_anatomy(st, types.SimpleNamespace(name=lambda i: i), rowid)
    assert a["protocol"] == "meshcore" and _text(a) == "Alice: hi" and a["snr"] == 6.0 and a["radio"]["freqMHz"] == 910.525
    st.close()
