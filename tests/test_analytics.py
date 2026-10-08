"""Analytics on the synthetic two-station mesh: counts, coverage gaps, the combined view, station comparison."""
import pytest

import analytics
import compare
import synth
import topology


@pytest.fixture(scope="module")
def mesh(tmp_path_factory):
    path = tmp_path_factory.mktemp("mesh") / "mesh.db"
    facts = synth.build(path)
    analytics.HOME["id"] = synth.HOME
    return path, facts


def test_counts_what_each_station_heard(mesh):
    path, f = mesh
    home = analytics.compute(path, "24h", synth.HOME, synth.describe)
    pi = analytics.compute(path, "24h", synth.PI, synth.describe)
    assert home["kpis"]["packets"] == f["home_packets"]
    assert pi["kpis"]["packets"] == f["pi_packets"]


def test_combined_view_counts_each_packet_once(mesh):
    path, f = mesh
    both = analytics.compute(path, "24h", analytics.ALL_STATIONS, synth.describe)
    assert both["kpis"]["packets"] == f["unique"]  # heard by both stations = counted once


def test_hours_the_logger_was_off_are_gaps_not_zeros(mesh):
    path, _ = mesh
    pi = analytics.compute(path, "24h", synth.PI, synth.describe)
    gaps = [b for b in pi["series"] if not b["covered"]]
    assert gaps and all(b["total"] is None for b in gaps)        # off = no data, never a zero
    assert all(b["total"] is not None for b in pi["series"] if b["covered"])
    assert pi["kpis"]["coveredHours"] <= 2                        # it logged during one hour (spanning two)


def test_relay_byte_resolves_through_the_measured_link(mesh):
    path, _ = mesh
    t = topology.compute(path, "24h", synth.HOME, synth.describe)
    ids = {n["id"] for n in t["nodes"]}
    assert synth.RELAY in ids
    assert any({e["a"], e["b"]} == {synth.RELAY, synth.HOME} for e in t["edges"])


def test_station_comparison_only_counts_minutes_both_were_listening(mesh):
    path, f = mesh
    m = compare.matrix(path, "24h", synth.describe)
    st = {s["id"]: s for s in m["stations"]}
    assert set(st) == {synth.HOME, synth.PI}
    # the PI heard every other packet while it listened, so its capture is about half
    assert 0.3 < (st[synth.PI]["medianCapture"] or 0) < 0.7
    assert (st[synth.HOME]["medianCapture"] or 0) > 0.95


def test_distance():
    assert abs(analytics.dist_m(47.6, -122.3, 47.6, -122.3) - 0) < 1e-6
    assert 110_000 < analytics.dist_m(47.0, -122.3, 48.0, -122.3) < 112_000  # one degree of latitude
