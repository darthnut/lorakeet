"""Test setup: never read a developer's own lorakeet.toml or touch their data folder.

The modules read their configuration when imported, so this runs first: LORAKEET_CONFIG points at a file
that doesn't exist (pure defaults) and the per-OS data folder at a temp directory.
"""
import os
import pathlib
import sys
import tempfile

_TMP = pathlib.Path(tempfile.mkdtemp(prefix="lktests-"))
os.environ["LORAKEET_CONFIG"] = str(_TMP / "no-config-here.toml")
if os.name == "nt" and os.environ.get("LOCALAPPDATA"):  # Playwright's browsers live there (tests/browser)
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(pathlib.Path(os.environ["LOCALAPPDATA"]) / "ms-playwright"))
os.environ["LOCALAPPDATA"] = str(_TMP / "appdata")
os.environ["XDG_DATA_HOME"] = str(_TMP / "xdg")
sys.argv = sys.argv[:1]  # server.py parses argv only in main(), but keep pytest's flags away from it anyway

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

try:  # the browser tests need Playwright (requirements-dev.txt + `python -m playwright install chromium`)
    import playwright  # noqa: F401
except ImportError:
    collect_ignore = ["browser"]

import pytest  # noqa: E402 - after the environment above, like every import that reads the configuration


@pytest.fixture(autouse=True)
def _commit_every_write(monkeypatch):
    """Most tests write through a Store and read the file back through another connection, as analytics does, so
    here each write commits at once, as before batching. tests/test_store_batching.py tests the batching itself."""
    import store
    monkeypatch.setattr(store, "COMMIT_MAX_ROWS", 1)
