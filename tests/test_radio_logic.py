"""Firmware log parsing, channel hashes, airtime and other small pieces of radio logic."""
import datetime

import analytics
import anatomy
import server
import storage

RX = ("Lora RX (id=0x7c5b5d61 fr=0xc0000001 to=0xffffffff, transport = 0, WantAck=0, HopLim=6 Ch=0x8 encrypted "
      "len=50 rxSNR=6.5 rxRSSI=-29 hopStart=7 relay=0x22)")
TX = ("Started Tx (id=0x60097892 fr=0xa0000001 to=0xffffffff, transport = 0, WantAck=0, HopLim=3 Ch=0x8 encrypted "
      "len=102 hopStart=3 relay=0x01 priority=10)")


def test_reception_line_gives_the_full_header():
    h = server.parse_rx(RX)
    assert h["from_id"] == "!c0000001" and h["pkt_id"] == 0x7c5b5d61 and h["to_id"] == "^all" and h["directed"] == 0
    assert (h["hop_start"], h["hop_limit"], h["hops"]) == (7, 6, 1)
    assert h["relay"] == 0x22 and h["channel"] == 8 and h["length"] == 50 and h["encrypted"] == 1
    assert (h["snr"], h["rssi"]) == (6.5, -29)


def test_firmware_28_prints_the_channel_hash_in_decimal():
    # firmware 2.8.1's printPacket: "Ch=%d" where 2.7.26 had "Ch=0x%x"; the same hash must come out of both
    h = server.parse_rx(RX.replace("Ch=0x8", "Ch=8").replace("rxSNR", "rxtime=1791400000 rxSNR"))
    assert h["channel"] == 8
    assert server.parse_rx(RX.replace("Ch=0x8", "Ch=31"))["channel"] == 31
    assert server.parse_rx(RX.replace("Ch=0x8", "Ch=0x1f"))["channel"] == 31


def test_transmission_line_has_priority_and_no_signal():
    h = server.parse_tx(TX)
    assert h["from_id"] == "!a0000001" and h["priority"] == 10 and "snr" not in h and "rssi" not in h


def test_other_lines_are_ignored():
    assert server.parse_rx("Received position from=0xc0000001") is None
    assert server.parse_tx(RX) is None and server.parse_rx(TX) is None


def test_boot_window_text_prefix_is_stripped():
    assert server.strip_text_prefix("DEBUG | ??:??:?? 32 [RadioIf] " + TX) == TX


def test_longfast_channel_hash_matches_what_is_heard_on_air():
    default_key = server._expand_psk(bytes([1]))
    assert server._xor(b"LongFast") ^ server._xor(default_key) == 0x08


def test_airtime_and_frequency_for_longfast_us():
    r = anatomy.radio_layer({"region": "US", "channel_name": "LongFast"}, 77)
    assert r["freqMHz"] == 906.875 and r["slot"] == 20
    assert r["airtimeMs"] == 804.9  # the firmware's own "Packet RX: 805 ms" figure for a 77-byte frame


def test_relay_byte_resolution():
    assert analytics.resolve_relay(["!c00000ff"], set()) == ("!c00000ff", "unique")
    assert analytics.resolve_relay([], set()) == (None, "unknown")
    assert analytics.resolve_relay(["!a00000ff", "!b00000ff"], {"!b00000ff"}) == ("!b00000ff", "by-link")
    assert analytics.resolve_relay(["!a00000ff", "!b00000ff"], set()) == (None, "ambiguous")


def test_who_may_use_the_dashboard():
    assert server.client_access("127.0.0.1") == "full" and server.client_access("::1") == "full"
    assert server.client_access("192.168.1.50") == "view" and server.client_access("::ffff:192.168.1.50") == "view"
    assert server.client_access("8.8.8.8") is None and server.client_access("not an ip") is None


def test_backup_retention():
    today = datetime.date(2026, 10, 7)
    dates = [today - datetime.timedelta(days=n) for n in range(400)]
    keep = storage.keep_dates(today, dates)
    assert all(today - datetime.timedelta(days=n) in keep for n in range(7))       # the last week
    assert datetime.date(2026, 9, 6) in keep                                       # a Sunday within 5 weeks
    assert datetime.date(2026, 8, 2) not in keep                                   # an older Sunday
    assert datetime.date(2025, 11, 1) in keep and datetime.date(2025, 9, 1) not in keep  # 1st of month, 12 months
    assert storage.keep_dates(today, [datetime.date(2020, 1, 5)]) == {datetime.date(2020, 1, 5)}  # newest stays


def test_arrival_tag():
    assert server.arrival_of({"transportMechanism": "TRANSPORT_LORA"}) == "lora"
    assert server.arrival_of({"transportMechanism": "TRANSPORT_MQTT"}) == "mqtt"
    assert server.arrival_of({}) == "local"


def test_pi_power_flags_in_words():
    f = server.power_flags("0x50000")   # what the Pi reported after the bowling-night trip
    assert f["underVoltageSinceBoot"] and f["throttledSinceBoot"] and not f["underVoltageNow"]
    assert server.power_flags("0x50005")["underVoltageNow"] and server.power_flags("garbage") == {}


def test_health_events_only_when_something_changes():
    events, power, net = [], iter(["0x0", "0x0", "0x50005", "0x50000"]), iter(["LaserCats", "no network"])
    h = server.HealthWatch(lambda kind, **d: events.append((kind, d)), power=lambda: next(power), network=lambda: next(net))
    for t in (0, 10, 20, 30):  # every 10 s; the network every 30 s
        h.step(t)
    assert [(k, d.get("throttled") or d.get("network")) for k, d in events] == [
        ("power", "0x0"), ("network", "LaserCats"), ("power", "0x50005"), ("power", "0x50000"), ("network", "no network")]
    assert events[2][1]["underVoltageNow"] and events[2][1]["prev"] == "0x0"
