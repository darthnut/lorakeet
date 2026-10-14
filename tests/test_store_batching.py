"""Store's batched commits: rows go to disk together every COMMIT_EVERY_S, not one transaction each."""
import sqlite3

import runtime
import store as store_mod


def _others_see(path):
    """What a separate connection (analytics, backups) can see."""
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    finally:
        db.close()


def test_rows_wait_for_the_committer_but_this_connection_sees_them(tmp_path, monkeypatch):
    monkeypatch.setattr(store_mod, "COMMIT_MAX_ROWS", 500)
    monkeypatch.setattr(store_mod, "COMMIT_EVERY_S", 3600)   # the committer thread won't run during the test
    path = tmp_path / "mesh.db"
    s = store_mod.Store(path)
    rowid = s.insert("events", ts=1.0, kind="test")
    assert rowid and s.query("SELECT COUNT(*) AS n FROM events")[0]["n"] == 1   # same connection: visible now
    assert _others_see(path) == 0                                                  # not committed yet
    s.flush()
    assert _others_see(path) == 1


def test_a_full_batch_commits_at_once(tmp_path, monkeypatch):
    monkeypatch.setattr(store_mod, "COMMIT_MAX_ROWS", 3)
    monkeypatch.setattr(store_mod, "COMMIT_EVERY_S", 3600)
    path = tmp_path / "mesh.db"
    s = store_mod.Store(path)
    for i in range(3):
        s.execute("INSERT INTO events (ts, kind) VALUES (?, 'x')", float(i))
    assert _others_see(path) == 3


def test_exit_now_flushes_before_exiting(tmp_path, monkeypatch):
    monkeypatch.setattr(store_mod, "COMMIT_MAX_ROWS", 500)
    monkeypatch.setattr(store_mod, "COMMIT_EVERY_S", 3600)
    path = tmp_path / "mesh.db"
    s = store_mod.Store(path)
    s.insert("events", ts=1.0, kind="test")
    exits = []
    monkeypatch.setattr(runtime.os, "_exit", exits.append)
    monkeypatch.setattr(runtime, "EXIT_HOOKS", [s.flush])
    runtime.exit_now(0)
    assert exits == [0] and _others_see(path) == 1


def test_flush_after_close_is_harmless(tmp_path):
    s = store_mod.Store(tmp_path / "mesh.db")
    s.db.close()
    s.flush()   # what atexit does for a store a test already closed


def test_a_new_database_counts_rows_from_a_time_based_floor(tmp_path, monkeypatch):
    """A replacement database for the same station must never reuse row numbers the hub already has: the hub
    ignores (station, row number) pairs it has seen, so reused numbers would be dropped silently."""
    monkeypatch.setattr(store_mod.time, "time", lambda: 1_700_000_000.0)
    old = store_mod.Store(tmp_path / "old.db")
    first_old = old.insert("packets", ts=1.0, from_id="!a0000001", station="!a0000002")
    second_old = old.insert("packets", ts=2.0, from_id="!a0000001", station="!a0000002")
    assert first_old == 1_700_000_000_000 and second_old == first_old + 1
    old.close()
    monkeypatch.setattr(store_mod.time, "time", lambda: 1_700_000_060.0)   # a minute later: a new SD card
    new = store_mod.Store(tmp_path / "new.db")
    assert new.insert("packets", ts=3.0, from_id="!a0000001", station="!a0000002") == 1_700_000_060_000
    assert new.insert("telemetry", ts=3.0, node="!a0000001", station="!a0000002") == 1_700_000_060_000
    new.close()
    again = store_mod.Store(tmp_path / "old.db")   # reopening keeps counting where it was
    assert again.insert("packets", ts=4.0, from_id="!a0000001", station="!a0000002") == second_old + 1
    again.close()


def test_a_database_from_before_floors_keeps_its_numbering(tmp_path):
    path = tmp_path / "mesh.db"
    s = store_mod.Store(path)
    s.db.execute("DELETE FROM settings WHERE key = ?", (store_mod.ROWID_FLOOR_KEY,))   # as an older Lorakeet made it
    s.db.commit()
    s.close()
    s = store_mod.Store(path)
    assert s.rowid_floor is None and s.insert("packets", ts=1.0, from_id="!a0000001") == 1
    s.close()


def test_debug_lines_reach_the_viewer_at_once_and_the_disk_in_batches(tmp_path, monkeypatch):
    import time as _time
    monkeypatch.setattr(store_mod.DebugLog, "BATCH_S", 0.2)
    monkeypatch.setattr(store_mod.DebugLog, "COMMIT_EVERY_S", 3600)   # only flush() commits during the test
    path = tmp_path / "debug.db"
    dl = store_mod.DebugLog(path)
    seen = []
    dl.on_batch = seen.extend
    dl.add("INFO", "Router", "one")
    dl.add("WARN", "Router", "two")
    for _ in range(50):                      # the viewer gets them within a batch, without waiting for a commit
        if len(seen) == 2:
            break
        _time.sleep(0.05)
    assert [r["message"] for r in seen] == ["one", "two"]
    other = sqlite3.connect(path)
    assert other.execute("SELECT COUNT(*) FROM debug_log").fetchone()[0] == 0   # not on disk yet
    other.close()
    rows = dl.query(10)                      # a page asking for lines makes the writer commit first
    assert sorted(r["message"] for r in rows) == ["one", "two"]


def test_a_busy_port_is_waited_out_on_linux_but_fails_fast_on_windows(monkeypatch):
    import errno
    import web
    tries = []

    def server(addr, handler):
        tries.append(addr)
        if len(tries) < 3:
            raise OSError(errno.EADDRINUSE, "Address already in use")
        return "server"

    monkeypatch.setattr(web, "ExclusiveHTTPServer", server)
    monkeypatch.setattr(web.time, "sleep", lambda s: None)
    monkeypatch.setattr(web.os, "name", "posix")
    assert web.bind_server(("127.0.0.1", 5190), None) == "server" and len(tries) == 3
    tries.clear()
    monkeypatch.setattr(web.os, "name", "nt")
    try:
        web.bind_server(("127.0.0.1", 5190), None)
        raise AssertionError("expected the busy port to fail at once on Windows")
    except OSError as e:
        assert e.errno == errno.EADDRINUSE and len(tries) == 1
