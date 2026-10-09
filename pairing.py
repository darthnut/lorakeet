"""Adding listening stations to a hub without editing files: pairing codes, the hub's list of paired stations, and
a plain-words connection test.

- **Pairing** (hub, the Stations page's "Add a station"): a random token for one station, wrapped with the hub's
  addresses into one code, `lk1-...`, shown once. The hub keeps only the token's SHA-256, in `stations.json` in its
  data folder (readable by this user only; not lorakeet.toml). The first station to send with a token claims it:
  from then on that token works only for that radio, so a copied code can't be reused by another. Revoking ends it.
- **Joining** (station: the setup page, the Stations page, or `python server.py --join <code>`): the code's addresses
  are tried in order with a hello request; the first that answers becomes `[sync] hub_url`, and the token goes into
  `[sync] token` (a station must be able to send it).
- The hand-configured ways (`[sync] token`, `station_tokens`) keep working beside this.
- **Peering** (two hubs sharing with each other): the receiving hub makes a *peer* code ("Let another hub send
  here"); the sending hub adds it as a peer (`Peers`, `peers.json`, this user only: it holds the token) with what to
  share. A peer pairing is claimed by the first hub that uses it (its install id). Each direction is separate.
"""
import base64
import hashlib
import hmac
import ipaddress
import json
import os
import secrets
import socket
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

CODE_PREFIX = "lk1-"
TAILSCALE_NETS = ["100.64.0.0/10", "fd7a:115c:a1e0::/48"]
LAN_NETS = ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16", "fc00::/7", "fe80::/10"]


def _hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


# ---------------------------------------------------------------- pairing codes

def make_code(urls, token, hub_name="", kind="station", hub_id=""):
    payload = json.dumps({"v": 1, "kind": kind, "hubs": list(urls), "token": token, "name": hub_name, "hubId": hub_id},
                         separators=(",", ":"))
    return CODE_PREFIX + base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")


def parse_code(code):
    """{hubs: [url], token, name} from a pairing code; ValueError with a plain reason otherwise."""
    code = "".join((code or "").split())  # pasted codes pick up line breaks
    if not code.startswith(CODE_PREFIX):
        raise ValueError("that isn't a Lorakeet pairing code (they start with lk1-)")
    raw = code[len(CODE_PREFIX):]
    try:
        d = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
    except Exception as e:  # noqa: BLE001
        raise ValueError("the pairing code is incomplete or damaged: copy all of it") from e
    hubs = [u for u in d.get("hubs") or [] if isinstance(u, str) and u.startswith(("http://", "https://"))]
    if d.get("v") != 1 or not hubs or not isinstance(d.get("token"), str) or len(d["token"]) < 16:
        raise ValueError("the pairing code is incomplete or damaged: copy all of it")
    return {"hubs": hubs, "token": d["token"], "name": str(d.get("name") or "")[:60],
            "kind": "peer" if d.get("kind") == "peer" else "station", "hubId": str(d.get("hubId") or "")[:64]}


# ---------------------------------------------------------------- the hub's paired stations

class Pairings:
    """stations.json: [{id, label, tokenHash, created, station, boundAt, revoked}]. Thread-safe; tokens never stored."""

    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.Lock()
        try:
            self.entries = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.entries = []

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)  # private from birth (peers.json holds tokens)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(self.entries, indent=1))
        os.replace(tmp, self.path)
        if os.name == "nt":
            try:
                from radio_setup import _icacls
                _icacls(self.path, "F")
            except Exception:  # noqa: BLE001 - the data folder is already the user's; this is belt and braces
                pass

    def create(self, label, kind="station"):
        label = (label or "").strip()[:60] or ("A peer hub" if kind == "peer" else "New station")
        token = secrets.token_urlsafe(24)
        e = {"id": secrets.token_hex(4), "kind": kind, "label": label, "tokenHash": _hash(token), "created": int(time.time()),
             "station": None, "hub": None, "boundAt": None, "revoked": None, "lastContact": None}
        with self.lock:
            self.entries.append(e)
            self._save()
        return e, token

    def find(self, token):
        """The entry a token belongs to (a copy, without its hash), or None. Doesn't claim anything."""
        if not token:
            return None
        h = _hash(token)
        with self.lock:
            e = next((x for x in self.entries if hmac.compare_digest(x["tokenHash"], h)), None)
            return {k: v for k, v in e.items() if k != "tokenHash"} if e else None

    def bind(self, pid, station=None, hub=None):
        """Claim an unclaimed pairing for `station` (a station's) or `hub` (a peer's). False if it's taken or gone."""
        with self.lock:
            for e in self.entries:
                if e["id"] == pid and not e["revoked"]:
                    key, val = ("hub", hub) if e.get("kind") == "peer" else ("station", station)
                    if e.get(key) not in (None, val) or not val:
                        return False
                    # a hub id is self-declared: one held by another LIVE peer pairing can't be claimed again, or a
                    # second peer could write into the first one's stations and "delete what it sent" would delete the
                    # first one's data. A revoked one can: re-pairing the same hub with a new code (after a leak) must
                    # work, and a new code is the operator's say-so.
                    if key == "hub" and any(x is not e and x.get("kind") == "peer" and x.get("hub") == val
                                            and not x["revoked"] for x in self.entries):
                        return False
                    if e.get(key) is None:
                        e.update({key: val, "boundAt": int(time.time())})
                        self._save()
                    return True
        return False

    def seen(self, pid):
        """Note a peer's contact (at most every 5 min, it's a file write)."""
        with self.lock:
            for e in self.entries:
                if e["id"] == pid and (not e.get("lastContact") or time.time() - e["lastContact"] > 300):
                    e["lastContact"] = int(time.time())
                    self._save()

    def is_revoked(self, owner):
        """For the hub's station owners: "pair:<id>" whose pairing is revoked (or forgotten)."""
        if not owner or not owner.startswith("pair:"):
            return False
        pid = owner.split(":", 1)[1]
        with self.lock:
            e = next((x for x in self.entries if x["id"] == pid), None)
            return e is None or bool(e["revoked"])

    def check(self, token, station, bind=True, hub=None):
        """'ok' (a station's pairing; it claims `station` on first use), 'peer' (a peer hub's; it claims `hub`, the
        sender's install id, on first use), 'revoked', 'other-station' / 'other-hub', or None (unknown token)."""
        if not token:
            return None
        h = _hash(token)
        with self.lock:
            e = next((x for x in self.entries if hmac.compare_digest(x["tokenHash"], h)), None)
            if e is None:
                return None
            if e["revoked"]:
                return "revoked"
            if e.get("kind") == "peer":
                if e.get("hub") is None and bind and hub:
                    e.update(hub=hub, boundAt=int(time.time()))
                if e.get("hub") and hub and e["hub"] != hub:
                    return "other-hub"
                if bind and (not e.get("lastContact") or time.time() - e["lastContact"] > 300):
                    e["lastContact"] = int(time.time())
                    self._save()
                return "peer"
            if e["station"] is None:
                if bind and station:
                    e.update(station=station, boundAt=int(time.time()))
                    self._save()
                return "ok"
            return "ok" if e["station"] == station or not station else "other-station"

    def set_paused(self, pid, paused):
        """Pause (or resume) a peer hub's pairing: its batches are declined until resumed, and it keeps them pending,
        so nothing is lost. Unlike revoking, its code keeps working. True if it's a live peer pairing."""
        with self.lock:
            for e in self.entries:
                if e["id"] == pid and e.get("kind") == "peer" and not e["revoked"]:
                    e["paused"] = bool(paused)
                    self._save()
                    return True
        return False

    def revoke(self, pid):
        with self.lock:
            for e in self.entries:
                if e["id"] == pid and not e["revoked"]:
                    e["revoked"] = int(time.time())
                    self._save()
                    return True
        return False

    def forget(self, pid=None, station=None):
        with self.lock:
            before = len(self.entries)
            self.entries = [e for e in self.entries if not (e["id"] == pid or (station and e["station"] == station))]
            if len(self.entries) != before:
                self._save()

    def list(self):
        with self.lock:
            return [{k: v for k, v in e.items() if k != "tokenHash"} for e in self.entries]

    def by_hub(self, hub):
        with self.lock:
            return next(({k: v for k, v in e.items() if k != "tokenHash"} for e in self.entries
                         if e.get("kind") == "peer" and e.get("hub") == hub), None)


# ---------------------------------------------------------------- the hubs this one sends to (peering)

class Peers:
    """peers.json: [{id, name, url, token, share, forward, hubId, added}]. Holds tokens, so this user only, and
    never returned to the page (list() drops them)."""

    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.Lock()
        try:
            self.entries = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.entries = []

    _save = Pairings._save

    def add(self, name, url, token, share="default", forward=False, hub_id="", exact=False):
        e = {"id": secrets.token_hex(4), "name": (name or "Peer hub")[:60], "url": url, "token": token,
             "share": share, "forward": bool(forward), "exact": bool(exact), "hubId": hub_id, "added": int(time.time()),
             "paused": False}
        with self.lock:
            self.entries.append(e)
            self._save()
        return dict(e)

    def update(self, pid, **kw):
        with self.lock:
            for e in self.entries:
                if e["id"] == pid:
                    e.update({k: v for k, v in kw.items() if k in ("share", "forward", "exact", "name", "url", "paused")})
                    self._save()
                    return dict(e)
        return None

    def remove(self, pid):
        with self.lock:
            self.entries = [e for e in self.entries if e["id"] != pid]
            self._save()

    def get(self, pid):
        with self.lock:
            return next((dict(e) for e in self.entries if e["id"] == pid), None)

    def all(self):
        with self.lock:
            return [dict(e) for e in self.entries]

    def list(self):
        return [{k: v for k, v in e.items() if k != "token"} for e in self.all()]


def install_id(data_dir):
    """A random id for this Lorakeet install, made once: how hubs recognise each other (never sending a peer back
    what came from it)."""
    p = Path(data_dir) / "install_id"
    try:
        v = p.read_text(encoding="utf-8").strip()
        if v:
            return v
    except OSError:
        pass
    v = secrets.token_hex(8)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(v, encoding="utf-8")
    return v


# ---------------------------------------------------------------- where stations can reach this hub

def hub_addresses(port):
    """[{url, kind, ip}] this computer can be reached at: its local-network and Tailscale addresses."""
    ips, primary = set(), None
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None):
            ips.add(info[4][0].split("%")[0])
    except OSError:
        pass
    try:  # the address of the default route (no packet is sent): the one other computers most likely reach
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))
            primary = s.getsockname()[0]
            ips.add(primary)
    except OSError:
        pass
    out = []
    for ip in ips:
        try:
            a = ipaddress.ip_address(ip)
        except ValueError:
            continue
        if a.is_loopback or a.is_link_local or a.is_multicast:
            continue
        kind = ("tailscale" if any(a in ipaddress.ip_network(n) for n in TAILSCALE_NETS) else
                "lan" if a.is_private else None)
        if kind:
            host = f"[{ip}]" if a.version == 6 else ip
            out.append({"url": f"http://{host}:{port}", "kind": kind, "ip": ip, "primary": ip == primary})
    # IPv6 only where there's no IPv4 of that kind (a PC has several temporary IPv6 addresses, all the same route)
    v4kinds = {x["kind"] for x in out if ":" not in x["ip"]}
    out = [x for x in out if ":" not in x["ip"] or x["kind"] not in v4kinds]
    # the main address first, then Tailscale, then other adapters (VirtualBox's, Hyper-V's...): a station tries them
    # in this order, and an unreachable one costs it a timeout
    return sorted(out, key=lambda x: (not x["primary"], x["kind"] != "tailscale", x["ip"]))


def allow_networks(lan, tailscale):
    return (LAN_NETS if lan else []) + (TAILSCALE_NETS if tailscale else [])


def allow_flags(networks):
    nets = set(networks or [])
    return {"lan": all(n in nets for n in LAN_NETS[:3]), "tailscale": TAILSCALE_NETS[0] in nets}


# ---------------------------------------------------------------- the station's connection test

def hello(url, token, station=None, timeout=8, hub_id=None):
    """One request to a hub's /api/ingest/hello. Returns {ok, url, hubName, hubVersion} or {ok: False, url, error, hint}."""
    req = urllib.request.Request(url.rstrip("/") + "/api/ingest/hello", data=b"{}", method="POST", headers={
        "Content-Type": "application/json", "Authorization": f"Bearer {token}", "X-Lorakeet-Station": station or "",
        "X-Lorakeet-Hub": hub_id or ""})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read())
            return {"ok": True, "url": url, "hubName": d.get("hub"), "hubVersion": d.get("version"), "hubId": d.get("hubId"),
                    "paused": bool(d.get("paused"))}
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read()).get("error", "")
        except Exception:  # noqa: BLE001
            msg = ""
        hint = {
            401: "The hub didn't accept this station's token: it may have been revoked. Make a new pairing code on the hub.",
            403: ("The hub doesn't accept stations from this network. On the hub's Stations page, allow its local network "
                  "(or Tailscale)." if "network" in msg else "The hub refused this station: " + msg),
            404: "That's a Lorakeet, but not a hub (or an older version). On it, open Stations and turn on hub mode.",
        }.get(e.code, f"The hub answered with an error ({e.code} {msg}).")
        return {"ok": False, "url": url, "error": f"HTTP {e.code} {msg}".strip(), "hint": hint}
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        reason = str(getattr(e, "reason", e))
        return {"ok": False, "url": url, "error": reason,
                "hint": "Can't reach the hub at this address. Check the hub computer is on and running Lorakeet, that this "
                        "station is on the same network (or Tailscale), and that the hub's firewall lets other computers "
                        "reach Lorakeet's port."}


def test_hubs(urls, token, station=None, hub_id=None):
    """Try each address in order; the first that answers wins. Returns (result, tried)."""
    tried = []
    for u in urls:
        r = hello(u, token, station, hub_id=hub_id)
        tried.append(r)
        if r["ok"]:
            return r, tried
        if r["error"].startswith("HTTP"):  # it answered: another address won't change the hub's mind
            return r, tried
    return (tried[-1] if tried else {"ok": False, "error": "no addresses", "hint": "The pairing code has no addresses."}), tried
