"""NODE_STATUS_APP (port 36) summaries: a node's status string, protobuf field 1.

The meshtastic library has no message type for this port and hands Lorakeet the payload as raw bytes (it re-slots
decoded["payload"] with the packet's bytes for every port). node_status_text used to base64-decode it as if it were
the stored JSON's text, so every summary came out empty. Found by the Android collector's row checks (2026-11-24).
"""
import base64

from mesh import node_status_text

STATUS = b"\x0a\x0bHiking 0000"  # field 1 (string), 11 bytes


def test_raw_bytes_from_the_library():
    assert node_status_text(STATUS) == "Hiking 0000"


def test_base64_text_as_stored_in_raw():
    # packets.raw keeps the payload as base64 (store.clean): reading it back from there works too
    assert node_status_text(base64.b64encode(STATUS).decode()) == "Hiking 0000"


def test_other_fields_are_skipped():
    # field 2 varint 150, then field 1
    assert node_status_text(b"\x10\x96\x01" + STATUS) == "Hiking 0000"


def test_long_status_is_cut_to_120_characters():
    text = "é" * 130
    b = text.encode()
    assert node_status_text(bytes([0x0A, len(b) & 0x7F | 0x80, len(b) >> 7]) + b) == "é" * 120


def test_nothing_or_garbage_gives_empty():
    assert node_status_text(None) == ""
    assert node_status_text(b"") == ""
    assert node_status_text(b"\x0a\xff") == ""  # a length running past the end
