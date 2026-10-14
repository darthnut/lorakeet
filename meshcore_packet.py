"""MeshCore packets on the wire: header, route, path prefixes, the packet hash, and radio clocks (docs/MESHCORE.md).

Pure functions over the raw bytes a MeshCore radio reports for each reception (RX_LOG_DATA), so the parsing is
testable without a radio or the optional `meshcore` library. Wire format per the firmware's Packet.cpp:

    header(1)  bits 0-1 route type, bits 2-5 payload type, bits 6-7 payload version
    transport codes(4)  two uint16 LE, only for the transport routes (region-scoped floods)
    path_len(1)  bits 0-5 hop count, bits 6-7 bytes per hop minus one (3 is reserved: invalid)
    path(hops * bytes per hop)  one public-key prefix per repeater, in order
    payload(...)

The packet hash is the firmware's (Packet::calculatePacketHash), the same however a packet travelled, so it
identifies one transmission across repeats and paths. The library's own `pkt_hash` is a different, shorter hash.
"""
import hashlib
import time

ROUTES = {0: "transport flood", 1: "flood", 2: "direct", 3: "transport direct"}
PAYLOAD_TYPES = {0: "REQ", 1: "RESPONSE", 2: "TXT_MSG", 3: "ACK", 4: "ADVERT", 5: "GRP_TXT", 6: "GRP_DATA",
                 7: "ANON_REQ", 8: "PATH", 9: "TRACE", 10: "MULTIPART", 15: "RAW_CUSTOM"}
TRACE = 9
MAX_PATH_BYTES = 64

# A radio whose clock was never set counts up from a firmware seed instead of the real time:
#   1715770351  2024-05-15, VolatileRTCClock's base time
#   1772323200  2026-03-01, RTC_TIME_MIN on the nRF52 and ESP32 RTC paths
# Nothing earlier than the first seed is a real time either (a raw 0 is 1970).
CLOCK_SEEDS = (1715770351, 1772323200)
CLOCK_SEED_DRIFT_S = 400 * 86400   # a seeded clock has run at most this long (uptime) since its seed
CLOCK_TOLERANCE_S = 10 * 60        # within this of when we heard it: the radio's clock is right


def _bytes(raw):
    if isinstance(raw, (bytes, bytearray)):
        return bytes(raw)
    return bytes.fromhex(str(raw).strip())


def path_layout(path_len_byte):
    """(hops, bytes per hop) from the path-length byte, or None when the firmware would reject it."""
    hops, per_hop = path_len_byte & 0x3F, (path_len_byte >> 6) + 1
    if per_hop == 4 or hops * per_hop > MAX_PATH_BYTES:
        return None
    return hops, per_hop


def parse(raw):
    """A raw MeshCore packet (bytes or hex) -> dict, or None when it's too short or malformed.

    hops: the path's key prefixes as lowercase hex, first repeater first. For a trace these are the hops so far."""
    try:
        b = _bytes(raw)
    except ValueError:
        return None
    if not b:
        return None
    header = b[0]
    route = header & 0x03
    at = 1
    transport = None
    if route in (0, 3):
        if len(b) < at + 4:
            return None
        transport = (int.from_bytes(b[at:at + 2], "little"), int.from_bytes(b[at + 2:at + 4], "little"))
        at += 4
    if len(b) <= at:
        return None
    path_len_byte = b[at]
    at += 1
    layout = path_layout(path_len_byte)
    if layout is None:
        return None
    hops, per_hop = layout
    if len(b) < at + hops * per_hop:
        return None
    path = [b[at + i * per_hop:at + (i + 1) * per_hop].hex() for i in range(hops)]
    at += hops * per_hop
    payload = b[at:]
    if not payload:
        return None
    payload_type = (header >> 2) & 0x0F
    return {
        "route": ROUTES[route], "routeType": route,
        "payloadType": payload_type, "payloadName": PAYLOAD_TYPES.get(payload_type, f"TYPE{payload_type}"),
        "version": header >> 6,
        "transport": transport,          # (code1, code2): a region-scoped flood (transport routes only)
        "hops": path, "hopBytes": per_hop,
        "payload": payload,
        "hash": _hash(payload_type, path_len_byte, payload),
    }


def _hash(payload_type, path_len_byte, payload):
    h = hashlib.sha256(bytes([payload_type]))
    if payload_type == TRACE:   # the firmware hashes path_len as a uint16 for traces
        h.update(path_len_byte.to_bytes(2, "little"))
    h.update(payload)
    return h.digest()[:8].hex()


def packet_hash(raw):
    """The firmware's packet hash (16 hex digits), or None for a packet parse() rejects."""
    p = parse(raw)
    return p["hash"] if p else None


def clock_status(claimed, heard_at=None):
    """What a radio's own timestamp (an advert's, a message's) says about its clock, given when we heard it:
    "ok", "unset" (counting up from a firmware seed, or before any seed) or "wrong" (set, but off).
    Lorakeet dates everything by when it heard it; this only labels the sender's clock."""
    heard_at = time.time() if heard_at is None else heard_at
    if claimed is None:
        return "unset"
    if abs(claimed - heard_at) <= CLOCK_TOLERANCE_S:
        return "ok"
    if claimed < CLOCK_SEEDS[0]:
        return "unset"
    # a seeded clock runs behind by (now - seed - uptime): well behind, not merely off by an hour or a time zone
    if claimed < heard_at - 86400 and any(s <= claimed < s + CLOCK_SEED_DRIFT_S for s in CLOCK_SEEDS):
        return "unset"
    return "wrong"
