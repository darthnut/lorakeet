"""Importing the Android app's packet log (CSV) and node database (JSON)."""
import json

import import_datalog

HEADER = '"date","time","from","sender name","sender lat","sender long","rx lat","rx long","rx elevation","rx snr",' \
         '"distance(m)","hop limit","hop start","relay node","payload"\n'
OWN = 3758096385  # 0xe0000001, the radio the phone was connected to


def csv_file(tmp_path):
    rows = [
        f'"2026-10-06","07:26:06","{OWN}","My radio","","","","","","0.0","0","3","3","","<TELEMETRY_APP>"',
        f'"2026-10-06","07:30:00","{OWN}","My radio","","","","","","0.0","0","3","3","","hello mesh"',
        '"2026-10-06","07:31:00","3221225473","Node 1","","","","","","-9.5","","2","3","ff","<POSITION_APP>"',
        '"2026-10-06","07:32:00","3221225474","Node 2","","","","","","-3.0","","3","3","","<NODEINFO_APP>"',
    ]
    f = tmp_path / "Meshtastic_datalog_TEST.csv"
    f.write_text(HEADER + "\n".join(rows) + "\n", encoding="utf-8")
    return f


def test_packet_log_converts_to_station_rows(tmp_path):
    station, rows = import_datalog.load(csv_file(tmp_path))
    assert station == "!e0000001"  # worked out from the radio's own local-only rows
    t = import_datalog.convert(station, rows, "x.csv")
    assert len(t["packets"]) == 2                     # the radio's own telemetry is skipped
    relayed, direct = t["packets"]
    assert relayed["hops"] == 1 and relayed["relay"] == 0xff and relayed["pkt_id"] is None
    assert direct["hops"] == 0 and len(t["links"]) == 1  # a 0-hop reception is a real link
    assert [m["text"] for m in t["messages"]] == ["hello mesh"] and t["messages"][0]["outgoing"] == 1
    assert json.loads(relayed["raw"])["import"] == "android-datalog"


def test_the_same_packet_gets_the_same_import_key(tmp_path):
    station, rows = import_datalog.load(csv_file(tmp_path))
    a = import_datalog.convert(station, rows, "a.csv")["packets"]
    b = import_datalog.convert(station, rows, "b.csv")["packets"]
    assert [p["src_rowid"] for p in a] == [p["src_rowid"] for p in b]


def test_node_database_export(tmp_path):
    f = tmp_path / "Meshtastic_nodedb_TEST.json"
    f.write_text(json.dumps({"schemaVersion": 1, "exportedAt": "2026-10-07T15:09:23.066Z", "myNodeNum": OWN, "nodes": [
        {"num": OWN, "id": "!e0000001", "longName": "My radio", "shortName": "me", "hwModel": "HELTEC_V3", "role": "CLIENT"},
        {"num": 3221225473, "id": "!c0000001", "longName": "Node 1", "shortName": "n1", "hwModel": "TBEAM",
         "role": "ROUTER", "lastHeard": 1791380000, "snr": 4.0},
        {"num": 3221225475, "id": "!c0000003", "longName": "Meshtastic 0003", "shortName": "0003", "lastHeard": 1},
    ]}), encoding="utf-8")
    station, t, _ = import_datalog.convert_nodedb(f, f.name)
    assert station == "!e0000001"
    assert [r["node"] for r in t["node_info"]] == ["!c0000001"]  # real identities only, never our own
    snap = json.loads(t["nodedb_snapshots"][0]["data"])
    assert snap["!c0000001"]["user"]["longName"] == "Node 1" and snap["!c0000001"]["snr"] == 4.0
