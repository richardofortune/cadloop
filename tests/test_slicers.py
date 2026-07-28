import os
import stat
from pathlib import Path

from cadloop import slicers


def _fake_binary(tmp_path: Path, name: str, body: str) -> str:
    p = tmp_path / name
    p.write_text("#!/bin/sh\n" + body)
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return str(p)


def test_family_of_recognises_the_orca_family():
    assert slicers.family_of("/x/OrcaSlicer") == "orca"
    assert slicers.family_of("/x/BambuStudio") == "bambu"
    assert slicers.family_of("/x/CrealityPrint") == "creality"
    assert slicers.family_of("/x/somethingelse") == "unknown"


def test_probe_accepts_a_binary_that_prints_flags(tmp_path):
    b = _fake_binary(tmp_path, "good", 'echo "OrcaSlicer 2.4.2"; echo " --slice"; echo " --export-3mf"')
    r = slicers.probe(b)
    assert r["ok"] is True
    assert "--slice" in r["flags"]
    assert "2.4.2" in (r["version"] or "")


def test_probe_rejects_a_binary_that_dies(tmp_path):
    b = _fake_binary(tmp_path, "bad", "kill -SEGV $$")
    r = slicers.probe(b)
    assert r["ok"] is False
    assert r["reason"]


def test_probe_rejects_a_binary_missing_the_flags_we_need(tmp_path):
    b = _fake_binary(tmp_path, "thin", 'echo "Some Slicer 1.0"; echo " --help"')
    r = slicers.probe(b)
    assert r["ok"] is False
    assert "--slice" in r["reason"]


def test_installed_finds_a_candidate_that_exists(tmp_path, monkeypatch):
    b = _fake_binary(tmp_path, "OrcaSlicer", 'echo " --slice"')
    monkeypatch.setattr(slicers, "SLICER_CANDIDATES", [b])
    found = slicers.installed()
    assert [f["family"] for f in found] == ["orca"]
