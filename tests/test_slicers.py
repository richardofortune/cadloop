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


import json


def test_adopt_reads_a_creality_style_config(tmp_path, monkeypatch):
    conf = tmp_path / "Creality.conf"
    conf.write_text(json.dumps({
        "presets": {
            "machine": "Creality Ender-3 V3 SE 0.4 nozzle",
            "process": "0.20mm Standard @Creality Ender-3 V3 SE 0.4 nozzle",
            "filaments": ["Hyper PLA @Creality Ender-3 V3 SE 0.4 nozzle"],
        }
    }))
    monkeypatch.setattr(slicers, "CONFIG_CANDIDATES", [("creality", str(conf))])
    got = slicers.adopt()
    assert len(got) == 1
    assert got[0]["printer"] == "Creality Ender-3 V3 SE 0.4 nozzle"
    assert got[0]["process"].startswith("0.20mm Standard")
    assert got[0]["filament"].startswith("Hyper PLA")


def test_adopt_returns_newest_first(tmp_path, monkeypatch):
    import os, time
    a = tmp_path / "a.conf"
    b = tmp_path / "b.conf"
    for p, name in ((a, "Old Printer"), (b, "New Printer")):
        p.write_text(json.dumps({"presets": {"machine": name}}))
    os.utime(a, (1000, 1000))
    os.utime(b, (2000, 2000))
    monkeypatch.setattr(slicers, "CONFIG_CANDIDATES",
                        [("creality", str(a)), ("orca", str(b))])
    got = slicers.adopt()
    assert [g["printer"] for g in got] == ["New Printer", "Old Printer"]


def test_adopt_skips_unreadable_config(tmp_path, monkeypatch):
    bad = tmp_path / "bad.conf"
    bad.write_text("this is not json")
    monkeypatch.setattr(slicers, "CONFIG_CANDIDATES", [("creality", str(bad))])
    assert slicers.adopt() == []


def test_adopt_returns_empty_when_nothing_is_configured(monkeypatch):
    monkeypatch.setattr(slicers, "CONFIG_CANDIDATES", [])
    assert slicers.adopt() == []
