"""Firmware 2.8 renumbering: a radio's old and new numbers are one radio in every analysis."""
import base64
import time
import zlib

import analytics
import nodeids
import server
import topology

KEY = base64.b64encode(b"k" * 32).decode()   # made-up key
OLD = "!b0000002"                              # the station's number under 2.7
NEW = f"!{zlib.crc32(b'k' * 32):08x}"          # what 2.8 renumbers it to
SENDER = "!c0000007"


def build(path, old_heard_after_upgrade=False):
    store = server.Store(path)
    t = time.time() - 3 * 3600
    store.insert("node_info", ts=t, node=OLD, public_key=KEY, long_name="Pi station", station=OLD)
    store.insert("node_info", ts=t + 3600, node=NEW, public_key=KEY, long_name="Pi station", station=NEW)
    # before the upgrade: logged by the station under its old number, relayed by a byte ending its old number
    for i in range(3):
        store.insert("packets", ts=t + i, from_id=SENDER, to_id="^all", pkt_id=i, hops=1, relay=0x02, station=OLD)
    # after: the same station, new number
    for i in range(3, 5):
        store.insert("packets", ts=t + 3600 + i, from_id=SENDER, to_id="^all", pkt_id=i, hops=0, station=NEW)
    store.insert("packets", ts=t + 3600, from_id=NEW, to_id="^all", pkt_id=99, station=NEW)
    if old_heard_after_upgrade:  # two radios on the air at once with one key: a clone, not a renumber
        store.insert("packets", ts=t + 3 * 3600, from_id=OLD, to_id="^all", pkt_id=100, station=NEW)
    store.db.close()


def test_old_and_new_numbers_are_one_station(tmp_path):
    path = tmp_path / "mesh.db"
    build(path)
    info = nodeids.info(path)
    assert info["aliases"] == {OLD: NEW} and NEW in info["v28"] and OLD not in info["v28"]
    assert [s["id"] for s in analytics.stations(path)] == [NEW]          # the station picker shows one station
    for asked in (NEW, OLD):                                             # either number scopes to all its rows
        db = analytics._connect(path, asked)
        assert db.execute("SELECT COUNT(*) FROM packets WHERE from_id = ?", (SENDER,)).fetchone()[0] == 5
        db.close()
    db = analytics._connect(path, NEW)
    # a relay byte logged before the upgrade ends in the OLD number, and still means this radio
    assert topology.relay_resolver(db, SENDER)(0x02)[0] == NEW
    db.close()
    raw = analytics._connect(path)  # the stored rows themselves are untouched
    assert raw.execute("SELECT COUNT(*) FROM main.packets WHERE station = ?", (OLD,)).fetchone()[0] == 3
    raw.close()


def test_both_numbers_on_the_air_at_once_is_not_a_renumber(tmp_path):
    path = tmp_path / "mesh.db"
    build(path, old_heard_after_upgrade=True)
    assert nodeids.info(path)["aliases"] == {}
    assert {s["id"] for s in analytics.stations(path)} == {OLD, NEW}
