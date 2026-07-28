import json
from pathlib import Path

import pytest

from cadloop import machine


@pytest.fixture(autouse=True)
def isolated_record(tmp_path, monkeypatch):
    monkeypatch.setenv("CADLOOP_MACHINE", str(tmp_path / "machine.json"))
    yield


def _profiles(tmp_path: Path) -> dict:
    out = {}
    for kind in ("machine", "process", "filament"):
        p = tmp_path / f"{kind}.json"
        p.write_text(json.dumps({"type": kind, "name": kind}))
        out[kind] = str(p)
    return out


@pytest.fixture
def fake_binary(tmp_path):
    """staleness() only checks the path exists, so this needs to be a file,
    not a working executable. Hardcoding /bin/true or /usr/bin/true passes on
    one platform and fails on the other."""
    b = tmp_path / "slicer"
    b.write_text("")
    return str(b)


def test_load_returns_none_when_nothing_saved():
    assert machine.load() is None


def test_save_then_load_round_trips(tmp_path):
    rec = {"schema": machine.SCHEMA, "name": "Fixture",
           "profiles": _profiles(tmp_path)}
    machine.save(rec)
    assert machine.load()["name"] == "Fixture"


def test_fingerprint_changes_when_a_profile_changes(tmp_path, fake_binary):
    profs = _profiles(tmp_path)
    before = machine.fingerprint(fake_binary, "1.0", profs)
    Path(profs["process"]).write_text('{"type": "process", "name": "edited"}')
    after = machine.fingerprint(fake_binary, "1.0", profs)
    assert before != after


def test_staleness_is_none_for_a_fresh_record(tmp_path, fake_binary):
    profs = _profiles(tmp_path)
    rec = {"schema": machine.SCHEMA, "slicer": {"binary": fake_binary, "version": "1.0"},
           "profiles": profs,
           "fingerprint": machine.fingerprint(fake_binary, "1.0", profs)}
    assert machine.staleness(rec) is None


def test_staleness_reports_an_edited_profile(tmp_path, fake_binary):
    profs = _profiles(tmp_path)
    rec = {"schema": machine.SCHEMA, "slicer": {"binary": fake_binary, "version": "1.0"},
           "profiles": profs,
           "fingerprint": machine.fingerprint(fake_binary, "1.0", profs)}
    Path(profs["machine"]).write_text('{"type": "machine", "name": "edited"}')
    assert "machine" in machine.staleness(rec)


def test_staleness_reports_a_missing_profile(tmp_path, fake_binary):
    profs = _profiles(tmp_path)
    rec = {"schema": machine.SCHEMA, "slicer": {"binary": fake_binary, "version": "1.0"},
           "profiles": profs,
           "fingerprint": machine.fingerprint(fake_binary, "1.0", profs)}
    Path(profs["filament"]).unlink()
    assert "filament" in machine.staleness(rec)


def test_staleness_reports_a_slicer_that_has_gone(tmp_path, fake_binary):
    profs = _profiles(tmp_path)
    rec = {"schema": machine.SCHEMA,
           "slicer": {"binary": fake_binary, "version": "1.0"},
           "profiles": profs,
           "fingerprint": machine.fingerprint(fake_binary, "1.0", profs)}
    Path(fake_binary).unlink()
    assert "gone" in machine.staleness(rec)


def test_staleness_reports_an_old_schema(tmp_path):
    assert "schema" in machine.staleness({"schema": 0})
