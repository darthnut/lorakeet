"""Database size monitoring, nightly backups, and desktop notifications for the dashboard.

Nothing here deletes logged data. When mesh.db reaches the size limit we only *say so* (dashboard
banner + Windows notification) and leave the decision to the user.

Backups: a consistent snapshot via SQLite's online-backup API (safe while the server keeps
writing), integrity-checked, gzipped, then copied to each destination through a temporary name and
renamed, so a half-synced file never looks like a finished backup.
"""
import gzip
import json
import logging
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from config import CFG

log = logging.getLogger("meshdash.storage")

WARN_BYTES = int(CFG["storage"]["db_warn_gb"] * 1024**3)
BACKUP_AT = tuple(int(x) for x in CFG["backup"]["time"].split(":"))  # local time, daily
BACKUP_STALE_S = 48 * 3600        # warn when the last good backup is older than this
DEFAULT_DESTS = []  # from lorakeet.toml [backup] destinations
NAME_FMT = "mesh-%Y-%m-%d.db.gz"


def file_bytes(path):
    """A SQLite database's real size includes its write-ahead log."""
    total = 0
    for p in (path, Path(str(path) + "-wal")):
        try:
            total += p.stat().st_size
        except OSError:
            pass
    return total


def keep_dates(today, dates):
    """Retention: last 7 days, Sundays for 5 weeks, 1st-of-month for 12 months."""
    keep = set()
    for d in dates:
        age = (today - d).days
        if age < 7 or (d.weekday() == 6 and age < 35) or (d.day == 1 and age < 366):
            keep.add(d)
    if dates:
        keep.add(max(dates))  # never delete the newest, whatever its date
    return keep


def notify(title, body):
    """Windows toast via PowerShell's WinRT bridge. Text is passed through env vars, not the command line.
    On other platforms this only logs (pluggable notifications are planned)."""
    if sys.platform != "win32":
        log.info("notification (no desktop notifier on this platform): %s: %s", title, body)
        return
    script = (
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime] | Out-Null;"
        "$x = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent("
        "[Windows.UI.Notifications.ToastTemplateType]::ToastText02);"
        "$t = $x.GetElementsByTagName('text');"
        "$t.Item(0).AppendChild($x.CreateTextNode($env:MD_TITLE)) | Out-Null;"
        "$t.Item(1).AppendChild($x.CreateTextNode($env:MD_BODY)) | Out-Null;"
        "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("
        "'{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe')"
        ".Show([Windows.UI.Notifications.ToastNotification]::new($x))"
    )
    try:
        subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                       env={**os.environ, "MD_TITLE": title, "MD_BODY": body}, timeout=30,
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception:  # noqa: BLE001 - a missed toast must never break the server
        log.exception("desktop notification failed")


class Storage:
    def __init__(self, db_path, debug_path, data_dir, on_change=None, on_event=None, dests=None):
        self.db_path = Path(db_path)
        self.debug_path = Path(debug_path)
        self.tmp_dir = Path(data_dir) / "backup-tmp"
        self.state_path = Path(data_dir) / "storage_state.json"
        self.dests = [Path(d) for d in (dests if dests is not None else DEFAULT_DESTS)]
        self.on_change = on_change or (lambda s: None)
        self.on_event = on_event or (lambda kind, **d: None)
        self.state = self._load_state()
        self.lock = threading.Lock()
        self.running = False

    # -- persisted state (last backup, last notifications) survives restarts
    def _load_state(self):
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save_state(self):
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=1), encoding="utf-8")
        os.replace(tmp, self.state_path)

    # -- status for the dashboard
    def status(self):
        db = file_bytes(self.db_path)
        last_ok = self.state.get("last_ok_ts")
        return {
            "dbBytes": db, "debugBytes": file_bytes(self.debug_path), "warnBytes": WARN_BYTES,
            "overLimit": db >= WARN_BYTES, "dbPath": str(self.db_path),
            "lastBackup": self.state.get("last_backup"), "lastOkTs": last_ok,
            "backupStale": bool(last_ok is None or time.time() - last_ok > BACKUP_STALE_S),
            "backupRunning": self.running, "nextBackupTs": self._next_due().timestamp(),
            "dests": [str(d) for d in self.dests],
        }

    def _next_due(self):
        """The most recent 03:30 slot if no good backup covers it yet (i.e. due now), else the next one.

        After a failure, retry hourly rather than every loop: each attempt compresses the whole DB.
        """
        now = datetime.now()
        slot = now.replace(hour=BACKUP_AT[0], minute=BACKUP_AT[1], second=0, microsecond=0)
        if now < slot:
            slot -= timedelta(days=1)
        last_ok = self.state.get("last_ok_ts")
        if last_ok is not None and datetime.fromtimestamp(last_ok) >= slot:
            return slot + timedelta(days=1)
        last_try = (self.state.get("last_backup") or {}).get("ts")
        if last_try and not (self.state.get("last_backup") or {}).get("ok"):
            return max(slot, datetime.fromtimestamp(last_try) + timedelta(hours=1))
        return slot

    # -- background loop
    def start(self):
        threading.Thread(target=self._loop, daemon=True, name="storage").start()

    def _loop(self):
        time.sleep(120)  # let startup (and the radio's boot burst) settle first
        last_sig = None
        while True:
            try:
                if datetime.now() >= self._next_due():
                    self.backup()
                self._check_warnings()
                st = self.status()
                sig = (st["dbBytes"] // (1024 * 1024), st["overLimit"], st["lastOkTs"], st["backupStale"])
                if sig != last_sig:  # push only when something visible changed (MB granularity)
                    self.on_change(st)
                    last_sig = sig
            except Exception:  # noqa: BLE001 - this loop must never die
                log.exception("storage loop error")
            time.sleep(300)

    def _check_warnings(self):
        now = time.time()
        st = self.status()
        if st["overLimit"]:
            last = self.state.get("size_notified_ts", 0)
            if not self.state.get("size_notified_over") or now - last > 7 * 86400:
                gb = st["dbBytes"] / 1024**3
                notify("Lorakeet: database is over 1 GB",
                       f"mesh.db is {gb:.2f} GB on D:. Nothing is deleted and logging continues. "
                       "Open the dashboard (127.0.0.1:5190) for options.")
                self.on_event("size_warning", bytes=st["dbBytes"])
                self.state.update(size_notified_over=True, size_notified_ts=now)
                self._save_state()
        elif self.state.get("size_notified_over"):
            self.state["size_notified_over"] = False  # dropped back under; warn again if it re-crosses
            self._save_state()
        if st["backupStale"] and self.state.get("last_ok_ts"):  # only after the first success ever
            if now - self.state.get("backup_notified_ts", 0) > 86400:
                notify("Lorakeet: backups are failing",
                       "No successful backup of the Meshtastic log in over 48 hours. "
                       "Check the dashboard (127.0.0.1:5190) for the error.")
                self.state["backup_notified_ts"] = now
                self._save_state()

    # -- the backup itself
    def backup(self):
        if not self.lock.acquire(blocking=False):
            return self.state.get("last_backup")
        self.running = True
        self.on_change(self.status())
        started = time.time()
        result = {"ts": started, "dests": [], "ok": False}
        try:
            self.tmp_dir.mkdir(parents=True, exist_ok=True)
            name = datetime.now().strftime(NAME_FMT)
            snap = self.tmp_dir / name.removesuffix(".gz")
            gz = self.tmp_dir / name
            for p in (snap, gz):
                p.unlink(missing_ok=True)

            src = sqlite3.connect(self.db_path, timeout=60)
            dst = sqlite3.connect(snap)
            try:
                src.backup(dst)  # consistent point-in-time copy while the server keeps logging
            finally:
                src.close()
            check = dst.execute("PRAGMA integrity_check").fetchone()[0]
            counts = {t: dst.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                      for t in ("packets", "telemetry_full", "positions", "messages", "node_info")}
            dst.close()
            if check != "ok":
                raise RuntimeError(f"integrity check failed on snapshot: {check}")
            with open(snap, "rb") as fi, gzip.open(gz, "wb", compresslevel=6) as fo:
                shutil.copyfileobj(fi, fo, 1024 * 1024)
            result.update(dbBytes=snap.stat().st_size, gzBytes=gz.stat().st_size, rows=counts, name=name)
            snap.unlink(missing_ok=True)

            for dest in self.dests:
                entry = {"path": str(dest)}
                try:
                    dest.mkdir(parents=True, exist_ok=True)
                    part = dest / (name + ".partial")
                    shutil.copyfile(gz, part)
                    if part.stat().st_size != gz.stat().st_size:
                        raise OSError("copied size mismatch")
                    os.replace(part, dest / name)
                    entry["ok"] = True
                    entry["pruned"] = self._prune(dest)
                except Exception as e:  # noqa: BLE001 - one destination failing mustn't stop the other
                    entry.update(ok=False, error=str(e))
                    log.warning("backup to %s failed: %s", dest, e)
                result["dests"].append(entry)
            gz.unlink(missing_ok=True)
            result["ok"] = all(d.get("ok") for d in result["dests"])
            result["partial"] = not result["ok"] and any(d.get("ok") for d in result["dests"])
        except Exception as e:  # noqa: BLE001
            result["error"] = str(e)
            log.exception("backup failed")
        finally:
            result["seconds"] = round(time.time() - started, 1)
            self.state["last_backup"] = result
            if result["ok"]:
                self.state["last_ok_ts"] = started
            self._save_state()
            self.running = False
            self.lock.release()
            log.info("backup %s in %.1f s: %s", "ok" if result["ok"] else "FAILED", result["seconds"],
                     ", ".join(f"{d['path']}={'ok' if d.get('ok') else d.get('error')}" for d in result["dests"])
                     or result.get("error"))
            self.on_event("backup", **{k: v for k, v in result.items() if k != "rows"}, rows=result.get("rows"))
            self.on_change(self.status())
        return result

    @staticmethod
    def _prune(dest):
        files = {}
        for p in dest.glob("mesh-*.db.gz"):
            try:
                files[datetime.strptime(p.name, NAME_FMT).date()] = p
            except ValueError:
                continue  # not ours; leave it alone
        keep = keep_dates(date.today(), set(files))
        removed = []
        for d, p in files.items():
            if d not in keep:
                p.unlink(missing_ok=True)
                removed.append(p.name)
        for p in dest.glob("*.partial"):  # leftovers from an interrupted copy
            if time.time() - p.stat().st_mtime > 3600:
                p.unlink(missing_ok=True)
        return removed
