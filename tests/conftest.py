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
os.environ["LOCALAPPDATA"] = str(_TMP / "appdata")
os.environ["XDG_DATA_HOME"] = str(_TMP / "xdg")
sys.argv = sys.argv[:1]  # server.py parses argv only in main(), but keep pytest's flags away from it anyway

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
