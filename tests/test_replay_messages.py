"""The replay's readable text messages: once each, channels named from the radio that logged them."""
import json

import analytics
import server
import topology
from synth import describe

A, B, SENDER = "!a0000001", "!b0000002", "!c0000009"


def connected(store, station, names):
    chans = [{"role": "PRIMARY", "settings": {}}] + [
        {"index": i, "role": "SECONDARY", "settings": {"name": n}} for i, n in names.items()]
    store.insert("events", ts=1, kind="connected", detail=json.dumps({"channels": chans}), station=station)


def test_messages_once_each_with_the_logging_radios_channel_names(tmp_path):
    path = tmp_path / "mesh.db"
    store = server.Store(path)
    # channel 2 means different channels on the two radios
    connected(store, A, {1: "Community", 2: "Private"})
    connected(store, B, {1: "Private", 2: "Community"})
    import time
    t = time.time() - 600
    store.insert("packets", ts=t - 5, from_id=SENDER, station=A, pkt_id=1, raw="{}")
    # the same broadcast heard by both stations: shown once
    for st in (A, B):
        store.insert("messages", ts=t, from_id=SENDER, to_id="^all", channel=0, text="hello", pkt_id=7, outgoing=0, station=st)
    # a message on B's channel 1 ("Private" there)
    store.insert("messages", ts=t + 1, from_id=SENDER, to_id="^all", channel=1, text="on private", pkt_id=8, outgoing=0, station=B)
    # a direct message, and one we couldn't decrypt (never shown)
    store.insert("messages", ts=t + 2, from_id=SENDER, to_id=A, channel=0, text="just for you", pkt_id=9, outgoing=0, station=A)
    store.insert("messages", ts=t + 3, from_id=SENDER, to_id=A, channel=0, text="", pkt_id=10, outgoing=0, encrypted=1, station=A)
    store.db.close()
    analytics.HOME["id"] = A
    msgs = topology.replay(path, "24h", analytics.ALL_STATIONS, describe)["messages"]
    assert [m["text"] for m in msgs] == ["hello", "on private", "just for you"]
    assert msgs[0]["channelName"] == "LongFast" and not msgs[0]["direct"]
    assert msgs[1]["channelName"] == "Private"          # named from station B's own channel list
    assert msgs[2]["direct"] and msgs[2]["channelName"] is None


def test_messages_tab_numbers_channels_as_this_radio_does_and_skips_phone_log_copies(tmp_path):
    store = server.Store(tmp_path / "mesh.db")
    connected(store, A, {1: "Private", 2: "Community"})   # this radio
    connected(store, B, {1: "Community", 2: "Private"})   # another station: the same channels, other numbers
    store.insert("messages", ts=10, from_id=SENDER, to_id="^all", channel=2, text="on private", pkt_id=1, outgoing=0, station=B)
    store.insert("messages", ts=11, from_id=SENDER, to_id="^all", channel=1, text="on community", pkt_id=2, outgoing=0, station=B)
    # a phone-log import of the same message: no packet id, no channel; it must not show as a LongFast duplicate
    store.insert("messages", ts=10, from_id=SENDER, to_id="^all", channel=None, text="on private", pkt_id=None, outgoing=0, station=B)
    # ...nor its own sends from the same export (no recipient, no channel: they looked like LongFast broadcasts)
    store.insert("messages", ts=12, from_id=B, to_id=None, channel=None, text="lk status", pkt_id=None, outgoing=1, station=B)
    rows = {m["text"]: m for m in server.messages_for_this_radio(store, A)}
    assert len(server.messages_for_this_radio(store, A)) == 2
    assert rows["on private"]["channel"] == 1 and rows["on community"]["channel"] == 2
