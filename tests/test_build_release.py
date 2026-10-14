"""The release download (tools/build_release.py): only what's needed to run Lorakeet, in one folder, with the line
endings each platform's scripts need."""
import io
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import build_release


def make_zip(tmp_path, files):
    p = tmp_path / "t.zip"
    with zipfile.ZipFile(p, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)
    return p


def good_files(prefix="Lorakeet-1.0.0/"):
    files = {prefix + m: b"x\n" for m in build_release.MUST_HAVE}
    files[prefix + "Install Lorakeet.cmd"] = b"@echo off\r\nrem hi\r\n"
    files[prefix + "Uninstall Lorakeet.cmd"] = b"@echo off\r\n"
    files[prefix + "install.ps1"] = b"Write-Host hi\r\n"
    return files


def test_a_good_zip_passes(tmp_path):
    assert build_release.check(make_zip(tmp_path, good_files()), "Lorakeet-1.0.0/") == []


def test_what_a_zip_mustnt_be(tmp_path):
    files = good_files()
    del files["Lorakeet-1.0.0/server.py"]
    files["Lorakeet-1.0.0/tests/test_x.py"] = b"x"
    files["Lorakeet-1.0.0/CLAUDE.md"] = b"x"
    files["Lorakeet-1.0.0/install.sh"] = b"#!/bin/sh\r\necho hi\r\n"      # CRLF breaks bash
    files["Lorakeet-1.0.0/install.ps1"] = b"Write-Host hi\n"            # LF in a Windows script
    files["stray.txt"] = b"x"                                            # outside the folder
    problems = "\n".join(build_release.check(make_zip(tmp_path, files), "Lorakeet-1.0.0/"))
    for want in ("missing server.py", "shouldn't contain tests/", "shouldn't contain CLAUDE.md",
                 "install.sh needs Unix (LF)", "install.ps1 needs Windows (CRLF)", "everything should be inside"):
        assert want in problems


def test_this_checkout_builds(tmp_path):
    if subprocess.run(["git", "-C", str(build_release.ROOT), "rev-parse", "HEAD"], capture_output=True).returncode:
        pytest.skip("not a git checkout")
    build_release.main(["--out", str(tmp_path)])
    version = build_release.git("show", "HEAD:VERSION").strip()   # what's committed, as the build reads it
    z = tmp_path / f"Lorakeet-{version}.zip"
    sums = (tmp_path / "SHA256SUMS.txt").read_text()
    assert z.exists() and sums.endswith(f"  {z.name}\n") and len(sums.split()[0]) == 64
    with zipfile.ZipFile(io.BytesIO(z.read_bytes())) as zf:
        assert f"Lorakeet-{version}/server.py" in zf.namelist()


def test_a_tag_must_match_its_version(tmp_path, monkeypatch):
    monkeypatch.setattr(build_release, "git", lambda *a, **k: "0.9.9" if a[0] == "show" else b"")
    with pytest.raises(SystemExit, match="should match"):
        build_release.main(["--ref", "v1.0.0", "--out", str(tmp_path)])
