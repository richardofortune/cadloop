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


def test_resolve_refuses_an_ambiguous_printer_name(monkeypatch, tmp_path):
    from cadloop import profiles
    idx = {}
    for name in ("Creality K1", "Creality K1C", "Creality K1 SE"):
        p = tmp_path / f"{name}.json"
        p.write_text(json.dumps({"type": "machine", "name": name}))
        idx[name] = p
    monkeypatch.setattr(profiles, "profile_index", lambda: idx)
    out = machine.resolve(printer="K1", filament=None)
    assert out["ok"] is False
    assert len(out["candidates"]) == 3
    assert "K1C" in " ".join(out["candidates"])


def test_resolve_accepts_an_exact_printer_name(monkeypatch, tmp_path):
    from cadloop import profiles
    idx = {}
    # an exact match must not drag in the longer name that contains it
    for name, kind in (("Creality K1", "machine"), ("Creality K1C", "machine"),
                       ("0.20mm Standard @Creality K1", "process"),
                       ("Generic PLA @Creality K1", "filament")):
        f = tmp_path / f"{name}.json"
        f.write_text(json.dumps({"type": kind, "name": name}))
        idx[name] = f
    monkeypatch.setattr(profiles, "profile_index", lambda: idx)
    out = machine.resolve(printer="Creality K1", filament=None)
    assert out["ok"] is True, out["reason"]
    assert out["profiles"]["machine"] == str(idx["Creality K1"])
    assert "Standard" in out["profiles"]["process"]


def test_resolve_reports_a_printer_with_no_process_profile(monkeypatch, tmp_path):
    from cadloop import profiles
    f = tmp_path / "Lonely Printer.json"
    f.write_text(json.dumps({"type": "machine", "name": "Lonely Printer"}))
    monkeypatch.setattr(profiles, "profile_index", lambda: {"Lonely Printer": f})
    out = machine.resolve(printer="Lonely Printer", filament=None)
    assert out["ok"] is False
    assert "process" in out["reason"]


def test_resolve_reports_when_nothing_matches(monkeypatch):
    from cadloop import profiles
    monkeypatch.setattr(profiles, "profile_index", lambda: {})
    out = machine.resolve(printer="No Such Printer", filament=None)
    assert out["ok"] is False
    assert "no printer" in out["reason"].lower()


def test_prove_reports_a_slicer_that_answers_help_but_cannot_slice(tmp_path):
    import stat
    from cadloop import slicer_server

    fake = tmp_path / "fussy"
    fake.write_text("#!/bin/sh\n"
                    "echo 'Relative extruder addressing requires resetting' >&2\n"
                    "exit 205\n")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)

    profs = {}
    for kind in ("machine", "process", "filament"):
        f = tmp_path / f"{kind}.json"
        f.write_text(json.dumps({"type": kind, "name": kind}))
        profs[kind] = str(f)

    out = slicer_server._prove(str(fake), profs)
    assert out["ok"] is False
    assert "205" in out["reason"] or "Relative extruder" in out["reason"]
