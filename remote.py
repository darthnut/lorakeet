"""Commands to a station over the mesh, for when it can't be reached any other way ([remote] in lorakeet.toml).

Send the station's radio a direct message starting with "lk": "lk status", "lk gps", "lk help". It answers
with a direct message. Off unless [remote] enabled = true; read-only commands only (nothing restarts or
changes anything yet).

Who may send one: a command counts only if all of these hold, otherwise it's ignored (and logged):
  - it's a direct message to this station's radio, PKI-encrypted (firmware 2.5+). A PKI message is encrypted
    with a secret only the sender's private key and ours can produce, so it also proves who sent it;
  - the sender is in [remote] allow;
  - the key it was sent with is the one this database first recorded for that radio (a replaced or copied
    key is refused), and the key isn't on the compromised or shared lists (keyflags.py).
Messages on channels, even private ones, are never commands. Replies are rate-limited per sender.
"""
import shutil
import subprocess
import sys
import threading
import time

PREFIX = "lk"
COMMANDS = ("status", "gps", "help")
MIN_GAP_S = 10  # one reply per sender per this many seconds


def parse(text):
    """'lk status' -> 'status'; '' for a bare 'lk'; None if it isn't a command at all."""
    words = (text or "").strip().lower().split()
    if not words or words[0] != PREFIX:
        return None
    return words[1] if len(words) > 1 else ""


def authorize(packet, local_id, allow, pinned_key, key_flag):
    """(True, "") or (False, why) for a command packet."""
    frm, to = packet.get("fromId"), packet.get("toId")
    if not local_id or to != local_id:
        return False, "not a direct message to this station"
    if not packet.get("pkiEncrypted"):
        return False, "not PKI-encrypted, so the sender isn't proven"
    if frm not in allow:
        return False, "sender not in [remote] allow"
    if not pinned_key:
        return False, "no key on record for the sender yet"
    if packet.get("publicKey") != pinned_key:
        return False, "sent with a different key than the one on record"
    if key_flag:
        return False, f"the sender's key is {key_flag['kind']}"
    return True, ""


def _ago(s):
    s = max(0, int(s))
    return f"{s} s" if s < 90 else f"{s // 60} min" if s < 5400 else f"{s / 3600:.1f} h"


def network():
    """The active network connection (Linux with NetworkManager), else None."""
    if not sys.platform.startswith("linux") or not shutil.which("nmcli"):
        return None
    try:
        out = subprocess.run(["nmcli", "-t", "-f", "NAME,TYPE", "con", "show", "--active"],
                             capture_output=True, text=True, timeout=5).stdout
    except Exception:  # noqa: BLE001
        return None
    for line in out.splitlines():
        name, _, kind = line.rpartition(":")
        if kind in ("802-11-wireless", "802-3-ethernet", "gsm"):
            return name.replace("netplan-wlan0-", "").replace("netplan-eth0", "ethernet")
    return "no network"


def power(throttled):
    """Pi power flags in words: 'power OK', 'UNDER-VOLTAGE now' or 'under-voltage earlier'."""
    try:
        v = int(throttled, 16)
    except (TypeError, ValueError):
        return None
    return "UNDER-VOLTAGE now" if v & 0x1 else "under-voltage earlier" if v & 0x10000 else "power OK"


def status_text(name, uptime_s, net, sync, health, fix, now=None):
    """The 'lk status' reply. sync = {"lastOk", "backlog"} or None; fix = {"lat", "lon", "time"} or None."""
    now = now or time.time()
    parts = [f"{name}: up {_ago(uptime_s)}" if uptime_s is not None else name]
    if net:
        parts.append(net)
    if sync is not None:
        last = sync.get("lastOk")
        backlog = sum((sync.get("backlog") or {}).values())
        parts.append(f"sync {_ago(now - last)} ago, backlog {backlog}" if last else f"sync not yet, backlog {backlog}")
    if health.get("tempC") is not None:
        p = power(health.get("throttled"))
        parts.append(f"{health['tempC']:.0f}°C" + (f", {p}" if p else ""))
    parts.append(f"GPS fix {_ago(now - fix['time'])} old" if fix else "no GPS fix")
    return " · ".join(parts)


def gps_text(fix, now=None):
    if not fix:
        return "No GPS fix."
    now = now or time.time()
    s = f"{fix['lat']:.5f},{fix['lon']:.5f}"
    if fix.get("alt") is not None:
        s += f" · {fix['alt']:.0f} m"
    return s + f" · fix {_ago(now - fix['time'])} old"


HELP = "Commands (direct message): lk status · lk gps · lk help"


class Remote:
    """Watches incoming text messages for commands; answers in a thread so the radio callback never waits."""

    def __init__(self, mesh, allow, pinned_key, key_flag, status, fix):
        # pinned_key(node) / key_flag(node) / status() -> str / fix() -> dict or None, supplied by server.py
        self.mesh, self.allow = mesh, set(allow)
        self.pinned_key, self.key_flag, self.status, self.fix = pinned_key, key_flag, status, fix
        self.seen = set()       # (sender, packet id): a repeated copy is answered once
        self.last_reply = {}    # sender -> time of our last reply

    def handle(self, packet, text):
        cmd = parse(text)
        if cmd is None:
            return
        frm, pid = packet.get("fromId"), packet.get("id")
        if (frm, pid) in self.seen:
            return
        self.seen.add((frm, pid))
        ok, why = authorize(packet, self.mesh.local_id, self.allow, self.pinned_key(frm), self.key_flag(frm))
        if ok and time.time() - self.last_reply.get(frm, 0) < MIN_GAP_S:
            ok, why = False, "rate limited"
        self.mesh.event("remote_command", sender=frm, command=cmd, accepted=ok, reason=why or None)
        if not ok:
            return
        self.last_reply[frm] = time.time()
        threading.Thread(target=self._answer, args=(frm, cmd), daemon=True, name="remote").start()

    def _answer(self, frm, cmd):
        try:
            reply = (self.status() if cmd == "status" else gps_text(self.fix()) if cmd == "gps"
                     else HELP if cmd in ("help", "") else f"Unknown command '{cmd[:20]}'. {HELP}")
            self.mesh.send_text(reply.encode("utf-8")[:200].decode("utf-8", "ignore"), to=frm)  # 200-byte limit
        except Exception as e:  # noqa: BLE001 - a failed reply must never disturb the radio link
            self.mesh.event("remote_command", sender=frm, command=cmd, accepted=True, reason=f"reply failed: {e}")
