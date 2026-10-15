"""meshcore_packet.py: MeshCore wire parsing, the firmware packet hash, radio clocks (docs/MESHCORE.md, decisions 3, 7, 9).

Frames are built here from the firmware's layout (Packet.cpp), not recorded: a recorded frame would carry real keys.
"""
import hashlib

import meshcore_packet as mp
import radioid


def frame(route, payload_type, hops=(), per_hop=1, payload=b"hello", transport=None):
    out = bytes([route | payload_type << 2])
    if route in (0, 3):
        a, b = transport or (0x1234, 0)
        out += a.to_bytes(2, "little") + b.to_bytes(2, "little")
    out += bytes([len(hops) | (per_hop - 1) << 6])
    for h in hops:
        out += bytes.fromhex(h)
    return out + payload


def test_a_flood_with_one_byte_hops():
    p = mp.parse(frame(1, 5, hops=("a1", "b2")))
    assert p["route"] == "flood" and p["payloadName"] == "GRP_TXT" and p["transport"] is None
    assert p["hops"] == ["a1", "b2"] and p["hopBytes"] == 1 and p["payload"] == b"hello"
    assert mp.parse(frame(1, 5, hops=("a1", "b2")).hex()) == p      # hex works as well as bytes


def test_wider_hops_and_region_scoped_floods():
    p = mp.parse(frame(0, 4, hops=("a10000", "b20000"), per_hop=3, transport=(0xBEEF, 7)))
    assert p["route"] == "transport flood" and p["transport"] == (0xBEEF, 7)
    assert p["hops"] == ["a10000", "b20000"] and p["hopBytes"] == 3 and p["payloadName"] == "ADVERT"
    assert mp.parse(frame(2, 2, hops=("a100",), per_hop=2))["hops"] == ["a100"]


def test_malformed_frames_are_rejected():
    assert mp.parse(b"") is None and mp.parse("zz") is None
    assert mp.parse(bytes([1 | 5 << 2, 0xC1, 0xAA]) + b"x") is None    # 4 bytes per hop is reserved
    assert mp.parse(bytes([1 | 5 << 2, 0x3F]) + b"\x00" * 10) is None   # 63 hops, 10 bytes: truncated
    assert mp.parse(frame(1, 5, payload=b"")) is None                   # no payload
    assert mp.parse(bytes([0, 1, 2])) is None                           # transport codes cut short


def test_the_packet_hash_ignores_the_route_taken():
    one = mp.packet_hash(frame(1, 5, hops=("a1",)))
    assert one == mp.packet_hash(frame(1, 5, hops=("a1", "b2", "c3"))) == mp.packet_hash(frame(0, 5, transport=(9, 9)))
    assert one == hashlib.sha256(bytes([5]) + b"hello").digest()[:8].hex()          # the firmware's recipe
    assert one != mp.packet_hash(frame(1, 4, hops=("a1",)))                       # payload type counts
    assert one != mp.packet_hash(frame(1, 5, payload=b"hellp"))
    # a trace hashes its path-length byte too, so its hops so far change it
    assert mp.packet_hash(frame(1, 9, hops=("a1",))) != mp.packet_hash(frame(1, 9, hops=("a1", "b2")))
    assert mp.packet_hash(b"\x05") is None


def test_radio_clocks():
    now = 1791800000
    assert mp.clock_status(now - 30, now) == "ok"
    assert mp.clock_status(1715770351 + 3600, now) == "unset"     # a 2024 seed plus an hour's uptime
    assert mp.clock_status(1772323200 + 86400 * 20, now) == "unset"
    assert mp.clock_status(0, now) == "unset" and mp.clock_status(None, now) == "unset"
    assert mp.clock_status(now - 3600, now) == "wrong"            # set, an hour off: not a seed
    assert mp.clock_status(now + 86400 * 30, now) == "wrong"


def test_path_hops_match_radios_by_prefix():
    mc = "mc:a1b2c30000ff"
    assert radioid.path_prefix_matches(mc, "a1") and radioid.path_prefix_matches(mc, "A1B2C3")
    assert not radioid.path_prefix_matches(mc, "b2") and not radioid.path_prefix_matches(mc, "a")
    assert not radioid.path_prefix_matches(mc, "a1b2c30000") and not radioid.path_prefix_matches("!a0000001", "a0")


def test_adverts_name_type_and_place():
    from meshcore_frames import KEY_A, SEATTLE, advert_payload, packet
    a = mp.parse_advert(advert_payload(KEY_A))
    assert a["publicKey"] == KEY_A.hex() and a["type"] == "repeater" and a["name"] == "Hilltop Rpt"
    assert (round(a["lat"], 4), round(a["lon"], 4)) == SEATTLE and a["timestamp"] == 1791000000
    bare = mp.parse_advert(advert_payload(KEY_A, kind=1, name=None, where=None))
    assert bare["type"] == "chat" and bare["name"] is None and bare["lat"] is None
    assert mp.parse_advert(advert_payload(KEY_A, where=(0, 0)))["lat"] is None   # 0,0 = no position
    assert mp.parse_advert(b"\x01" * 50) is None
    p = mp.parse(packet(4, advert_payload(KEY_A), path=["a0"]))
    assert p["payloadName"] == "ADVERT" and p["hops"] == ["a0"]
