"""Block a public release that still contains personal details.

    python tools/release_check.py [files...]      # default: every file git tracks

Flags private and Tailscale IP addresses, real-looking node ids (!xxxxxxxx other than the broadcast id and
documented placeholders), MAC addresses, email addresses, Windows user and drive paths, precise
coordinate pairs, amateur radio callsigns, and every term in .release-denylist (one per line, case-insensitive;
the file is git-ignored, so the personal words it lists are never published). Exits 1 if anything is found.

To accept a line on purpose, put `release-ok` in a comment on it.
"""
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
DENYLIST = ROOT / ".release-denylist"

PLACEHOLDER_IDS = {"!ffffffff", "!1234abcd", "!5678ef90", "!abcd1234", "!deadbeef"}
EXAMPLE_COORDS = {"47.60620", "-122.33210", "47.6062", "-122.3321"}  # Seattle, the documented example
CHECKS = [
    ("private IP", re.compile(r"\b(?:10\.\d{1,3}|192\.168|172\.(?:1[6-9]|2\d|3[01]))\.\d{1,3}\.\d{1,3}\b")),
    ("Tailscale IP", re.compile(r"\b100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d{1,3}\.\d{1,3}\b(?!/)")),
    ("node id", re.compile(r"![0-9a-f]{8}\b")),
    ("MAC address", re.compile(r"\b(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}\b")),
    ("email", re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")),
    ("user path", re.compile(r"[A-Za-z]:\\\\?Users\\\\?\w+|/home/(?!lorakeet\b)\w+|/Users/\w+")),
    ("drive path", re.compile(r"\b[D-Z]:\\\\?\w")),
    ("coordinates", re.compile(r"-?\b\d{1,3}\.\d{4,}\s*,\s*-?\d{1,3}\.\d{4,}\b")),
    ("callsign", re.compile(r"\b[AKNW][A-Z]?\d[A-Z]{2,3}\b")),
]
ALLOWED_EMAIL = re.compile(r"noreply@|@users\.noreply\.github\.com|@example\.(?:com|org)|^lorakeet@")  # lorakeet@host: an ssh login
SKIP_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".mp4", ".ico", ".woff", ".woff2"}


def files(argv):
    if argv:
        return [pathlib.Path(a) for a in argv]
    out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True).stdout
    return [ROOT / f for f in out.splitlines() if f]


def main(argv):
    terms = []
    if DENYLIST.exists():
        terms = [t.strip() for t in DENYLIST.read_text(encoding="utf-8").splitlines() if t.strip() and not t.startswith("#")]
    deny = re.compile("|".join(re.escape(t) for t in terms), re.I) if terms else None
    hits = 0
    for f in files(argv):
        if f.suffix.lower() in SKIP_SUFFIXES or not f.is_file():
            continue
        try:
            lines = f.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError:
            continue
        rel = f.relative_to(ROOT) if f.is_relative_to(ROOT) else f
        for n, line in enumerate(lines, 1):
            if "release-ok" in line:
                continue
            found = []
            for label, rx in CHECKS:
                for m in rx.finditer(line):
                    v = m.group(0)
                    if label == "node id" and v in PLACEHOLDER_IDS:
                        continue
                    if label == "email" and ALLOWED_EMAIL.search(v):
                        continue
                    if label == "coordinates" and any(c in v for c in EXAMPLE_COORDS):
                        continue
                    found.append(f"{label} {v!r}")
            if deny:
                found += [f"denylisted {m.group(0)!r}" for m in deny.finditer(line)]
            if found:
                hits += 1
                print(f"{rel}:{n}: {', '.join(found)}")
    print(f"\n{hits} line(s) need attention" if hits else "clean: nothing personal found")
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
