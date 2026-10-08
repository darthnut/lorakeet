"""Make and install a private channel on our radios without ever printing its key.

    # once: a new channel with a random 256-bit key, saved as a Meshtastic "add channel" URL (keep it private:
    # anyone with the file can read the channel). Refuses to overwrite.
    python deploy/channel_tool.py generate --name Lorakeet --out <file>

    # on each radio (stop the logger first; it holds the serial port)
    python deploy/channel_tool.py install --port COM10 --url-file <file>

install adds the channel in the first free secondary slot. If a channel of that name is already there it
checks the key matches and changes nothing; a different key is an error (never silently replaced). The
primary channel, LoRa settings and every other channel are left alone. The key is reported only as its
size, never its value.
"""
import argparse
import base64
import os
import secrets
import sys

from meshtastic import apponly_pb2, channel_pb2


def generate(name, out, precision):
    if os.path.exists(out):
        sys.exit(f"{out} exists; not overwriting a channel key")
    s = channel_pb2.ChannelSettings(name=name, psk=secrets.token_bytes(32))
    s.module_settings.position_precision = precision
    cs = apponly_pb2.ChannelSet()
    cs.settings.append(s)
    frag = base64.urlsafe_b64encode(cs.SerializeToString()).decode().rstrip("=")
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(f"https://meshtastic.org/e/?add=true#{frag}\n")
    print(f"channel {name!r}: 256-bit key generated, saved to {out}")


def load(url_file):
    url = open(url_file).read().strip()
    frag = url.split("#", 1)[1]
    cs = apponly_pb2.ChannelSet()
    cs.ParseFromString(base64.urlsafe_b64decode(frag + "=" * (-len(frag) % 4)))
    if len(cs.settings) != 1:
        sys.exit("expected exactly one channel in the URL")
    return cs.settings[0]


def install(port, url_file):
    from meshtastic.serial_interface import SerialInterface
    s = load(url_file)
    i = SerialInterface(port)
    try:
        node = i.localNode
        active = [c for c in node.channels if c.role != channel_pb2.Channel.Role.DISABLED]
        same = [c for c in active if c.settings.name == s.name]
        if same:
            if bytes(same[0].settings.psk) == bytes(s.psk):
                print(f"{s.name!r} already on this radio (index {same[0].index}) with the same key; nothing to do")
                return
            sys.exit(f"{s.name!r} is on this radio with a DIFFERENT key; not replacing it")
        free = [c for c in node.channels if c.index > 0 and c.role == channel_pb2.Channel.Role.DISABLED]
        if not free:
            sys.exit("no free channel slot")
        slot = free[0]
        slot.settings.CopyFrom(s)
        slot.role = channel_pb2.Channel.Role.SECONDARY
        node.writeChannel(slot.index)
        print(f"{s.name!r} installed at index {slot.index} ({len(s.psk) * 8}-bit key)")
    finally:
        i.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("generate")
    g.add_argument("--name", required=True)
    g.add_argument("--out", required=True)
    g.add_argument("--precision", type=int, default=32, help="position precision on this channel (32 = exact)")
    n = sub.add_parser("install")
    n.add_argument("--port", required=True)
    n.add_argument("--url-file", required=True)
    a = ap.parse_args()
    generate(a.name, a.out, a.precision) if a.cmd == "generate" else install(a.port, a.url_file)
