"""The weekly rhythm always covers the rolling past 7 days, whatever range the page asks for."""
import time

import analytics
import synth


def test_weekly_rhythm_is_the_past_week_whatever_the_range(tmp_path):
    path = tmp_path / "mesh.db"
    now = time.time()
    synth.build(path, now=now - 10 * 86400)  # a mesh logged ten days ago (outside the week)...
    synth.build(path, now=now - 3 * 86400)   # ...three days ago (inside it)...
    synth.build(path, now=now)               # ...and now
    analytics.HOME["id"] = synth.HOME
    day = analytics.compute(path, "24h", synth.HOME, synth.describe)
    week = analytics.compute(path, "7d", synth.HOME, synth.describe)
    every = analytics.compute(path, "all", synth.HOME, synth.describe)
    assert day["kpis"]["packets"] < week["kpis"]["packets"] < every["kpis"]["packets"]  # the range scopes the rest
    assert day["heat"] == week["heat"] == every["heat"]
    assert day["heatCoverage"] == every["heatCoverage"]
    in_week = sum(c["packets"] for c in day["heat"])
    assert in_week == week["kpis"]["packets"]                       # three days ago counts, ten days ago doesn't
    assert max(c["hours"] for c in day["heatCoverage"]) == 1          # one week: each slot is one hour, never two
