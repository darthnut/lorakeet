"""The version number, the changelog and the database layout version."""
import re
import sqlite3
from pathlib import Path

import server

ROOT = Path(__file__).resolve().parent.parent


def test_version_is_in_the_changelog():
    v = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    assert re.fullmatch(r"\d+\.\d+\.\d+(-dev)?", v)
    log = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    # a development version collects changes under "Unreleased"; a release has its own heading
    assert "## Unreleased" in log if v.endswith("-dev") else f"## {v} (" in log
    assert server.VERSION == v


def test_database_records_its_layout_version(tmp_path):
    path = tmp_path / "mesh.db"
    old = sqlite3.connect(path)                      # a database from before the version was recorded
    old.execute("CREATE TABLE packets (ts REAL, from_id TEXT)")
    old.execute("INSERT INTO packets VALUES (1, '!a0000001')")
    old.commit()
    old.close()
    store = server.Store(path)
    assert store.db.execute("PRAGMA user_version").fetchone()[0] == server.SCHEMA_VERSION
    assert store.db.execute("SELECT COUNT(*) FROM packets").fetchone()[0] == 1   # logged data untouched
    store.db.close()


def test_a_newer_database_is_never_downgraded(tmp_path, caplog):
    path = tmp_path / "mesh.db"
    db = sqlite3.connect(path)
    db.execute(f"PRAGMA user_version = {server.SCHEMA_VERSION + 5}")
    db.commit()
    db.close()
    store = server.Store(path)
    assert store.db.execute("PRAGMA user_version").fetchone()[0] == server.SCHEMA_VERSION + 5
    assert "newer Lorakeet" in caplog.text
    store.db.close()


def test_layout_2_adds_received_via_to_an_existing_database_and_explains_it(tmp_path):
    import sqlite3
    path = tmp_path / "mesh.db"
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE packets (ts REAL, from_id TEXT, station TEXT, src_rowid INTEGER)")
    old.execute("INSERT INTO packets VALUES (1, '!a0000001', '!a0000001', NULL)")
    old.execute("PRAGMA user_version = 1")
    old.commit()
    old.close()
    store = server.Store(path)
    assert "received_via" in {r[1] for r in store.db.execute("PRAGMA table_info(packets)")}
    assert store.db.execute("SELECT COUNT(*) FROM packets").fetchone()[0] == 1          # nothing lost
    notes = {(t, c) for t, c, _ in store.db.execute("SELECT * FROM field_notes")}
    assert ("*", "received_via") in notes and ("*", "src_rowid") in notes
    store.db.close()
