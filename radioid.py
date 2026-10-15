"""What a radio id is: the one place that knows (docs/MESHCORE.md, decision 2).

    Meshtastic  !1234abcd          the node number, 8 hex digits
    MeshCore    mc:a0000017c2be    the first 12 hex digits of the radio's 32-byte public key

Everything that validates an id, tells an id apart from a relay label ("0x22", "unresolved"), or does Meshtastic-only
relay-byte arithmetic asks here, never matches on "!" or a length itself. MeshCore ids are recognised already but
only ACCEPTED (by ingest, station ids, settings) once Lorakeet logs MeshCore.
"""
import re

MESHTASTIC_RE = re.compile(r"![0-9a-f]{8}")
MESHCORE_RE = re.compile(r"mc:[0-9a-f]{12}")
BROADCAST = ("^all", "!ffffffff")

ACCEPTED = (MESHTASTIC_RE, MESHCORE_RE)


def is_radio_id(s):
    """A radio id Lorakeet accepts (a station, a sender, a setting naming a radio). Not the broadcast address."""
    return isinstance(s, str) and any(rx.fullmatch(s) for rx in ACCEPTED) and s not in BROADCAST


def is_meshtastic(s):
    """A Meshtastic node id: the only kind with a node number, so the only kind with a 1-byte relay ID."""
    return isinstance(s, str) and bool(MESHTASTIC_RE.fullmatch(s))


def is_meshcore(s):
    return isinstance(s, str) and bool(MESHCORE_RE.fullmatch(s))


def protocol_of(s):
    """"meshtastic", "meshcore" or None."""
    return "meshtastic" if is_meshtastic(s) else "meshcore" if is_meshcore(s) else None


def node_num(s):
    """A Meshtastic id's node number (int), or None for anything else."""
    return int(s[1:], 16) if is_meshtastic(s) else None


def relay_byte(s):
    """The 1-byte relay ID a Meshtastic radio stamps on packets it relays (its node number's last byte), or None."""
    return int(s[-2:], 16) if is_meshtastic(s) else None


def path_prefix_matches(s, prefix):
    """Whether a MeshCore radio could be the repeater behind a path hop: a 1-3 byte prefix of its public key, as
    hex (meshcore_packet.parse's hops). Like a relay byte, a short prefix can match several radios."""
    p = str(prefix).lower()
    return is_meshcore(s) and 2 <= len(p) <= 6 and len(p) % 2 == 0 and s[3:].startswith(p)


def split_station_token(entry):
    """A sync.station_tokens entry, "<station id>:<token>", split at its LAST colon (a MeshCore id has one of its
    own). Returns (station id, token)."""
    sid, _, tok = str(entry).rpartition(":")
    return sid, tok


def from_meshcore_key(public_key):
    """A MeshCore radio's id from its public key (hex string or bytes; at least 6 bytes)."""
    h = public_key.hex() if isinstance(public_key, (bytes, bytearray)) else str(public_key).lower()
    if not re.fullmatch(r"[0-9a-f]{12,}", h):
        raise ValueError("a MeshCore public key is hex, at least 12 digits")
    return "mc:" + h[:12]
