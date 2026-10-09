"""Drive coverage: receptions placed along a moving station's own GPS track."""
import json
import time

import pytest

import analytics
import drive
import server
import synth

CAR = "!e0000001"


@pytest.fixture
def drive_db(tmp_path):
    path = tmp_path / "mesh.db"
    store = server.Store(path)
    t0 = time.time() - 3600
    # a 20-minute drive east, one exact fix every 30 s (~110 m apart)
    for k in range(40):
        store.insert("positions", ts=t0 + 30 * k, node=CAR, lat=47.60, lon=-122.33 + 0.0015 * k, alt=50,
                     precision_bits=None, source="own", fix_time=int(t0 + 30 * k), station=CAR)
    # a rounded (channel-precision) fix far away must never be used
    store.insert("positions", ts=t0 + 5, node=CAR, lat=47.70, lon=-122.40, alt=None, precision_bits=13,
                 source="own", fix_time=int(t0 + 5), station=CAR)
    # receptions only during the first half of the drive; the second half is a coverage hole
    for k in range(0, 600, 20):
        store.insert("rx_hops", ts=t0 + k + 3, from_id="!c0000001", hops=0, relay=None, snr=4.0, rssi=-90,
                     pkt_id=5000 + k, channel=8, directed=0, to_id="^all", length=40, encrypted=1, hop_start=3,
                     hop_limit=3, station=CAR)
    # a parked station: has fixes but never moved, so it isn't a drive
    for k in range(5):
        store.insert("positions", ts=t0 + 600 * k, node="!a0000001", lat=47.5, lon=-122.2, alt=None,
                     precision_bits=None, source="own", fix_time=int(t0 + 600 * k), station="!a0000001")
    store.insert("packets", ts=t0, from_id="!c0000001", station="!a0000001", raw=json.dumps({}))
    store.db.close()
    return path, t0


def test_receptions_follow_the_route_and_holes_show(drive_db):
    path, _ = drive_db
    r = drive.compute(path, "24h", None, synth.describe, 250)
    assert [s["id"] for s in r["stations"]] == [CAR]          # only the station that moved
    assert r["stats"]["receptions"] == 30
    heard = [c for c in r["cells"] if c["receptions"]]
    holes = [c for c in r["cells"] if c["status"] == "hole"]
    assert heard and holes                                     # coverage early, a gap later
    assert all(c["lon"] > -122.30 for c in holes)              # the gap is the silent second half
    assert any(p[3] == 0 for s in r["tracks"][0]["segments"] for p in s)  # and the route is dashed there
    assert all(c["lon"] < -122.30 for c in heard)              # receptions sit on the first part of the route
    assert max(c["lat"] for c in r["cells"]) < 47.65           # the rounded fix far away was ignored


def test_quiet_squares_crossed_at_speed_are_not_holes(tmp_path):
    """Normal traffic (one packet a minute) on a fast drive: most squares see no packet, none is a hole."""
    path = tmp_path / "mesh.db"
    store = server.Store(path)
    t0 = time.time() - 3600
    for k in range(90):  # 15 minutes, a fix every 10 s, ~150 m apart: each 250 m square crossed in ~17 s
        store.insert("positions", ts=t0 + 10 * k, node=CAR, lat=47.60, lon=-122.33 + 0.002 * k, alt=50,
                     precision_bits=None, source="own", fix_time=int(t0 + 10 * k), station=CAR)
    for k in range(15):
        store.insert("rx_hops", ts=t0 + 60 * k + 5, from_id="!c0000001", hops=1, relay=None, snr=1.0, rssi=-100,
                     pkt_id=6000 + k, channel=8, directed=0, to_id="^all", length=40, encrypted=1, hop_start=3,
                     hop_limit=2, station=CAR)
    store.insert("packets", ts=t0, from_id="!c0000001", station=CAR, raw=json.dumps({}))  # the log's start
    store.db.close()
    r = drive.compute(path, "24h", CAR, synth.describe, 250)
    statuses = [c["status"] for c in r["cells"]]
    assert statuses.count("brief") > statuses.count("heard") > 0  # most squares heard nothing...
    assert "hole" not in statuses                                  # ...and none of them is a gap
    assert 0.9 < r["tracks"][0]["ratePerMin"] < 1.2 and 120 < r["tracks"][0]["silentAfterS"] < 160


def test_a_parked_station_shows_when_picked_explicitly(drive_db):
    path, _ = drive_db
    r = drive.compute(path, "24h", "!a0000001", synth.describe, 250)
    assert [s["id"] for s in r["stations"]] == ["!a0000001"]


def test_station_position_uses_only_recent_exact_fixes(drive_db):
    path, t0 = drive_db
    db = analytics._connect(path)
    try:
        p = analytics.station_position(db, CAR, t0 + 61)
        assert p[3] == "track" and abs(p[1] - (-122.33 + 0.0015 * 2)) < 1e-9
        assert analytics.station_position(db, CAR, t0 + 30 * 39 + 20 * 60) is None  # last fix too old
    finally:
        db.close()


def test_cell_size_is_validated(drive_db):
    path, _ = drive_db
    with pytest.raises(ValueError):
        drive.compute(path, "24h", None, synth.describe, 333)


def test_where_the_mesh_heard_our_own_transmissions(drive_db):
    """The other direction: each packet the car sent, placed on its route, with the best evidence it got out."""
    path, t0 = drive_db
    store = server.Store(path)
    home = "!a0000001"  # another of our listening stations (it has packets in the fixture)
    for k, pid in enumerate((900, 901, 902)):
        store.insert("tx_log", ts=t0 + 100 + 200 * k, from_id=CAR, to_id="^all", pkt_id=pid, hop_start=4, hop_limit=4,
                     station=CAR)
    store.insert("rx_hops", ts=t0 + 101, from_id=CAR, pkt_id=900, hops=2, snr=-12.0, station=home)  # reached home
    store.insert("rx_hops", ts=t0 + 302, from_id=CAR, pkt_id=901, hops=1, relay=0x22, snr=3.0, station=CAR)  # echo
    store.db.close()
    r = drive.compute(path, "24h", CAR, synth.describe, 250)
    by = {s["pkt"]: s for s in r["sent"]}
    assert by[900]["result"] == "station" and by[900]["heardBy"][0]["hops"] == 2
    assert by[901]["result"] == "mesh" and by[901]["echoes"] == 1
    assert by[902]["result"] == "none"
    assert abs(by[902]["lon"] - (-122.33 + 0.0015 * 16)) < 1e-9   # sent at t0+500: the fix from t0+480


def test_drive_pings_follow_distance_and_time():
    a = (47.60, -122.33)
    b = (47.60, -122.33 + 0.0135)  # ~1 km east
    assert server.ping_due(a, 0, b, 120, 1000, 60)
    assert not server.ping_due(a, 0, b, 30, 1000, 60)              # too soon
    assert not server.ping_due(a, 0, (47.6001, -122.33), 600, 1000, 60)  # parked: jitter, not distance
