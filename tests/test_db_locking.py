"""A long analytics read must never make the logger's writes fail ("database is locked")."""
import sqlite3
import time

import server


def test_writes_go_through_while_a_reader_holds_the_database(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "DB_WAIT_S", 1)  # in rollback mode the write below would wait this long, then fail
    path = tmp_path / "mesh.db"
    store = server.Store(path)
    assert store.db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    store.insert("packets", ts=1, from_id="!c0000001")
    reader = sqlite3.connect(f"file:{path}?mode=ro", uri=True, isolation_level=None)
    reader.execute("BEGIN")
    reader.execute("SELECT COUNT(*) FROM packets").fetchone()  # a read transaction held open, like a slow query
    t0 = time.time()
    store.insert("packets", ts=2, from_id="!c0000002")          # the logger writing meanwhile
    assert time.time() - t0 < 0.5
    assert reader.execute("SELECT COUNT(*) FROM packets").fetchone()[0] == 1  # the reader keeps its snapshot
    reader.execute("COMMIT")
    assert reader.execute("SELECT COUNT(*) FROM packets").fetchone()[0] == 2
    reader.close()
    store.db.close()
