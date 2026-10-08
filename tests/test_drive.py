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
    holes = [c for c in r["cells"] if not c["receptions"] and c["minutes"] >= 0.5]
    assert heard and holes                                     # coverage early, a gap later
    assert all(c["lon"] < -122.30 for c in heard)              # receptions sit on the first part of the route
    assert max(c["lat"] for c in r["cells"]) < 47.65           # the rounded fix far away was ignored


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
