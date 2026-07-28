import json
import os
from pathlib import Path

import pytest

from cadloop import machine
from cadloop import profiles


@pytest.fixture(autouse=True)
def isolated_record(tmp_path, monkeypatch):
    """The record lives beside nothing else: it used to be written to
    tmp_path/machine.json, which is also where the machine *profile* fixtures
    go, so saving a record silently rewrote the profile it fingerprinted."""
    monkeypatch.setenv("CADLOOP_MACHINE", str(tmp_path / "record.json"))
    yield


@pytest.fixture(autouse=True)
def only_declared_roots(monkeypatch):
    """No test here may see the developer's own installed slicers.

    Three resolve tests used to patch profile_index but not profile_roots, so
    _root_of scanned whatever was really installed, found nothing, and turned
    the same-install guard into a no-op for the whole test."""
    monkeypatch.setattr(profiles, "PROFILE_CANDIDATES", [])
    monkeypatch.setenv("SLICER_PROFILE_DIRS", "")
    profiles.reset_cache()
    yield
    profiles.reset_cache()


@pytest.fixture
def tree(tmp_path):
    """Resolved: profile roots are resolved before they are compared, and on
    macOS /var is a symlink to /private/var."""
    return tmp_path.resolve()


@pytest.fixture
def roots(monkeypatch):
    """Point profile discovery at real directories and let profile_roots(),
    profile_index() and root_profiles() actually run against them."""
    def use(*dirs):
        monkeypatch.setenv("SLICER_PROFILE_DIRS",
                           os.pathsep.join(str(d) for d in dirs))
        profiles.reset_cache()
    return use


def write(root: Path, kind: str, name: str, **extra) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    p = root / f"{name}.json"
    p.write_text(json.dumps({"type": kind, "name": name, **extra}))
    return p


def _profiles(tmp_path: Path) -> dict:
    out = {}
    for kind in ("machine", "process", "filament"):
        p = tmp_path / f"{kind}.json"
        p.write_text(json.dumps({"type": kind, "name": kind}))
        out[kind] = str(p)
    return out


@pytest.fixture
def fake_binary(tmp_path):
    """staleness() only checks the path is a file, so this needs to be a file,
    not a working executable. Hardcoding /bin/true or /usr/bin/true passes on
    one platform and fails on the other."""
    b = tmp_path / "slicer"
    b.write_text("")
    return str(b)


def _record(binary: str, profs: dict, name: str = "Fixture") -> dict:
    return {"schema": machine.SCHEMA, "name": name,
            "slicer": {"family": "orca", "binary": binary, "version": "1.0"},
            "profiles": profs,
            "fingerprint": machine.fingerprint(binary, "1.0", profs)}


# ------------------------------------------------------------------ record

def test_load_returns_none_when_nothing_saved():
    assert machine.load() is None


def test_save_then_load_round_trips(tmp_path):
    rec = {"schema": machine.SCHEMA, "name": "Fixture",
           "profiles": _profiles(tmp_path)}
    machine.save(rec)
    assert machine.load()["name"] == "Fixture"


def test_save_leaves_no_temporary_file_behind(tmp_path):
    machine.save({"schema": machine.SCHEMA, "name": "Fixture"})
    assert sorted(p.name for p in tmp_path.iterdir()) == ["record.json"]


def test_a_record_path_pointing_at_a_directory_is_a_reason_not_a_traceback(
        tmp_path, monkeypatch):
    d = tmp_path / "adirectory"
    d.mkdir()
    monkeypatch.setenv("CADLOOP_MACHINE", str(d))
    with pytest.raises(machine.RecordError) as exc:
        machine.load()
    assert "directory" in str(exc.value)


def test_a_record_path_does_not_get_to_create_directory_trees(
        tmp_path, monkeypatch):
    monkeypatch.setenv("CADLOOP_MACHINE", str(tmp_path / "a" / "b" / "rec.json"))
    with pytest.raises(machine.RecordError):
        machine.save({"schema": machine.SCHEMA})
    assert not (tmp_path / "a").exists()


def test_a_record_anyone_could_replace_is_refused(tmp_path, monkeypatch):
    """The record supplies argv[0] to subprocess.run, so a directory other
    users may write to without the sticky bit that stops them removing our
    file hands them the program this server runs. /tmp has the sticky bit and
    is fine; a mode 777 directory is not."""
    if not hasattr(os, "getuid"):
        pytest.skip("no POSIX ownership here")
    open_dir = tmp_path / "open"
    open_dir.mkdir()
    open_dir.chmod(0o777)      # mkdir(mode=) is filtered through the umask
    monkeypatch.setenv("CADLOOP_MACHINE", str(open_dir / "rec.json"))
    with pytest.raises(machine.RecordError) as exc:
        machine.load()
    assert "writable by other users" in str(exc.value)


def test_a_sticky_shared_directory_is_still_allowed(tmp_path, monkeypatch):
    if not hasattr(os, "getuid"):
        pytest.skip("no POSIX ownership here")
    sticky = tmp_path / "sticky"
    sticky.mkdir()
    sticky.chmod(0o1777)
    monkeypatch.setenv("CADLOOP_MACHINE", str(sticky / "rec.json"))
    machine.save({"schema": machine.SCHEMA, "name": "Fixture"})
    assert machine.load()["name"] == "Fixture"


def test_fingerprint_changes_when_a_profile_changes(tmp_path, fake_binary):
    profs = _profiles(tmp_path)
    before = machine.fingerprint(fake_binary, "1.0", profs)
    Path(profs["process"]).write_text('{"type": "process", "name": "edited"}')
    after = machine.fingerprint(fake_binary, "1.0", profs)
    assert before != after


def test_staleness_is_none_for_a_fresh_record(tmp_path, fake_binary):
    assert machine.staleness(_record(fake_binary, _profiles(tmp_path))) is None


def test_staleness_reports_an_edited_profile(tmp_path, fake_binary):
    profs = _profiles(tmp_path)
    rec = _record(fake_binary, profs)
    Path(profs["machine"]).write_text('{"type": "machine", "name": "edited"}')
    assert "machine" in machine.staleness(rec)


def test_staleness_reports_a_missing_profile(tmp_path, fake_binary):
    profs = _profiles(tmp_path)
    rec = _record(fake_binary, profs)
    Path(profs["filament"]).unlink()
    assert "filament" in machine.staleness(rec)


def test_staleness_reports_a_slicer_that_has_gone(tmp_path, fake_binary):
    profs = _profiles(tmp_path)
    rec = _record(fake_binary, profs)
    Path(fake_binary).unlink()
    assert "gone" in machine.staleness(rec)


def test_staleness_reports_an_old_schema():
    assert "schema" in machine.staleness({"schema": 0})


# ----------------------------------------------------------------- resolve

def test_resolve_refuses_an_ambiguous_printer_name(tree, roots):
    for name in ("Creality K1", "Creality K1C", "Creality K1 SE"):
        write(tree / "orca", "machine", name)
    roots(tree / "orca")
    out = machine.resolve(printer="K1", filament=None)
    assert out["ok"] is False
    assert len(out["candidates"]) == 3
    assert "K1C" in " ".join(out["candidates"])


def test_resolve_accepts_an_exact_printer_name(tree, roots):
    # an exact match must not drag in the longer name that contains it
    root = tree / "orca"
    for name, kind in (("Creality K1", "machine"), ("Creality K1C", "machine"),
                       ("0.20mm Standard @Creality K1", "process"),
                       ("Generic PLA @Creality K1", "filament")):
        write(root, kind, name)
    roots(root)
    out = machine.resolve(printer="Creality K1", filament=None)
    assert out["ok"] is True, out["reason"]
    assert out["profiles"]["machine"] == str(root / "Creality K1.json")
    assert "Standard" in out["profiles"]["process"]
    assert out["names"]["process"] == "0.20mm Standard @Creality K1"


def test_resolve_reports_a_printer_with_no_process_profile(tree, roots):
    write(tree / "orca", "machine", "Lonely Printer")
    roots(tree / "orca")
    out = machine.resolve(printer="Lonely Printer", filament=None)
    assert out["ok"] is False
    assert "process" in out["reason"]


def test_resolve_reports_when_nothing_matches(tree, roots):
    (tree / "empty").mkdir()
    roots(tree / "empty")
    out = machine.resolve(printer="No Such Printer", filament=None)
    assert out["ok"] is False
    assert "no printer" in out["reason"].lower()


def test_resolve_keeps_the_triple_within_one_profile_root(tree, roots):
    """Both installs ship the machine and the filament under the same names;
    only the second ships a process. The tempting answer is the first
    install's machine and filament with the second's process, and that is a
    combination nobody has ever validated: Creality's machine profile sets
    use_relative_e_distances and Orca's leaves it unset.

    Built on disk, with profile_roots() and profile_index() running against
    it for real. The fixture used to hand resolve() a dict that had already
    collapsed all three names onto one root, so the assertion held whether
    the guard ran or not."""
    first, second = tree / "Creality Print.app", tree / "OrcaSlicer.app"
    write(first, "machine", "Printer X")
    write(first, "filament", "PLA @Printer X")
    write(second, "machine", "Printer X")
    write(second, "process", "0.20mm Standard @Printer X")
    write(second, "filament", "PLA @Printer X")
    roots(first, second)

    out = machine.resolve(printer="Printer X", filament=None)
    assert out["ok"] is True, out["reason"]
    # The directory each file is really in, read off the paths rather than
    # asked of the code under test, so a broken root_of cannot make this
    # assertion agree with itself.
    where = {str(Path(p).parent) for p in out["profiles"].values()}
    assert where == {str(second)}, f"triple spans installs: {out['profiles']}"


def _shadowed_pair(base: Path) -> tuple[Path, Path]:
    """The Ender-3 V3 SE shape. Two installs ship the same machine name and
    the same process name; only the second has a filament for it, so only the
    second has a complete triple. The first is scanned first and therefore
    wins both names in the flattened index."""
    first, second = base / "Creality Print.app", base / "OrcaSlicer.app"
    write(first, "machine", "Printer X")
    write(first, "process", "0.20mm Standard @Printer X")
    write(second, "machine", "Printer X")
    write(second, "process", "0.20mm Standard @Printer X")
    write(second, "filament", "PLA @Printer X")
    return first, second


def test_a_root_that_contains_another_root_does_not_defeat_the_guard(
        tree, roots):
    """SLICER_PROFILE_DIRS pointing at /Applications is natural, documented,
    and used to turn the same-install guard off: every install sits under it,
    so "is this profile under the machine's root" answered yes for all of
    them, and the triple was assembled across vendors again with ok: True."""
    apps = tree / "Applications"
    first, second = _shadowed_pair(apps)
    roots(apps, first, second)

    out = machine.resolve(printer="Printer X", filament=None)
    assert out["ok"] is True, out["reason"]
    # Read off the paths, not asked of the code under test, so a broken
    # root_of cannot make this assertion agree with itself.
    where = {str(Path(p).parent) for p in out["profiles"].values()}
    assert where == {str(second)}, f"triple spans installs: {out['profiles']}"


def test_a_machine_alone_in_a_wrapper_root_cannot_borrow_from_the_installs_inside(
        tree, roots):
    """The other half of the same hole: a profile sitting loose in a
    directory that merely contains two installs belongs to neither of them,
    and completing its triple out of one of them is a guess."""
    apps = tree / "Applications"
    orca = apps / "OrcaSlicer.app"
    write(apps, "machine", "Printer X")
    write(orca, "process", "0.20mm Standard @Printer X")
    write(orca, "filament", "PLA @Printer X")
    roots(apps, orca)

    out = machine.resolve(printer="Printer X", filament=None)
    assert out["ok"] is False, out.get("profiles")
    assert "process" in out["reason"] or "filament" in out["reason"]


def test_resolve_recovers_a_complete_triple_shadowed_by_another_root(
        tree, roots):
    """profile_index() keeps only one path per name, whichever root it saw
    first - so when two vendors ship an identically named machine profile,
    the copy it drops can be the one with a matching process and filament.

    The process name collides across the two installs too, which is normal
    for two Orca forks. That is what makes the flattened index useless for
    the retry as well: it has already dropped the second install's copy of
    the process, so scoping it to the second install finds nothing and the
    printer that does work here is reported as broken."""
    first, second = _shadowed_pair(tree)
    roots(first, second)

    # The incomplete install wins both names, because it is scanned first.
    idx = profiles.profile_index()
    assert idx["Printer X"] == first / "Printer X.json"
    assert idx["0.20mm Standard @Printer X"] == (
        first / "0.20mm Standard @Printer X.json")

    out = machine.resolve(printer="Printer X", filament=None)
    assert out["ok"] is True, out["reason"]
    assert out["profiles"]["machine"] == str(second / "Printer X.json")
    assert out["profiles"]["process"] == str(
        second / "0.20mm Standard @Printer X.json")
    assert out["profiles"]["filament"] == str(second / "PLA @Printer X.json")


def test_resolve_uses_the_process_it_is_given(tree, roots):
    root = tree / "orca"
    write(root, "machine", "Printer X")
    write(root, "process", "0.12mm Fine @Printer X")
    write(root, "process", "0.20mm Standard @Printer X")
    write(root, "filament", "PLA @Printer X")
    roots(root)
    out = machine.resolve(printer="Printer X", filament=None,
                          process="0.20mm Standard @Printer X")
    assert "0.20mm Standard" in out["profiles"]["process"]


def test_a_demanded_process_in_another_install_is_refused_not_replaced(
        tree, roots):
    """A user preset selected in the GUI lives in the config directory while
    the stock profiles live in the app bundle. Silently throwing the preset
    away for the alphabetically first stock one is the worst of the three
    available answers."""
    stock, user = tree / "stock", tree / "user"
    write(stock, "machine", "Printer X")
    write(stock, "process", "0.20mm Standard @Printer X")
    write(stock, "filament", "PLA @Printer X")
    write(user, "process", "My Custom Fine @Printer X")
    roots(stock, user)

    out = machine.resolve(printer="Printer X", filament=None,
                          process="My Custom Fine @Printer X")
    assert out["ok"] is False
    assert "My Custom Fine @Printer X" in out["reason"]
    assert str(user) in out["reason"]


def test_a_demanded_filament_that_exists_nowhere_is_named(tree, roots):
    root = tree / "orca"
    write(root, "machine", "Printer X")
    write(root, "process", "0.20mm Standard @Printer X")
    write(root, "filament", "PLA @Printer X")
    roots(root)
    out = machine.resolve(printer="Printer X", filament="Unobtainium",
                          explicit=["filament"])
    assert out["ok"] is False
    assert "Unobtainium" in out["reason"]


def test_an_adopted_filament_that_has_gone_falls_back_and_says_so(tree, roots):
    """What the slicer's config names can be two changes out of date. That is
    a starting point, not a demand, so it substitutes - and reports it."""
    root = tree / "orca"
    write(root, "machine", "Printer X")
    write(root, "process", "0.20mm Standard @Printer X")
    write(root, "filament", "PLA @Printer X")
    roots(root)
    out = machine.resolve(printer="Printer X", filament="Deleted Preset",
                          explicit=[])
    assert out["ok"] is True, out["reason"]
    assert out["profiles"]["filament"] == str(root / "PLA @Printer X.json")
    assert any("Deleted Preset" in n for n in out["notes"]), out["notes"]


# ------------------------------------------------------------------ server

def test_binary_prefers_the_stored_record_over_auto_detection(
        monkeypatch, tmp_path, fake_binary):
    from cadloop import slicer_server

    monkeypatch.delenv("SLICER_BIN", raising=False)
    Path(fake_binary).chmod(0o755)
    machine.save(_record(fake_binary, _profiles(tmp_path)))
    monkeypatch.setattr(slicer_server, "_probe_once",
                        lambda b: {"ok": True, "version": "1.0", "reason": ""})
    monkeypatch.setattr(slicer_server, "_detect",
                        lambda: (_ for _ in ()).throw(
                            AssertionError("detection should not run")))
    assert slicer_server._binary() == fake_binary


def test_binary_refuses_a_stale_record_rather_than_substituting(
        monkeypatch, tmp_path, fake_binary):
    """Falling through to detection here runs the slice on a slicer that was
    never proven against these profiles, and says nothing about it."""
    from cadloop import slicer_server

    monkeypatch.delenv("SLICER_BIN", raising=False)
    profs = _profiles(tmp_path)
    machine.save(_record(fake_binary, profs))
    Path(profs["process"]).write_text('{"type": "process", "name": "edited"}')
    monkeypatch.setattr(slicer_server, "_detect",
                        lambda: (_ for _ in ()).throw(
                            AssertionError("detection should not run")))
    with pytest.raises(slicer_server.Refused) as exc:
        slicer_server._binary()
    assert "process profile changed" in str(exc.value)


def test_binary_refuses_a_recorded_path_that_is_not_executable(
        monkeypatch, tmp_path, fake_binary):
    from cadloop import slicer_server

    monkeypatch.delenv("SLICER_BIN", raising=False)
    Path(fake_binary).chmod(0o644)
    machine.save(_record(fake_binary, _profiles(tmp_path)))
    monkeypatch.setattr(slicer_server, "_detect",
                        lambda: (_ for _ in ()).throw(
                            AssertionError("detection should not run")))
    with pytest.raises(slicer_server.Refused) as exc:
        slicer_server._binary()
    assert "executable" in str(exc.value)


def test_slicer_bin_still_beats_the_stored_record(
        monkeypatch, tmp_path, fake_binary):
    from cadloop import slicer_server

    machine.save(_record(fake_binary, _profiles(tmp_path)))
    override = tmp_path / "override-slicer"
    override.write_text("")
    monkeypatch.setenv("SLICER_BIN", str(override))
    assert slicer_server._binary() == str(override)


def test_a_stale_record_refuses_the_call_instead_of_warning_about_it(
        tmp_path, fake_binary):
    """A3: reported, never used, and before any G-code is written. A warning
    attached to the result of the call that already read a cached bed and
    already wrote the file is none of those three."""
    from cadloop import slicer_server

    profs = _profiles(tmp_path)
    machine.save(_record(fake_binary, profs))
    Path(profs["machine"]).write_text('{"type": "machine", "name": "edited"}')
    with pytest.raises(slicer_server.Refused) as exc:
        slicer_server._active_profiles()
    assert "machine profile changed" in str(exc.value)


def test_an_explicit_machine_is_not_paired_with_a_stored_process(
        tree, roots, tmp_path, fake_binary):
    """Field-by-field filling from the record produces exactly the
    cross-vendor triple this record exists to eliminate, approached from the
    other side."""
    from cadloop import slicer_server

    orca, crea = tree / "orca", tree / "crea"
    write(orca, "machine", "Printer X")
    stored = {"machine": str(write(crea, "machine", "Printer X")),
              "process": str(write(crea, "process", "Std @Printer X")),
              "filament": str(write(crea, "filament", "PLA @Printer X"))}
    roots(orca, crea)
    machine.save(_record(fake_binary, stored))

    with pytest.raises(slicer_server.Refused) as exc:
        slicer_server._active_profiles(
            machine_profile=str(orca / "Printer X.json"))
    assert "different slicer installs" in str(exc.value)


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
