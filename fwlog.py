"""Parsing the radio firmware's debug log: the over-the-air reception (`Lora RX (`) and transmission
(`Started Tx (`) lines that rx_hops and tx_log are built from.
"""
import logging
import re

from store import node_id


log = logging.getLogger("meshdash")


ANSI = re.compile(r"\x1b\[[0-9;]*m")  # firmware colours its plain-text boot output

RX_FIELDS = re.compile(r"\b(id|fr|to|transport|Ch|HopLim|hopStart|relay|nextHop|WantAck|len|rxSNR|rxRSSI|priority)\s*=\s*(-?[0-9a-fA-Fx.]+)")
BROADCAST_NUM = 0xFFFFFFFF


# Plain-text log lines (boot, before the API switches to structured records) carry a prefix like
# "DEBUG | ??:??:?? 32 [RadioIf] " in front of the message itself.
TEXT_PREFIX = re.compile(r"^(?:[A-Z]+\s*\|\s*\S+\s+\d+\s+)?(?:\[[^\]]+\]\s+)?")


def strip_text_prefix(line):
    return TEXT_PREFIX.sub("", line, count=1)


def parse_rx(message):
    """Every header field from a firmware 'Lora RX (...)' line, or None."""
    if not message.startswith("Lora RX ("):
        return None
    return _parse_header(message)


def parse_tx(message):
    """Every header field from a firmware 'Started Tx (...)' line (our radio transmitting), or None."""
    if not message.startswith("Started Tx ("):
        return None
    h = _parse_header(message, tx=True)
    if h:
        for k in ("snr", "rssi"):
            h.pop(k, None)
    return h


def _parse_header(message, tx=False):
    f = dict(RX_FIELDS.findall(message))
    try:
        frm = int(f["fr"], 16)
        hop_start, hop_lim = int(f["hopStart"]), int(f["HopLim"])
    except (KeyError, ValueError):
        return None
    relay = int(f["relay"], 16) if f.get("relay") else None
    to = int(f["to"], 16) if f.get("to") else None
    return {"from_id": node_id(frm), "relay": relay or None,
            "pkt_id": int(f["id"], 16) if f.get("id") else None,
            # 2.7 prints the channel hash in hex ("Ch=0x1f"), 2.8.1 in decimal ("Ch=31"): read what's there
            "channel": (int(f["Ch"], 16) if f["Ch"].lower().startswith("0x") else int(f["Ch"])) if f.get("Ch") else None,
            "directed": None if to is None else int(to != BROADCAST_NUM),
            "to_id": None if to is None else ("^all" if to == BROADCAST_NUM else node_id(to)),
            "want_ack": int(f["WantAck"]) if f.get("WantAck") else None,
            "next_hop": int(f["nextHop"], 16) if f.get("nextHop") else None,
            "length": int(f["len"]) if f.get("len") else None,
            "encrypted": int(" encrypted " in message),
            "transport": int(f["transport"]) if f.get("transport") else None,
            "hop_start": hop_start, "hop_limit": hop_lim,
            "hops": hop_start - hop_lim if hop_start >= hop_lim else None,
            "snr": float(f["rxSNR"]) if "rxSNR" in f else None,
            "rssi": int(float(f["rxRSSI"])) if "rxRSSI" in f else None,
            **({"priority": int(f["priority"]) if f.get("priority") else None} if tx else {})}
