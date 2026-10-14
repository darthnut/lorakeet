"""Browser tests: real pages in headless Chromium (Playwright) against a real Lorakeet.

Two servers, each a separate process with its own temp lorakeet.toml and data folder (never the developer's):
`demo_url` is `server.py --demo` (the made-up mesh: map, analytics, visualizations, packets); `local_url` is a
configured install with no radio (a port that doesn't exist), for the Settings, Radio and Stations pages, with the
demo database as its log. Left out (tests/conftest.py) when Playwright isn't installed:

    pip install -r requirements-dev.txt && python -m playwright install chromium
"""
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _start(tmp, demo):
    port = _free_port()
    data = tmp / "data"
    data.mkdir(parents=True, exist_ok=True)
    cfg = tmp / "lorakeet.toml"
    cfg.write_text(f'# browser test\n[radio]\nport = "{"COM250" if os.name == "nt" else "/dev/lorakeet-none"}"\n\n'
                   f'[storage]\ndata_dir = "{data.as_posix()}"\n\n[http]\nport = {port}\n', encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k not in ("LORAKEET_SUPERVISED", "INVOCATION_ID")}
    env["LORAKEET_CONFIG"] = str(cfg)
    if not demo:  # the made-up mesh as this install's log, so the pages have data
        subprocess.run([sys.executable, "-c", f"import demo; demo.build(r'{data / 'mesh.db'}')"], cwd=ROOT, env=env,
                       check=True)
    args = [sys.executable, "-u", "server.py", "--http", str(port)] + (["--demo"] if demo else [])
    proc = subprocess.Popen(args, cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    url = f"http://127.0.0.1:{port}"
    for _ in range(240):
        try:
            urllib.request.urlopen(url + "/api/version", timeout=2)
            return proc, url, cfg
        except OSError:
            if proc.poll() is not None:
                raise RuntimeError(f"Lorakeet exited with {proc.returncode}") from None
            time.sleep(0.5)
    proc.kill()
    raise RuntimeError("Lorakeet didn't start")


@pytest.fixture(scope="session")
def demo_url(tmp_path_factory):
    proc, url, _ = _start(tmp_path_factory.mktemp("demo"), demo=True)
    yield url
    proc.kill()


@pytest.fixture(scope="session")
def local(tmp_path_factory):
    proc, url, cfg = _start(tmp_path_factory.mktemp("local"), demo=False)
    yield url, cfg
    proc.kill()


BLANK_PNG = bytes.fromhex("89504e470d0a1a0a0000000d4948445200000001000000010806000000"
                          "1f15c4890000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082")


def _offline_tiles(route):
    """Lorakeet itself and the page libraries (cdnjs) load for real; map tiles get a blank square, so the tests
    don't depend on (or load) OpenStreetMap's or Esri's servers."""
    host = route.request.url.split("/")[2]
    if host.startswith("127.0.0.1") or host == "cdnjs.cloudflare.com":
        route.continue_()
    else:
        route.fulfill(status=200, content_type="image/png", body=BLANK_PNG)


@pytest.fixture(scope="session")
def browser():
    with sync_playwright() as p:
        b = p.chromium.launch()
        yield b
        b.close()


@pytest.fixture
def page(browser):
    """A fresh page that records every script error, console error and failed (5xx) request; the tests assert
    there were none with `clean(page)`."""
    ctx = browser.new_context(viewport={"width": 1366, "height": 900})
    ctx.route("**/*", _offline_tiles)
    pg = ctx.new_page()
    pg.problems = []
    pg.on("pageerror", lambda e: pg.problems.append(f"script error: {e}"))
    pg.on("console", lambda m: m.type == "error" and pg.problems.append(f"console: {m.text}"))
    pg.on("response", lambda r: r.status >= 500 and pg.problems.append(f"{r.status} {r.url}"))
    yield pg
    ctx.close()
