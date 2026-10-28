"""The MeshCore link (meshcore_link.py): made-up companion-radio frames played through the `meshcore` library's own
reader (what a radio's USB frames go through) into a real Store, then the rows checked."""
import asyncio
import json
import struct
import threading

import pytest

try:   # a release that doesn't load on this Python (2.3.16 on 3.11) is a skip, not a failure to collect
    import meshcore  # noqa: F401
except (ImportError, SyntaxError) as e:
    pytest.skip(f"the meshcore package doesn't load here: {e}", allow_module_level=True)
from meshcore.events import EventDispatcher  # noqa: E402
from meshcore.reader import MessageReader  # noqa: E402

import meshcore_link  # noqa: E402
import store as store_mod  # noqa: E402
from meshcore_frames import KEY_A, KEY_SELF, advert_payload, grp_txt_payload, packet, rx_frame  # noqa: E402


def self_info_frame(name="Station Test"):
    """The companion's SELF_INFO (0x05): type, tx power, max power, key, lat, lon, flags, radio settings, name."""
    return (bytes([0x05, 1, 22, 22]) + KEY_SELF + struct.pack("<ii", 0, 0) + bytes([0, 0, 0, 0])
            + struct.pack("<II", 910525, 62500) + bytes([7, 5]) + name.encode())


def play(link, frames):
    async def go():
        d = EventDispatcher()
        await d.start()
        link.attach(d)
        r = MessageReader(d)
        for f in frames:
            await r.handle_rx(bytearray(f))
        await asyncio.sleep(0.2)   # the dispatcher hands events on in its own task
        await d.stop()
    # in a thread of its own: another test (Playwright's sync API) may have left an event loop running on this one
    done = []

    def worker():
        try:
            asyncio.run(go())
            done.append(None)
        except BaseException as e:  # noqa: BLE001 - handed to the test below
            done.append(e)
    t = threading.Thread(target=worker)
    t.start()
    t.join(30)
    assert done, "the frames didn't play within 30 s"
    if done[0] is not None:
        raise done[0]


@pytest.fixture
def link(tmp_path):
    st = store_mod.Store(tmp_path / "mesh.db")
    yield meshcore_link.MeshCoreLink(st, "COM99")
    st.close()


def rows(link, sql):
    return link.store.query(sql)


def test_the_radio_says_who_it_is(link):
    play(link, [self_info_frame()])
    assert link.local_id == "mc:c0000099d0d0" and link.store.station == "mc:c0000099d0d0"
    assert link.self_info["name"] == "Station Test" and link.status()["protocol"] == "meshcore"


def test_every_reception_is_kept_and_each_packet_once(link):
    advert = packet(4, advert_payload(KEY_A), path=["a0"])
    play(link, [self_info_frame(), rx_frame(advert, snr=6.5, rssi=-90), rx_frame(advert, snr=-3.25, rssi=-110),
                rx_frame(packet(5, grp_txt_payload(), path=["a0", "b1"]))])
    rx = rows(link, "SELECT * FROM mc_rx ORDER BY rowid")
    assert len(rx) == 3 and all(r["station"] == "mc:c0000099d0d0" for r in rx)
    first = rx[0]
    assert (first["snr"], first["rssi"], first["payload_name"], first["path"], first["hops"]) == (6.5, -90, "ADVERT", "a0", 1)
    assert first["from_id"] == "mc:a0000017c2be" and first["frame"] == advert.hex()
    assert rx[0]["packet_hash"] == rx[1]["packet_hash"] != rx[2]["packet_hash"]
    assert rx[2]["path"] == "a0,b1" and rx[2]["from_id"] is None and rx[2]["payload_name"] == "GRP_TXT"
    pk = rows(link, "SELECT * FROM packets ORDER BY rowid")
    assert [p["portnum"] for p in pk] == ["MC_ADVERT", "MC_GRP_TXT"]   # the advert's repeat isn't a second packet
    assert pk[0]["summary"] == "advert: Hilltop Rpt (repeater) via a0" and pk[0]["pkt_id"] > 0
    assert json.loads(pk[0]["raw"])["frame"] == advert.hex()


def test_adverts_give_identity_and_place_once(link):
    advert = packet(4, advert_payload(KEY_A))
    renamed = packet(4, advert_payload(KEY_A, name="Hilltop 2", ts=1791000600))
    play(link, [self_info_frame(), rx_frame(advert), rx_frame(advert), rx_frame(renamed)])
    ni = rows(link, "SELECT * FROM node_info WHERE node = 'mc:a0000017c2be' ORDER BY rowid")
    assert [r["long_name"] for r in ni] == ["Hilltop Rpt", "Hilltop 2"]          # a row per change, not per hearing
    assert ni[0]["role"] == "repeater" and ni[0]["public_key"] == KEY_A.hex()
    pos = rows(link, "SELECT * FROM positions WHERE node = 'mc:a0000017c2be'")
    assert len(pos) == 3 and round(pos[0]["lat"], 4) == 47.6062 and pos[0]["source"] == "advert"


def test_counters_and_a_malformed_frame(link):
    play(link, [self_info_frame(), rx_frame(bytes([0x11, 0x3F]))])   # 63 hops claimed, none there
    bad = rows(link, "SELECT * FROM mc_rx")
    assert len(bad) == 1 and bad[0]["packet_hash"] is None and bad[0]["frame"] == "113f"
    link.on_counters({"noise_floor": -98, "recv": 3}, type("K", (), {"value": "stats_radio"})())
    t = rows(link, "SELECT * FROM telemetry_full")
    assert t[0]["kind"] == "meshcore_stats_radio" and json.loads(t[0]["data"])["noise_floor"] == -98


def test_messages(link):
    play(link, [self_info_frame()])
    link.on_direct_message({"pubkey_prefix": "a0000017c2be", "text": "hello"})
    link.on_channel_message({"channel_idx": 0, "text": "Hilltop: hi all"})
    m = rows(link, "SELECT from_id, to_id, channel, text FROM messages ORDER BY rowid")
    assert m == [{"from_id": "mc:a0000017c2be", "to_id": "mc:c0000099d0d0", "channel": None, "text": "hello"},
                 {"from_id": None, "to_id": "^all", "channel": 0, "text": "Hilltop: hi all"}]


def test_a_meshcore_station_runs_as_a_mesh_and_keeps_the_pages_current(link):
    import mesh
    assert isinstance(link, mesh.Mesh)                          # the pages, alerts and reports call Mesh's methods
    play(link, [self_info_frame(), rx_frame(packet(4, advert_payload(KEY_A)), snr=5.0, rssi=-95)])
    a = "mc:a0000017c2be"
    assert link.name(a) == "Hilltop Rpt" and link.rx[a]["snr"] == 5.0 and round(link.pos[a]["lat"], 4) == 47.6062
    assert link.status()["localId"] == "mc:c0000099d0d0" and link.status()["protocol"] == "meshcore"
    with pytest.raises(RuntimeError, match="MeshCore"):
        link.send_text("hi")
    with pytest.raises(RuntimeError, match="MeshCore"):
        link.traceroute(a)


def test_the_protocol_setting(tmp_path, monkeypatch):
    import config
    p = tmp_path / "lorakeet.toml"
    monkeypatch.setenv("LORAKEET_CONFIG", str(p))
    p.write_text('[radio]\nprotocol = "meshcore"\nport = "COM99"\n', encoding="utf-8")
    config.file_settings()
    for bad in ('[radio]\nprotocol = "meshcore"\n', '[radio]\nprotocol = "meshcore"\nhost = "192.168.1.50"\nport = "COM9"\n',
                '[radio]\nprotocol = "reticulum"\n'):
        p.write_text(bad, encoding="utf-8")
        with pytest.raises(ValueError):
            config.file_settings()


def test_the_map_works_with_packets_from_unknown_senders(tmp_path):
    """Most MeshCore packets don't name their sender: packets.from_id is empty. A restart loads last-heard from
    packets, and an empty sender once broke the map (/api/state) for the whole station."""
    st = store_mod.Store(tmp_path / "mesh.db")
    first = meshcore_link.MeshCoreLink(st, "COM99")
    play(first, [self_info_frame(), rx_frame(packet(5, grp_txt_payload())), rx_frame(packet(4, advert_payload(KEY_A)))])
    st.flush()
    again = meshcore_link.MeshCoreLink(st, "COM99")       # as after a restart
    ids = {n["id"] for n in again.state()["nodes"]}
    assert None not in again.rx and "mc:a0000017c2be" in ids
    st.close()


def test_analytics_with_meshcore_channel_messages(tmp_path):
    """A MeshCore channel message has no sender id (its text starts "Name: "): the analytics overview names it
    instead of failing (it once failed the whole page for a MeshCore station)."""
    import analytics
    st = store_mod.Store(tmp_path / "mesh.db")
    link = meshcore_link.MeshCoreLink(st, "COM99")
    play(link, [self_info_frame(), rx_frame(packet(4, advert_payload(KEY_A)))])
    link.on_channel_message({"channel_idx": 1, "text": "Hilltop: hi all"})
    st.flush()
    out = analytics.compute(tmp_path / "mesh.db", "24h", link.local_id, link.describe)
    assert out and link.describe(None)["name"] == "(sender not named)"
    st.close()


def test_the_packet_browser_searches_meshcore_receptions(tmp_path):
    import packetsearch
    st = store_mod.Store(tmp_path / "mesh.db")
    link = meshcore_link.MeshCoreLink(st, "COM99")
    play(link, [self_info_frame(), rx_frame(packet(4, advert_payload(KEY_A), path=["a0"])),
                rx_frame(packet(5, grp_txt_payload(), path=["b1", "a0"])), rx_frame(packet(5, grp_txt_payload(0x22)))])
    st.flush()
    db, sid = tmp_path / "mesh.db", link.local_id
    every = packetsearch.search(db, "mc_rx", {}, station=sid)
    assert every["total"] == 3 and "packet_hash" in every["columns"] and every["facets"]["payload"][0] == "GRP_TXT"
    assert packetsearch.search(db, "mc_rx", {"payload": "ADVERT"}, station=sid)["total"] == 1
    assert packetsearch.search(db, "mc_rx", {"path": "a0"}, station=sid)["total"] == 2      # any hop, not just the first
    assert packetsearch.search(db, "mc_rx", {"path": "b1"}, station=sid)["total"] == 1
    assert packetsearch.search(db, "mc_rx", {"node": "mc:a0000017c2be", "kind": "broadcast", "to_id": "^all"},
                               station=sid)["total"] == 1                                       # filters it lacks are ignored
    assert "MC_ADVERT" in packetsearch.search_csv(db, "packets", {}, station=sid)
    st.close()


def test_the_map_knows_each_radios_protocol(link):
    play(link, [self_info_frame(), rx_frame(packet(4, advert_payload(KEY_A)))])
    nodes = {n["id"]: n for n in link.state()["nodes"]}
    assert nodes["mc:a0000017c2be"]["protocol"] == "meshcore" and link.status()["protocol"] == "meshcore"


def test_a_station_lists_its_own_radio(link):
    play(link, [self_info_frame()])
    me = {n["id"]: n for n in link.state()["nodes"]}.get("mc:c0000099d0d0")
    assert me and me["longName"] == "Station Test" and me["isLocal"] and me["protocol"] == "meshcore"


def test_a_silent_esp32_radio_is_restarted(link, monkeypatch):
    """Found on a station: the library's retry left an ESP32-S3 in its USB bootloader, silent until reset."""
    import mesh
    import serial
    resets = []

    class Port:
        def __init__(self, dev, vid):
            self.device, self.vid = dev, vid

    class FakeSerial:
        def __init__(self, port):
            resets.append(port)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass
    monkeypatch.setattr(serial, "Serial", FakeSerial)
    monkeypatch.setattr(mesh.list_ports, "comports", lambda: [Port("COM99", mesh.ESPRESSIF_VID), Port("COM98", 0x239A)])

    async def silent(*a, **k):
        return None
    import meshcore
    monkeypatch.setattr(meshcore.MeshCore, "create_serial", silent)
    monkeypatch.setattr(link, "_pick_port", lambda: "COM99")
    for _ in range(meshcore_link.RESET_AFTER_FAILS):
        link.connect()
    assert resets == ["COM99"] and not link.connected
    assert link.store.query("SELECT kind FROM events WHERE kind = 'radio_reset'")
    for _ in range(meshcore_link.RESET_AFTER_FAILS):      # again at once: not within RESET_EVERY_S
        link.connect()
    assert resets == ["COM99"]
    link._last_reset = 0
    monkeypatch.setattr(link, "_pick_port", lambda: "COM98")  # not an ESP32: never toggled
    for _ in range(meshcore_link.RESET_AFTER_FAILS):
        link.connect()
    assert resets == ["COM99"]


def test_packet_anatomy_from_what_the_link_logged(tmp_path):
    """End to end: a reception the link logged, laid out from its stored bytes (meshcore_anatomy.py)."""
    import web
    st = store_mod.Store(tmp_path / "mesh.db")
    link = meshcore_link.MeshCoreLink(st, "COM99")
    play(link, [self_info_frame(), rx_frame(packet(4, advert_payload(KEY_A)))])
    st.flush()
    rowid = st.query("SELECT rowid FROM packets WHERE portnum = 'MC_ADVERT'")[0]["rowid"]
    a = web.packet_anatomy(st, link, rowid)
    assert a["protocol"] == "meshcore" and all(c["ok"] for c in a["checks"]) and len(a["hearings"]) == 1
    assert {f["name"]: f["value"] for f in a["payload"]["fields"]}["name"] == "Hilltop Rpt"
    st.close()


def test_receptions_before_the_radio_says_who_it_is_wait_for_it(tmp_path):
    # a [[radios]] MeshCore radio beside a Meshtastic main radio: a reception arriving before SELF_INFO must not be
    # stored under the main radio's station
    st = store_mod.Store(tmp_path / "mesh.db")
    st.claim_station("!a0000001")
    lk = meshcore_link.MeshCoreLink(store_mod.StationStore(st), "COM99")
    play(lk, [rx_frame(packet(4, advert_payload(KEY_A))), self_info_frame()])
    st.flush()
    assert {r["station"] for r in st.query("SELECT station FROM mc_rx")} == {lk.local_id}
    assert lk.local_id.startswith("mc:") and not st.query("SELECT 1 FROM packets WHERE station = '!a0000001'")
    st.close()


class _AnswersThenFails:
    """A MeshCore radio that answers create_serial, then fails or is paused part way through the setup."""
    def __init__(self, link=None, fail=False):
        import types
        self.link, self.fail, self.closed = link, fail, False
        self.commands = types.SimpleNamespace(send_appstart=self.appstart)

    def subscribe(self, *a):
        pass

    async def appstart(self):
        if self.fail:
            raise RuntimeError("the radio went quiet")

    async def start_auto_message_fetching(self):
        if self.link:
            self.link.paused = True          # the pause lands mid-connect

    async def disconnect(self):
        self.closed = True


def _connect_with(link, monkeypatch, radio):
    import meshcore

    async def create(*a, **k):
        return radio
    monkeypatch.setattr(meshcore.MeshCore, "create_serial", create)
    monkeypatch.setattr(link, "_pick_port", lambda: "COM99")
    link.connect()


def test_a_connect_that_fails_part_way_lets_go_of_the_port(link, monkeypatch):
    radio = _AnswersThenFails(fail=True)
    _connect_with(link, monkeypatch, radio)
    assert radio.closed and not link.connected and link.mc is None


def test_a_pause_during_the_connect_lets_go_of_the_port(link, monkeypatch):
    radio = _AnswersThenFails(link)
    _connect_with(link, monkeypatch, radio)
    assert radio.closed and not link.connected and link.mc is None and link.paused
