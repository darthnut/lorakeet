"""A small synthetic mesh, built fresh for each test that needs one: two listening stations, a relay, a few
nodes, minute-by-minute station reports and packets heard by one or both stations. Every node id is made up.
The expected numbers are known exactly, so analytics can be checked against them."""
import json
import time

import server

HOME, PI = "!a0000001", "!b0000002"      # listening stations
RELAY = "!d00000ff"                       # relays everything multi-hop (relay byte 0xff)
NODES = [f"!c00000{i:02x}" for i in range(1, 6)]   # five ordinary nodes
HOURS = 3                                 # the mesh runs for the last 3 hours
EVERY_S = 600                             # each node sends a position every 10 minutes


def _packet(store, station, ts, frm, pkt_id, hops, snr, relay=None):
    raw = {"from": int(frm[1:], 16), "fromId": frm, "toId": "^all", "id": pkt_id, "rxSnr": snr, "rxRssi": -100,
           "hopStart": 3, "hopLimit": 3 - hops, "transportMechanism": "TRANSPORT_LORA",
           "decoded": {"portnum": "POSITION_APP", "position": {"latitude": 47.6062, "longitude": -122.3321}}}
    store.insert("packets", ts=ts, from_id=frm, to_id="^all", portnum="POSITION_APP", channel=0, snr=snr, rssi=-100,
                 hops=hops, via_mqtt=0, summary="47.6062, -122.3321", relay=relay, pkt_id=pkt_id, pki=0,
                 raw=json.dumps(raw), station=station)
    store.insert("rx_hops", ts=ts, from_id=frm, hops=hops, relay=relay, snr=snr, rssi=-100, pkt_id=pkt_id, channel=8,
                 directed=0, to_id="^all", length=40, encrypted=1, hop_start=3, hop_limit=3 - hops, station=station)


def build(path, now=None):
    """Write the synthetic mesh to a new database at path; returns facts the tests compare against."""
    now = now or time.time()
    start = now - HOURS * 3600
    store = server.Store(path)
    # station reports, once a minute: HOME the whole time, PI only during the middle hour
    for m in range(HOURS * 60):
        ts = start + m * 60
        for st in (HOME,) + ((PI,) if 60 <= m < 120 else ()):
            store.insert("telemetry_full", ts=ts, node=st, kind="deviceMetrics", station=st,
                         data=json.dumps({"channelUtilization": 2.0, "airUtilTx": 0.1, "batteryLevel": 100}))
    for nid in [HOME, PI, RELAY] + NODES:
        store.insert("node_info", ts=start, node=nid, long_name=f"Synthetic {nid[-4:]}", short_name=nid[-4:],
                     hw_model="HELTEC_V3", role="CLIENT", public_key=None, is_licensed=0, data="{}", station=HOME)
    facts = {"home_packets": 0, "pi_packets": 0, "both": 0, "unique": 0, "start": start, "now": now}
    pkt = 1000
    for k, nid in enumerate(NODES):
        direct = k < 2  # the first two nodes are heard directly, the rest through RELAY
        for t in range(int(start) + 30 + k * 7, int(now) - 60, EVERY_S):
            pkt += 1
            hops, relay = (0, None) if direct else (1, 0xff)
            _packet(store, HOME, t, nid, pkt, hops, 5.0 - k, relay)
            facts["home_packets"] += 1
            facts["unique"] += 1
            pi_listening = start + 3600 <= t < start + 7200
            if pi_listening and pkt % 2 == 0:  # PI hears every other packet while it's logging
                _packet(store, PI, t + 0.4, nid, pkt, hops, 2.0 - k, relay)
                facts["pi_packets"] += 1
                facts["both"] += 1
    # the relay is heard directly, so relay byte 0xff resolves to it
    store.insert("links", ts=start + 5, a=RELAY, b=HOME, snr=7.0, source="direct", station=HOME)
    store.db.close()
    return facts


def describe(nid):
    return {"name": f"Synthetic {nid[-4:]}", "short": nid[-4:], "hw": None, "role": None, "announced": False,
            "isBase": False, "isLocal": nid == HOME}
