"""Collector → hub sync: batches, duplicates, tokens and station reports."""
import pytest

import server
import sync
from synth import PI


@pytest.fixture
def hub(tmp_path):
    store = server.Store(tmp_path / "hub.db")
    store.station = "!a0000001"
    return sync.Hub(store)


def batch(rows, station=PI):
    return {"protocol": sync.PROTOCOL, "station": station, "table": "events", "rows": rows}


def row(n, station=PI):
    return {"src_rowid": n, "station": station, "ts": 1000 + n, "kind": "test"}


def test_a_resent_batch_is_stored_once(hub):
    r1 = hub.ingest(batch([row(1), row(2)]))
    r2 = hub.ingest(batch([row(1), row(2), row(3)]))
    assert r1["stored"] == 2 and r2["stored"] == 1 and r2["duplicates"] == 2
    assert hub.store.query("SELECT COUNT(*) AS n FROM events WHERE station = ?", PI)[0]["n"] == 3


def test_rows_must_belong_to_the_batch_station(hub):
    with pytest.raises(sync.IngestError):
        hub.ingest(batch([row(1, station="!c0000001")]))


def test_a_token_only_sends_its_own_station(hub):
    with pytest.raises(sync.IngestError) as e:
        hub.ingest(batch([row(1)]), bound_station="!c0000001")
    assert e.value.status == 403


def test_the_hub_refuses_rows_claiming_to_be_its_own_radio(hub):
    with pytest.raises(sync.IngestError) as e:
        hub.ingest(batch([row(1, station="!a0000001")], station="!a0000001"))
    assert e.value.status == 409


def test_per_station_tokens():
    tokens = sync.station_tokens([f"{PI}:station-two-token-123456"])
    shared = "shared-token-0123456789"
    ok = lambda header, st, require=False: sync.authorize(f"Bearer {header}", st, shared, tokens, require)  # noqa: E731
    assert ok("station-two-token-123456", PI) == PI
    assert ok(shared, PI) is None                      # a station with its own token can't use the shared one
    assert ok(shared, "!c0000001") == "!c0000001"      # others still can
    assert ok(shared, "!c0000001", require=True) is None


def test_station_reports_are_filtered_and_typed():
    r = sync.clean_report({"name": "x" * 500, "location": [47.6, -122.3], "mobile": True, "tempC": True,
                           "evil": "<script>", "backlog": 3})
    assert len(r["name"]) == 120 and r["location"] == [47.6, -122.3] and r["mobile"] is True and r["backlog"] == 3
    assert "evil" not in r and "tempC" not in r  # unknown fields and wrong types are dropped
    assert "location" not in sync.clean_report({"location": [95, 0]})
