"""Make a Lorakeet release in two steps, stopping at the first thing that's wrong.

    python tools/release.py prepare 0.5.0    # checks, version, changelog date, then the release pull request
    python tools/release.py finish 0.5.0     # once that pull request has merged: tag, release, download, next version

`prepare` runs on `dev` (clean, up to date with main, changelog "Unreleased" not empty): every test (the browser tests
too), the lint, the personal-details check and the API doc check; then sets VERSION, dates the changelog, commits and
pushes dev to the private remote; then squashes dev onto main in a worktree as one "Lorakeet x.y.z" commit, pushes
`release/x.y.z` to origin and opens its pull request with auto-merge on. `main` is protected: the pull request merges
itself once all of CI passes, and nothing reaches main if anything fails.

`finish` tags the merge commit, publishes the GitHub release (regular, marked Latest; notes from `--notes FILE` or
built from the changelog), waits for the release workflow to attach Lorakeet-x.y.z.zip and SHA256SUMS.txt and checks
the zip against the checksum, then merges main into dev and starts the next version (`--next`, default the next patch
as -dev). Add `--dry-run` to either to see the steps without running them.

Run it with the developer environment's Python (the one with pytest, Playwright and ruff).
"""
import argparse
import datetime
import hashlib
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PUBLIC, BACKUP = "origin", "private"
REPO = "darthnut/lorakeet"
DRY = False


class Stop(Exception):
    """Something isn't right: say what, and stop before anything else happens."""


def run(*cmd, cwd=ROOT, capture=True, changes=True, input=None):
    """Run a command; with --dry-run, only print the ones that change something."""
    shown = " ".join(str(c) for c in cmd)
    if DRY and changes:
        print(f"  would run: {shown}")
        return ""
    r = subprocess.run([str(c) for c in cmd], cwd=cwd, capture_output=capture, text=True, input=input)
    if r.returncode:
        detail = ((r.stdout or "") + (r.stderr or "")).strip()[-2000:] if capture else ""
        raise Stop(f"failed: {shown}\n{detail}")
    return (r.stdout or "").strip() if capture else ""


def git(*a, cwd=ROOT, changes=False):
    return run("git", *a, cwd=cwd, changes=changes)


def step(text):
    print(f"\n== {text}")


# -- the changelog (pure: tested in tests/test_release.py)
def section(log, heading):
    """The text under '## <heading>' up to the next '## ', or None."""
    m = re.search(rf"^## {re.escape(heading)}[^\n]*\n(.*?)(?=^## |\Z)", log, re.S | re.M)
    return m.group(1).strip() if m else None


def date_release(log, version, day):
    """'## Unreleased' -> '## x.y.z (day)'."""
    if log.count("## Unreleased") != 1:
        raise Stop("CHANGELOG.md needs exactly one '## Unreleased' heading")
    return log.replace("## Unreleased", f"## {version} ({day})", 1)


def start_next(log, version):
    """A fresh '## Unreleased' above the release just made."""
    return re.sub(rf"^## {re.escape(version)} \(", f"## Unreleased\n\n## {version} (", log, count=1, flags=re.M)


def unwrap(md):
    """GitHub release pages show hard line wraps as they are: one line per paragraph and per bullet. A line starts
    a new one when it begins at the margin with a bullet or a number, or is a bold heading on its own; anything else
    (indented, or plain text under a paragraph) continues the line above."""
    out = []
    for line in md.split("\n"):
        text = line.strip()
        new = (not text or line.startswith(("- ", "* ")) or re.match(r"\d+\. ", line)
               or (line.startswith("**") and line.rstrip().endswith("**")))
        if new or not out or not out[-1]:
            out.append(text)
        else:
            out[-1] += " " + text
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip() + "\n"


def summary(body):
    """The commit message's bullet list: each changelog bullet's bold title."""
    titles = re.findall(r"^- \*\*(.+?)\*\*", body, re.M)
    return "\n".join(f"- {t.rstrip('.:')}" for t in titles)


# a release (0.5.0) or a test build of one (0.5.0-beta.1, 0.5.0-rc.1): a test build is a GitHub pre-release, never
# "latest", so the README's download link keeps pointing at the last real release
VERSION_RE = r"\d+\.\d+\.\d+(-(beta|rc)\.\d+)?"


def is_test_build(version):
    return "-" in version


def next_dev(version):
    """The version dev moves to: the next patch after a release; after a test build, that same release (-dev)."""
    if is_test_build(version):
        return version.split("-", 1)[0] + "-dev"
    a, b, c = (int(x) for x in version.split("."))
    return f"{a}.{b}.{c + 1}-dev"


def check_version(v):
    if not re.fullmatch(VERSION_RE, v):
        raise Stop(f"{v!r} isn't a version like 0.5.0 (or a test build like 0.5.0-beta.1)")


# -- the two steps
def prepare(version):
    log_path, ver_path = ROOT / "CHANGELOG.md", ROOT / "VERSION"
    step("dev is ready")
    if git("branch", "--show-current") != "dev":
        raise Stop("switch to the dev branch first")
    if git("status", "--porcelain"):
        raise Stop("dev has uncommitted changes: commit or stash them first")
    if not ver_path.read_text().strip().endswith("-dev"):
        raise Stop(f"VERSION is {ver_path.read_text().strip()}: expected a -dev version")
    run("git", "fetch", "-q", PUBLIC, changes=False)
    try:
        git("merge-base", "--is-ancestor", f"{PUBLIC}/main", "dev")
    except Stop:
        raise Stop(f"main has commits dev doesn't: git merge {PUBLIC}/main first") from None
    if run("git", "ls-remote", "--tags", PUBLIC, f"v{version}", changes=False):
        raise Stop(f"v{version} already exists on {PUBLIC}")
    body = section(log_path.read_text(encoding="utf-8"), "Unreleased")
    if not body:
        raise Stop("CHANGELOG.md has nothing under '## Unreleased'")
    print(f"releasing {version}:\n{summary(body)}")

    step("every check")
    py = sys.executable
    run(py, "-m", "pytest", "tests", "-q", "-p", "no:cacheprovider", changes=False, capture=False)
    ruff = shutil.which("ruff", path=str(Path(py).parent)) or shutil.which("ruff") or "ruff"
    run(ruff, "check", ".", changes=False)
    run(py, "tools/release_check.py", changes=False)
    run(py, "tools/api_docs.py", "--check", changes=False)
    run(py, "tools/build_release.py", "--out", tempfile.mkdtemp(prefix="lorakeet-zip-"), changes=False)
    print("all passed")

    step(f"VERSION {version} and the changelog date, on dev")
    if not DRY:
        newline = "\r\n" if b"\r\n" in ver_path.read_bytes() else "\n"
        ver_path.write_text(version + "\n", encoding="utf-8", newline=newline)
        raw = log_path.read_bytes()
        log = date_release(raw.decode("utf-8"), version, datetime.date.today().isoformat())
        log_path.write_bytes(log.encode("utf-8"))
    git("commit", "-q", "-am", f"Lorakeet {version}", changes=True)
    git("push", "-q", BACKUP, "dev", changes=True)

    step(f"release/{version}: dev squashed onto main, pull request with auto-merge")
    branch = f"release/{version}"
    wt = Path(tempfile.mkdtemp(prefix="lorakeet-release-")) / "tree"   # outside the checkout (and any synced folder)
    git("worktree", "add", "-q", "-b", branch, wt, f"{PUBLIC}/main", changes=True)
    try:
        git("merge", "--squash", "dev", cwd=wt, changes=True)
        run("git", "commit", "-q", "-F", "-", cwd=wt, input=f"Lorakeet {version}\n\n{summary(body)}\n")
        if not DRY:
            run(sys.executable, "tools/release_check.py", cwd=wt, changes=False)
        git("push", "-q", "-u", PUBLIC, branch, cwd=wt, changes=True)
    finally:
        if not DRY and wt.exists():
            git("worktree", "remove", "--force", wt, changes=True)
    pr_body = f"Release {version}. Full notes in CHANGELOG.md.\n\n{unwrap(body)}"
    url = run("gh", "pr", "create", "-R", REPO, "--base", "main", "--head", branch, "--title", f"Lorakeet {version}",
              "--body", pr_body)
    run("gh", "pr", "merge", url or branch, "-R", REPO, "--auto", "--squash")
    print(f"\n{url or '(the pull request)'}\nIt merges itself once CI passes. Then: python tools/release.py finish {version}")


def finish(version, notes=None, nxt=None):
    branch, tag = f"release/{version}", f"v{version}"
    step("the pull request merged")
    state = run("gh", "pr", "view", branch, "-R", REPO, "--json", "state", "-q", ".state", changes=False)
    if state != "MERGED":
        raise Stop(f"the {branch} pull request is {state or 'not found'}: it has to merge first")
    run("git", "fetch", "-q", "--prune", PUBLIC, changes=False)
    v = git("show", f"{PUBLIC}/main:VERSION").strip()
    if v != version:
        raise Stop(f"main's VERSION is {v}, not {version}")

    step(f"tag {tag} and the release")
    if not run("git", "ls-remote", "--tags", PUBLIC, tag, changes=False):
        git("tag", "-a", tag, "-m", f"Lorakeet {version}", f"{PUBLIC}/main", changes=True)
        git("push", "-q", PUBLIC, tag, changes=True)
    if notes:
        text = Path(notes).read_text(encoding="utf-8")
        text = text.split("\n---\n", 1)[1] if text.startswith("Title:") else text   # private/release-x.y.z.md form
    else:
        text = unwrap(section(git("show", f"{PUBLIC}/main:CHANGELOG.md"), version) or "")
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as f:
        f.write(text.strip() + "\n")
    exists = subprocess.run(["gh", "release", "view", tag, "-R", REPO], capture_output=True).returncode == 0
    if exists:
        print(f"{tag} is already released: leaving it as it is")   # finish can be run again after a hiccup
    else:
        kind = ["--title", f"{tag} test build", "--prerelease"] if is_test_build(version) else \
               ["--title", f"{tag} preview", "--latest"]
        run("gh", "release", "create", tag, "-R", REPO, *kind, "--notes-file", f.name)

    step("the download (the release workflow attaches it)")
    if not DRY:
        verify_download(tag, version)

    step("dev: main merged in, the next version started")
    after = nxt or next_dev(version)
    git("merge", "-q", f"{PUBLIC}/main", "-m", f"Merge main ({tag}) into dev", changes=True)
    if not DRY:
        ver = ROOT / "VERSION"
        newline = "\r\n" if b"\r\n" in ver.read_bytes() else "\n"
        ver.write_text(after + "\n", encoding="utf-8", newline=newline)
        log = ROOT / "CHANGELOG.md"
        log.write_bytes(start_next(log.read_bytes().decode("utf-8"), version).encode("utf-8"))
    git("commit", "-q", "-am", f"Start {after.removesuffix('-dev')}", changes=True)
    git("push", "-q", BACKUP, "dev", changes=True)
    run("git", "branch", "-D", branch, changes=True)   # GitHub deleted its copy on merge
    print(f"\n{tag} is out: https://github.com/{REPO}/releases/tag/{tag}\ndev is now {after}")


def verify_download(tag, version, wait_s=600):
    names = {f"Lorakeet-{version}.zip", "SHA256SUMS.txt"}
    deadline = time.time() + wait_s
    while True:
        have = set(run("gh", "release", "view", tag, "-R", REPO, "--json", "assets", "-q", ".assets[].name",
                       changes=False).split())
        if names <= have:
            break
        if time.time() > deadline:
            raise Stop(f"the release workflow hasn't attached {', '.join(sorted(names - have))} after {wait_s // 60} "
                       f"min: see the Actions tab")
        time.sleep(15)
    d = Path(tempfile.mkdtemp(prefix="lorakeet-release-"))
    run("gh", "release", "download", tag, "-R", REPO, "-D", d, changes=False)
    want = (d / "SHA256SUMS.txt").read_text().split()[0]
    got = hashlib.sha256((d / f"Lorakeet-{version}.zip").read_bytes()).hexdigest()
    if want != got:
        raise Stop(f"Lorakeet-{version}.zip doesn't match SHA256SUMS.txt")
    print(f"Lorakeet-{version}.zip attached, checksum matches")


def main(argv=None):
    global DRY
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("action", choices=["prepare", "finish"])
    ap.add_argument("version", help="e.g. 0.5.0")
    ap.add_argument("--notes", help="finish: release notes file (default: built from the changelog)")
    ap.add_argument("--next", help="finish: the version dev moves to (default: the next patch, as -dev)")
    ap.add_argument("--dry-run", action="store_true", help="show the steps without making any change")
    a = ap.parse_args(argv)
    DRY = a.dry_run
    try:
        check_version(a.version)
        if a.action == "prepare":
            prepare(a.version)
        else:
            finish(a.version, a.notes, a.next)
    except Stop as e:
        sys.exit(f"\nSTOPPED: {e}")


if __name__ == "__main__":
    main()
