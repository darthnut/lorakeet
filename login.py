"""Password login for other devices on the local network.

Without a password, this computer gets full access and other LAN devices at most a read-only view
(`[http] lan`). With one (`python server.py --set-password`), a LAN device that logs in gets full access too:
sending, traceroutes, settings.

- The password is never stored: only a salted scrypt hash, in `login.json` in the data folder (not in
  lorakeet.toml, which people paste when asking for help). Setting a new password logs every device out.
- A login gives the browser a random session token in an HttpOnly, SameSite=Strict cookie; only its SHA-256
  is kept, so the file can't be used to log in. Sessions last 30 days from their last use.
- Wrong passwords are rate limited per address (5 free tries, then a lockout that doubles, up to 15 min) and
  overall (30 failures a minute from everywhere together).
- It's plain HTTP: anyone who can watch your network traffic could read the password or the cookie. It keeps
  housemates and guests on your Wi-Fi from sending on your radio; it doesn't make the dashboard safe to put on
  the internet.
"""
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from pathlib import Path

COOKIE = "lk_session"
SESSION_S = 30 * 86400
MIN_LENGTH = 10
SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1}
FREE_TRIES = 5
LOCKOUT_S = 30           # after the free tries; doubles with each further failure
LOCKOUT_MAX_S = 15 * 60
GLOBAL_PER_MIN = 30
# At most this many password checks at once (each scrypt takes ~16 MB and real CPU): a burst of logins can't exhaust
# a Raspberry Pi's memory.
HASH_SLOTS = threading.BoundedSemaphore(2)


def _hash(password, salt, n, r, p):
    return hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, maxmem=64 * 1024 * 1024, dklen=32)


def _token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


class Login:
    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.Lock()
        self.fails = {}        # ip -> (count, locked_until)
        self.recent = []       # times of recent failures from anywhere
        self.inflight = set()  # addresses with a password check running: one at a time each
        self.data = self._load()

    def _load(self):
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self):
        tmp = self.path.with_suffix(".tmp")
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)  # private from birth
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(self.data, indent=1))
        os.replace(tmp, self.path)

    @property
    def enabled(self):
        return bool(self.data.get("password"))

    # ---- the password (set from the terminal only)

    def set_password(self, password):
        if len(password) < MIN_LENGTH:
            raise ValueError(f"use at least {MIN_LENGTH} characters")
        salt = secrets.token_bytes(16)
        with self.lock:
            self.data = {"password": {"scrypt": _hash(password, salt, **SCRYPT).hex(), "salt": salt.hex(), **SCRYPT,
                                      "set": int(time.time())},
                         "sessions": {}}  # a new password logs everyone out
            self._save()

    def clear_password(self):
        with self.lock:
            self.data = {}
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass

    def _check(self, password):
        pw = self.data.get("password")
        if not pw or not isinstance(password, str) or not password:
            return False
        got = _hash(password, bytes.fromhex(pw["salt"]), pw["n"], pw["r"], pw["p"])
        return hmac.compare_digest(got.hex(), pw["scrypt"])

    # ---- logging in

    def _wait(self, ip, now):
        """Seconds until `ip` may try again (call with the lock held)."""
        self.recent = [t for t in self.recent if now - t < 60]
        if len(self.recent) >= GLOBAL_PER_MIN:
            return max(1, int(60 - (now - self.recent[0])))
        _, until = self.fails.get(ip, (0, 0))
        return max(0, int(until - now + 0.999))

    def wait_s(self, ip, now=None):
        """Seconds until `ip` may try again (0 = now)."""
        with self.lock:
            return self._wait(ip, now or time.time())

    def login(self, ip, password, now=None):
        """A new session token, or None. Raise LockedOut while `ip` must wait.

        Every attempt is counted as a failure BEFORE the (slow) password check and forgiven if it succeeds, and each
        address gets one check at a time: firing many guesses in parallel can't slip past the lockout."""
        now = now or time.time()
        with self.lock:
            wait = self._wait(ip, now)
            if wait:
                raise LockedOut(wait)
            if ip in self.inflight:
                raise LockedOut(1)
            self.inflight.add(ip)
            n, _ = self.fails.get(ip, (0, 0))
            n += 1
            until = now + min(LOCKOUT_MAX_S, LOCKOUT_S * 2 ** (n - FREE_TRIES - 1)) if n > FREE_TRIES else 0
            self.fails[ip] = (n, until)
            self.recent.append(now)
        try:
            with HASH_SLOTS:
                ok = self._check(password)
        finally:
            with self.lock:
                self.inflight.discard(ip)
        if ok:
            with self.lock:
                self.fails.pop(ip, None)
                if now in self.recent:
                    self.recent.remove(now)
                token = secrets.token_urlsafe(32)
                sessions = self.data.setdefault("sessions", {})
                for h in [h for h, s in sessions.items() if now - s["seen"] > SESSION_S]:
                    del sessions[h]
                sessions[_token_hash(token)] = {"created": int(now), "seen": int(now), "ip": ip}
                self._save()
            return token
        return None

    def session(self, token, now=None):
        """True if `token` is a live session (and note it as used)."""
        if not self.enabled or not token:
            return False
        now = now or time.time()
        h = _token_hash(token)
        with self.lock:
            s = self.data.get("sessions", {}).get(h)
            if not s or now - s["seen"] > SESSION_S:
                return False
            if now - s["seen"] > 3600:  # write the file at most hourly per session
                s["seen"] = int(now)
                self._save()
            return True

    def logout(self, token):
        with self.lock:
            if self.data.get("sessions", {}).pop(_token_hash(token or ""), None) is not None:
                self._save()


class LockedOut(Exception):
    def __init__(self, wait_s):
        super().__init__(f"too many wrong passwords: try again in {wait_s} s")
        self.wait_s = wait_s


def cookie_value(header):
    """The session token from a Cookie header."""
    for part in (header or "").split(";"):
        k, _, v = part.strip().partition("=")
        if k == COOKIE:
            return v
    return None


def set_cookie(token):
    return f"{COOKIE}={token}; Path=/; Max-Age={SESSION_S}; HttpOnly; SameSite=Strict"


def clear_cookie():
    return f"{COOKIE}=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict"


def access(base, lan, logged_in):
    """What a request may do. `base` = client_access(ip): 'full' (this computer), 'view' (a LAN address) or None.

    'full', 'view' (read-only), 'login' (nothing but the login page until logged in) or None (refused)."""
    if base == "full":
        return "full"
    if base is None or lan == "off":
        return None
    if logged_in:
        return "full"
    return "view" if lan == "view" else "login"
