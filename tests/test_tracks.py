"""Moving stations on the map: their GPS track, where they spent the range, and never a rounded position."""
import time

import analytics
import server
import topology

STATION = "!b0000002"
HOME = (47.6062, -122.3321)                    # the documented example location
AWAY = (HOME[0] + 0.02, HOME[1] + 0.02)        # ~2.7 km from it


def build(path):
    store = server.Store(path)
    t = time.time() - 6 * 3600
    store.insert("positions", ts=t - 600, node=STATION, lat=HOME[0], lon=HOME[1], source="own", station=STATION)
    for i in range(30):  # five hours parked at home, a fix every 10 min
        store.insert("positions", ts=t + i * 600, node=STATION, lat=HOME[0] + 1e-5, lon=HOME[1], source="own", station=STATION)
    for i in range(1, 11):  # then a 10-minute drive
        f = i / 10
        store.insert("positions", ts=t + 18000 + i * 60, node=STATION, station=STATION, source="own",
                     lat=HOME[0] + (AWAY[0] - HOME[0]) * f, lon=HOME[1] + (AWAY[1] - HOME[1]) * f)
    # another station heard the car's own broadcast: rounded to a box kilometres wide, and newer than any fix
    store.insert("positions", ts=time.time(), node=STATION, lat=47.5, lon=-122.5, precision_bits=13, station="!a0000001")
    store.db.close()


def test_a_moving_station_is_drawn_where_it_spent_the_range(tmp_path):
    path = tmp_path / "mesh.db"
    build(path)
    db = analytics._connect(path, "*")
    try:
        since, until = time.time() - 7 * 3600, time.time()
        tracks = topology.station_tracks(db, since, until)
        tr = tracks[STATION]["track"]
        assert tr[0][0] < since + 3600 and len(tr) == 41                 # includes the fixes, in order
        assert analytics.dist_m(*HOME, *tracks[STATION]["dwell"]) < 50    # five hours at home beats a short drive
        pos = topology._positions(db, {STATION}, tracks)
        assert analytics.dist_m(*HOME, *pos[STATION]) < 50               # not the newer rounded broadcast
        # a station without a track: its newest EXACT position, never the rounded one
        assert analytics.dist_m(*AWAY, *topology._positions(db, {STATION}, {})[STATION]) < 50
    finally:
        db.close()


def test_parked_gps_jitter_is_one_spot_and_driving_is_not():
    t, fixes = 0, []
    for k in range(20):  # 10 minutes parked, wandering up to ~35 m (multipath in a cab)
        fixes.append((t + 30 * k, HOME[0] + (0.0003 if k % 3 == 0 else -0.0002), HOME[1]))
    for k in range(1, 11):  # then driving away, ~150 m per 10 s
        fixes.append((600 + 10 * k, HOME[0] + 0.00135 * k, HOME[1]))
    sm = analytics.smooth_track(fixes)
    parked = {(f[1], f[2]) for f in sm[:20]}
    assert len(parked) == 1                                   # one spot while parked
    assert [f[1:] for f in sm[20:]] == [f[1:] for f in fixes[20:]]  # the drive is untouched
    assert all(a[0] == b[0] for a, b in zip(sm, fixes))       # times never change


def test_coverage_squares_are_the_same_whatever_else_is_shown():
    import drive
    a = drive._bin([{"station": STATION, "segments": [[[HOME[0], HOME[1], 0, 1], [HOME[0] + 0.001, HOME[1], 60, 1]]]}], [], 250, None)
    far = [[AWAY[0] + 1, AWAY[1], 120, 1], [AWAY[0] + 1.001, AWAY[1], 180, 1]]  # another drive, far north
    b = drive._bin([{"station": STATION, "segments": [[[HOME[0], HOME[1], 0, 1], [HOME[0] + 0.001, HOME[1], 60, 1]], far]}], [], 250, None)
    home_square = lambda cells: next(c["bounds"] for c in cells if c["bounds"][0][0] <= HOME[0] < c["bounds"][1][0])  # noqa: E731
    assert home_square(a) == home_square(b)                   # the old grid moved with the data's mean latitude
