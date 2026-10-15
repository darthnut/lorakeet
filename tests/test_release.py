"""tools/release.py's changelog handling: the steps that touch git and GitHub are its own checks; these are the text
it writes (VERSION, CHANGELOG.md, the commit message and the release page)."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import release

LOG = """# Changelog

Intro.

## Unreleased

**New**
- **A proper download.** Each release now has a zip: one folder with only what's
  needed to run it, and a **checksum** to check the download.
- **A USB drive** for a station's data.

**Fixed**
- **Two identical radios.** Lorakeet remembers its radio,
  so they can go in any ports.

A paragraph that wraps
over two lines.

1. A numbered step that
   wraps.
2. Another.

## 0.4.0 (2026-10-13)

**New**
- **Old stuff.**
"""


def test_section_and_dating():
    body = release.section(LOG, "Unreleased")
    assert body.startswith("**New**") and body.endswith("2. Another.") and "Old stuff" not in body
    assert release.section(LOG, "0.4.0") == "**New**\n- **Old stuff.**"
    assert release.section(LOG, "9.9.9") is None
    dated = release.date_release(LOG, "0.5.0", "2026-11-01")
    assert "## 0.5.0 (2026-11-01)" in dated and "## Unreleased" not in dated
    again = release.start_next(dated, "0.5.0")
    assert "## Unreleased\n\n## 0.5.0 (2026-11-01)" in again and again.count("## Unreleased") == 1
    with pytest.raises(release.Stop):
        release.date_release(dated, "0.5.1", "2026-11-02")       # no Unreleased heading to date


def test_unwrap_gives_one_line_per_paragraph_and_bullet():
    out = release.unwrap(release.section(LOG, "Unreleased")).split("\n")
    assert "- **A proper download.** Each release now has a zip: one folder with only what's needed to run it, " \
           "and a **checksum** to check the download." in out
    assert "- **Two identical radios.** Lorakeet remembers its radio, so they can go in any ports." in out
    assert "A paragraph that wraps over two lines." in out
    assert "1. A numbered step that wraps." in out and "2. Another." in out
    assert "**New**" in out and "**Fixed**" in out


def test_the_real_changelog_unwraps_cleanly():
    log = (Path(release.ROOT) / "CHANGELOG.md").read_text(encoding="utf-8")
    out = release.unwrap(release.section(log, "0.4.0"))
    assert all(not line.startswith(" ") for line in out.split("\n"))           # no continuation left behind
    assert sum(line.startswith("- **") for line in out.split("\n")) >= 10


def test_summary_versions():
    assert release.summary(release.section(LOG, "Unreleased")) == \
        "- A proper download\n- A USB drive\n- Two identical radios"
    assert release.next_dev("0.4.1") == "0.4.2-dev"
    assert release.next_dev("0.5.0-beta.1") == "0.5.0-dev"     # a test build: dev carries on towards 0.5.0
    for good in ("0.5.0", "0.5.0-beta.1", "0.5.0-rc.2"):
        release.check_version(good)
    assert release.is_test_build("0.5.0-beta.1") and not release.is_test_build("0.5.0")
    for bad in ("v0.5.0", "0.5", "0.5.0-dev", "0.5.0-beta", "0.5.0-alpha.1"):
        with pytest.raises(release.Stop):
            release.check_version(bad)
