"""Placing radios from drives: direct receptions from enough different places, signal-weighted; strict rules."""
import math
import time

import analytics
import insights
import server

CAR, HOME = "!b0000002", "!a0000001"
TARGET = (47.6062, -122.3321)        # the documented example location: where the hidden radio really is
RADIO, FEW, CLOSE = "!c0000001", "!c0000002", "!c0000003"


def build(path):
    store = server.Store(path)
    store.insert("packets", ts=1, from_id="!c0000009", station=HOME)  # another listening station exists
    t, k = time.time() - 7200, 0
    # the car stops at 6 spots in a ring 2-6 km around the radio, a few minutes at each
    for i, (bearing, km) in enumerate([(0, 2), (60, 4), (120, 3), (180, 6), (240, 2.5), (300, 5)]):
        lat = TARGET[0] + km / 111.32 * math.cos(math.radians(bearing))
        lon = TARGET[1] + km / (111.32 * math.cos(math.radians(TARGET[0]))) * math.sin(math.radians(bearing))
        for j in range(3):
            ts = t + i * 600 + j * 30
            store.insert("positions", ts=ts, node=CAR, lat=lat, lon=lon, source="own", station=CAR)
            snr = 12 - 8 * km  # weaker further away
            store.insert("rx_hops", ts=ts + 5, from_id=RADIO, hops=0, snr=snr, pkt_id=k, station=CAR); k += 1
            if i < 2:   # only heard from 2 spots: not enough
                store.insert("rx_hops", ts=ts + 6, from_id=FEW, hops=0, snr=0.0, pkt_id=k, station=CAR); k += 1
    for j in range(4):  # heard from 3+ "spots" but all within a few hundred metres: not spread enough
        ts = t + 5000 + j * 200
        store.insert("positions", ts=ts, node=CAR, lat=TARGET[0] + 0.0022 * j, lon=TARGET[1], source="own", station=CAR)
        store.insert("rx_hops", ts=ts + 5, from_id=CLOSE, hops=0, snr=1.0, pkt_id=1000 + j, station=CAR)
    store.db.close()


def test_a_radio_heard_from_many_places_is_placed_near_where_it_is(tmp_path):
    path = tmp_path / "mesh.db"
    build(path)
    est = insights.drive_estimates(path)
    assert set(est) == {RADIO}                         # FEW (2 spots) and CLOSE (0.7 km spread) are refused
    e = est[RADIO]
    err_km = analytics.dist_m(*TARGET, e["lat"], e["lon"]) / 1000
    assert e["spots"] == 6 and e["receptions"] == 18
    assert err_km < 2.0                                 # the stops were 2-6 km out; the strong ones pull it in
    assert err_km < e["radiusKm"] + 1.5
