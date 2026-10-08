"""Compromised / shared public keys: which radios get the warning, and the packet browser's filter."""
import base64
import hashlib
import time
import zlib

import keyflags
import nodeids
import packetsearch
import server
import weak_keys

WEAK = base64.b64encode(b"w" * 32).decode()      # made-up keys
CLONED = base64.b64encode(b"c" * 32).decode()
OWN = base64.b64encode(b"o" * 32).decode()
RENUMBERED = base64.b64encode(b"r" * 32).decode()
NEW_NUMBER = f"!{zlib.crc32(base64.b64decode(RENUMBERED)):08x}"


def test_which_radios_are_flagged(monkeypatch):
    monkeypatch.setattr(weak_keys, "LOW_ENTROPY_SHA256", {hashlib.sha256(b"w" * 32).hexdigest()})
    now = time.time()
    latest = {"!c0000001": WEAK, "!c0000002": CLONED, "!c0000003": CLONED, "!c0000004": OWN,
              "!c0000005": RENUMBERED, NEW_NUMBER: RENUMBERED}
    # the old number went quiet before the new one appeared: renumbered by firmware 2.8, one radio
    aliases = nodeids.classify(latest, {NEW_NUMBER: now - 86400}, {"!c0000005": now - 5 * 86400})["aliases"]
    f = keyflags.classify(latest, aliases)
    assert f["!c0000001"] == {"kind": "compromised", "weak": True, "with": []}
    assert f["!c0000002"]["kind"] == "shared" and f["!c0000002"]["with"] == ["!c0000003"]
    assert "!c0000004" not in f                              # its own key
    assert "!c0000005" not in f and NEW_NUMBER not in f      # the firmware 2.8 renumber is benign
    assert "compromised" in keyflags.text(f["!c0000001"]).lower()


def test_packet_browser_filters_by_sender_key(tmp_path):
    path = tmp_path / "mesh.db"
    store = server.Store(path)
    for i, sender in enumerate(("!c0000002", "!c0000004")):
        store.insert("packets", ts=time.time() - 60, from_id=sender, to_id="^all", pkt_id=i, portnum="TEXT_MESSAGE_APP", raw="{}")
    store.insert("node_info", ts=1, node="!c0000002", public_key=CLONED)
    store.insert("node_info", ts=1, node="!c0000003", public_key=CLONED)
    store.insert("node_info", ts=1, node="!c0000004", public_key=OWN)
    store.db.close()
    flagged = keyflags.compute(path)
    assert set(flagged) == {"!c0000002", "!c0000003"}
    rows = packetsearch.search(path, "packets", {"keyflag": "any", "keyflagNodes": flagged}, station="*")["rows"]
    assert [r["from_id"] for r in rows] == ["!c0000002"]
    assert packetsearch.search(path, "packets", {"keyflag": "compromised", "keyflagNodes": flagged}, station="*")["rows"] == []
