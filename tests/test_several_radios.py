"""Several radios, one Lorakeet (MESHCORE.md step 6): each radio its own station in the one database, synced to the
hub on its own pairing, with its own bookmark; a hub never takes rows for any of its own radios."""
import json

import pytest

import config
import mesh as mesh_mod
import store as store_mod
import sync

MAIN, EXTRA = "!a0000001", "mc:a0000017c2be"
TOKENS = {"t" * 20: MAIN, "u" * 20: EXTRA}


def test_an_extra_radios_rows_carry_its_own_station(tmp_path):
    st = store_mod.Store(tmp_path / "mesh.db")
    st.claim_station(MAIN)
    extra = store_mod.StationStore(st)
    extra.claim_station(EXTRA)
    st.insert("events", ts=1.0, kind="main")
    extra.insert("events", ts=2.0, kind="extra")
    assert {r["kind"]: r["station"] for r in st.query("SELECT kind, station FROM events")} == {"main": MAIN, "extra": EXTRA}
    assert st.station == MAIN and st.own_stations() == {MAIN, EXTRA}   # the extra one doesn't become the main radio
    st.close()


def test_each_station_syncs_on_its_own_pairing_and_bookmark(tmp_path):
    home = store_mod.Store(tmp_path / "hub.db")
    home.claim_station("!a0000009")
    hub = sync.Hub(home)
    st = store_mod.Store(tmp_path / "station.db")
    st.claim_station(MAIN)
    extra = store_mod.StationStore(st)
    extra.claim_station(EXTRA)
    for i in range(3):
        st.insert("events", ts=1.7e9 + i, kind="main")
        extra.insert("events", ts=1.7e9 + i, kind="extra")
    st.flush()
    posted = []

    def post(payload, what="station report", token=None):
        token = token or c.token
        posted.append((payload["station"], token))
        # the hub binds a pairing token to its station: a token can only ever send its own
        return hub.ingest(json.loads(json.dumps(payload)), bound_station=TOKENS[token])

    c = sync.Collector(st, "http://hub", "t" * 20, 60, report=lambda: {"name": "Main"},
                       extra=[{"station": lambda: EXTRA, "token": "u" * 20, "report": lambda: {"name": "Extra"}}])
    c._post = post
    assert c.sync_once() == 6
    got = {r["station"]: r["n"] for r in home.query("SELECT station, COUNT(*) AS n FROM events GROUP BY station")}
    assert got == {MAIN: 3, EXTRA: 3}
    assert {s for s, t in posted if t == "u" * 20} == {EXTRA} and {s for s, t in posted if t == "t" * 20} == {MAIN}
    assert c._progress()["events"] and c._progress(EXTRA)["events"]       # two bookmarks
    assert c.status["stations"] == {MAIN: 0, EXTRA: 0}
    posted.clear()
    assert c.sync_once() == 0 and all(p[0] in (MAIN, EXTRA) for p in posted)   # nothing resent, reports only
    home.close()
    st.close()


def test_a_hub_refuses_rows_for_any_of_its_own_radios(tmp_path):
    home = store_mod.Store(tmp_path / "hub.db")
    home.claim_station(MAIN)
    store_mod.StationStore(home).claim_station(EXTRA)
    hub = sync.Hub(home)
    for sid in (MAIN, EXTRA):
        with pytest.raises(sync.IngestError):
            hub.ingest({"protocol": sync.PROTOCOL, "station": sid, "table": "events",
                        "rows": [{"src_rowid": 1, "station": sid, "ts": 1.7e9, "kind": "x"}]})
    home.close()


def test_the_radios_setting(tmp_path, monkeypatch):
    p = tmp_path / "lorakeet.toml"
    monkeypatch.setenv("LORAKEET_CONFIG", str(p))
    ok = ('[radio]\nport = "COM7"\n[[radios]]\nprotocol = "meshcore"\nport = "COM12"\nname = "Desk MC"\n')
    p.write_text(ok, encoding="utf-8")
    assert config.load()["radios"][0]["port"] == "COM12"
    for bad in ('[[radios]]\nprotocol = "meshcore"\n',                                       # no port
                '[radio]\nport = "COM7"\n[[radios]]\nport = "COM7"\n',                       # the main radio's port
                '[[radios]]\nport = "COM12"\ncolour = "red"\n',                              # unknown setting
                '[[radios]]\nport = "COM12"\nprotocol = "lora"\n',
                '[sync]\nmode = "collector"\nhub_url = "http://h:5190"\ntoken = "' + "t" * 20 + '"\n'
                '[[radios]]\nport = "COM12"\n'):                                             # a collector's needs a token
        p.write_text(bad, encoding="utf-8")
        with pytest.raises(ValueError):
            config.load()


def test_auto_detect_leaves_an_extra_radios_port_alone(tmp_path, monkeypatch):
    st = store_mod.Store(tmp_path / "mesh.db")
    m = mesh_mod.Mesh(st, None)
    monkeypatch.setattr(mesh_mod, "radio_ports", lambda: [("COM7", "AA"), ("COM12", "BB")])
    monkeypatch.setattr(mesh_mod, "resolve_port", lambda p: p)
    assert m._pick_port() is None and m.needs_choice   # two radios, none remembered: it asks
    m.claimed_ports = {"COM12"}
    assert m._pick_port() == "COM7"                    # the other one is an extra radio's
    st.close()


def test_the_librarys_shared_events_stay_with_their_own_radio(tmp_path):
    st = store_mod.Store(tmp_path / "mesh.db")
    m = mesh_mod.Mesh(st, None)
    seen = []
    m.broadcast = lambda kind, data: seen.append(kind)
    m.iface = object()
    m._on_node({"num": 1, "user": {"id": "!a0000005"}}, interface=object())   # another radio's
    assert seen == []
    st.close()


def test_radios_open_their_ports_one_at_a_time(monkeypatch):
    # opening a USB radio's port restarts it: radios restarting together tripped a Pi 4's USB over-current limit
    import threading
    import time

    import link as link_mod

    monkeypatch.setattr(link_mod, "STAGGER_S", 0.4)
    monkeypatch.setattr(link_mod, "_last_open", [0.0])
    opened = []

    class Fake(link_mod.RadioLink):
        def __init__(self, there):
            super().__init__(None)
            self.there = there

        def connect(self):
            if self.there:
                time.sleep(0.05)   # a real port opening
                self.connected = True
                opened.append(time.monotonic())

    absent = Fake(False)
    absent.connect_staggered()            # no radio there: fails fast, doesn't hold the others back
    t0 = time.monotonic()
    links = [Fake(True), Fake(True)]
    threads = [threading.Thread(target=x.connect_staggered) for x in links]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert all(x.connected for x in links)
    assert opened[0] - t0 < 0.3                     # the first goes straight away
    assert opened[1] - opened[0] >= 0.4             # the second waits its turn
