"""Made-up MeshCore packets and companion-radio frames for the tests (never a real radio: ids have four zeros in a
row, places are the documented Seattle example). Built byte by byte from the firmware's layouts."""
import struct

KEY_A = bytes.fromhex("a0000017c2be") + bytes(26)   # a repeater
KEY_B = bytes.fromhex("b00000aa11ee") + bytes(26)   # a chat radio
KEY_SELF = bytes.fromhex("c0000099d0d0") + bytes(26)  # the station's own radio
SEATTLE = (47.6062, -122.3321)


def advert_payload(key, kind=2, name="Hilltop Rpt", where=SEATTLE, ts=1791000000):
    flags = kind | (0x80 if name else 0) | (0x10 if where else 0)
    out = key + struct.pack("<I", ts) + bytes(64) + bytes([flags])
    if where:
        out += struct.pack("<ii", round(where[0] * 1e6), round(where[1] * 1e6))
    if name:
        out += name.encode()
    return out


def packet(payload_type, payload, route=1, path=(), hop_bytes=1, transport=None):
    """A raw packet: header, transport codes (transport routes), path length, path, payload."""
    out = bytes([(payload_type << 2) | route])
    if route in (0, 3):
        out += struct.pack("<HH", *(transport or (0, 0)))
    out += bytes([len(path) | ((hop_bytes - 1) << 6)])
    for hop in path:
        out += bytes.fromhex(hop)
    return out + payload


def rx_frame(raw_packet, snr=7.25, rssi=-88):
    """The companion's RX_LOG_DATA push: 0x88, SNR x4 (signed), RSSI (signed), the packet."""
    return bytes([0x88, round(snr * 4) & 0xFF, rssi & 0xFF]) + raw_packet


def grp_txt_payload(channel_hash=0x11, body=b"\x00" * 18):
    """A channel message as heard by a radio without the key: channel hash, MAC(2), ciphertext."""
    return bytes([channel_hash]) + b"\xbe\xef" + body
