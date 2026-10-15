"""Build the release download: Lorakeet-<version>.zip (one Lorakeet-<version> folder inside, only what's needed to run
Lorakeet) and SHA256SUMS.txt, from a tag or any commit.

    python tools/build_release.py                 # HEAD, into dist/
    python tools/build_release.py --ref v0.4.1    # a tag: its VERSION must match it

What's left out (tests, CI, developer notes and tools) is marked `export-ignore` in .gitattributes, which GitHub's own
"Source code" downloads honour too. The current .gitattributes is applied whatever the ref, so an older tag builds the
same way. Line endings come from .gitattributes as well: the zip is checked for CRLF Windows scripts and LF shell
scripts, because a zip built on Linux would otherwise carry whatever the repository stores.
"""
import argparse
import hashlib
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MUST_HAVE = ["README.md", "LICENSE", "CHANGELOG.md", "VERSION", "requirements.txt", "server.py", "install.sh",
             "install.ps1", "Install Lorakeet.cmd", "Uninstall Lorakeet.cmd", "lorakeet.pyw", "supervise.pyw",
             "lorakeet.example.toml", "static/index.html", "deploy/setup-station.sh", "docs/PI-SETUP.md",
             "requirements-meshcore.txt", "docs/MESHCORE-GUIDE.md"]
MUST_NOT = ["tests/", ".github/", "CLAUDE.md", "requirements-dev.txt", "tools/release_check.py", "tools/build_release.py"]
CRLF = (".cmd", ".ps1")
LF = (".sh", ".service", ".timer", ".conf")


def git(*args, binary=False):
    r = subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, check=True)
    return r.stdout if binary else r.stdout.decode("utf-8").strip()


def check(zip_path, prefix):
    """Every problem with a built zip, as text (empty = fine)."""
    problems = []
    with zipfile.ZipFile(zip_path) as z:
        names = z.namelist()
        if not names or any(not n.startswith(prefix) for n in names):
            problems.append(f"everything should be inside {prefix}")
        rel = {n[len(prefix):] for n in names}
        problems += [f"missing {m}" for m in MUST_HAVE if m not in rel]
        problems += [f"shouldn't contain {m}" for m in MUST_NOT if any(r == m or r.startswith(m) for r in rel)]
        for n in names:
            if n.endswith(CRLF) or n.endswith(LF):
                data = z.read(n)
                lf_only = data.count(b"\n") - data.count(b"\r\n")
                if n.endswith(CRLF) and lf_only:
                    problems.append(f"{n[len(prefix):]} needs Windows (CRLF) line endings")
                if n.endswith(LF) and b"\r\n" in data:
                    problems.append(f"{n[len(prefix):]} needs Unix (LF) line endings")
    return problems


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--ref", default="HEAD", help="tag or commit (default HEAD)")
    ap.add_argument("--out", default=str(ROOT / "dist"), help="output folder (default dist/)")
    a = ap.parse_args(argv)
    version = git("show", f"{a.ref}:VERSION").strip()
    if a.ref.startswith("v") and a.ref[1:] != version:
        sys.exit(f"{a.ref} has VERSION {version}: they should match")
    name = f"Lorakeet-{version}"
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    zip_path = out / f"{name}.zip"
    # --worktree-attributes: this checkout's .gitattributes (export-ignore, line endings) for any ref
    zip_path.write_bytes(git("archive", "--worktree-attributes", "--format=zip", f"--prefix={name}/", a.ref, binary=True))
    problems = check(zip_path, f"{name}/")
    if problems:
        sys.exit(f"{zip_path.name} isn't right:\n  " + "\n  ".join(problems))
    digest = hashlib.sha256(zip_path.read_bytes()).hexdigest()
    (out / "SHA256SUMS.txt").write_text(f"{digest}  {zip_path.name}\n", encoding="utf-8", newline="\n")
    with zipfile.ZipFile(zip_path) as z:
        n = len(z.namelist())
    print(f"{zip_path} ({zip_path.stat().st_size // 1024} KB, {n} entries)\nsha256 {digest}")


if __name__ == "__main__":
    main()
