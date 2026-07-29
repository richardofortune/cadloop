import json
import os
from pathlib import Path

import pytest

from cadloop import profiles

FIXTURES = Path(__file__).parent / "profiles"


@pytest.fixture(autouse=True)
def only_fixture_profiles(monkeypatch):
    """Point profile discovery at the fixtures, not at whatever is installed."""
    monkeypatch.setenv("SLICER_PROFILE_DIRS", str(FIXTURES))
    monkeypatch.setattr(profiles, "PROFILE_CANDIDATES", [])
    profiles.reset_cache()
    yield
    profiles.reset_cache()


def _two_roots(tmp_path, monkeypatch, nest=False):
    """A real two-install tree, optionally with a third root wrapping both -
    which is what SLICER_PROFILE_DIRS=/Applications amounts to."""
    base = tmp_path.resolve()
    apps = base / "Applications"
    orca, crea = apps / "orca", apps / "crea"
    for root, tag in ((orca, "orca"), (crea, "crea")):
        root.mkdir(parents=True)
        (root / "Printer X.json").write_text(
            json.dumps({"type": "machine", "name": "Printer X", "from": tag}))
    dirs = [apps, orca, crea] if nest else [orca, crea]
    monkeypatch.setenv("SLICER_PROFILE_DIRS",
                       os.pathsep.join(str(d) for d in dirs))
    profiles.reset_cache()
    return apps, orca, crea


def test_root_of_answers_with_the_most_specific_root(tmp_path, monkeypatch):
    apps, orca, crea = _two_roots(tmp_path, monkeypatch, nest=True)
    assert apps in profiles.profile_roots()
    assert profiles.root_of(orca / "Printer X.json") == orca
    assert profiles.root_of(crea / "Printer X.json") == crea


def test_same_install_separates_two_installs_under_one_configured_root(
        tmp_path, monkeypatch):
    apps, orca, crea = _two_roots(tmp_path, monkeypatch, nest=True)
    assert profiles.same_install(orca / "Printer X.json",
                                 crea / "Printer X.json") is False
    assert profiles.same_install(orca / "Printer X.json",
                                 orca / "Printer X.json") is True


def test_same_install_is_none_when_it_cannot_tell(tmp_path, monkeypatch):
    _two_roots(tmp_path, monkeypatch)
    assert profiles.same_install(tmp_path / "elsewhere.json",
                                 tmp_path / "other.json") is None


def test_root_profiles_keeps_the_copy_profile_index_drops(
        tmp_path, monkeypatch):
    _apps, orca, crea = _two_roots(tmp_path, monkeypatch)
    # one path per name across every root, so one of the two copies is gone
    assert profiles.profile_index()["Printer X"] == orca / "Printer X.json"
    assert [r["path"] for r in profiles.root_profiles(crea)] == [
        str(crea / "Printer X.json")]


def test_reset_cache_lets_a_later_install_be_seen(tmp_path, monkeypatch):
    _apps, orca, _crea = _two_roots(tmp_path, monkeypatch)
    assert "Printer Y" not in profiles.profile_index()
    (orca / "Printer Y.json").write_text(
        json.dumps({"type": "machine", "name": "Printer Y"}))
    assert "Printer Y" not in profiles.profile_index()
    profiles.reset_cache()
    assert "Printer Y" in profiles.profile_index()
    assert any(r["name"] == "Printer Y" for r in profiles.root_profiles(orca))


def test_area_points_reads_a_list_of_pairs():
    pts = profiles.area_points(["0x0", "220x0", "220x220", "0x220"])
    assert pts == [(0.0, 0.0), (220.0, 0.0), (220.0, 220.0), (0.0, 220.0)]


def test_area_points_reads_one_comma_separated_string():
    pts = profiles.area_points("0x0,180x0,180x180,0x180")
    assert pts == [(0.0, 0.0), (180.0, 0.0), (180.0, 180.0), (0.0, 180.0)]


def test_area_points_rejects_nonsense():
    assert profiles.area_points(None) == []
    assert profiles.area_points(42) == []


def test_bed_of_list_form():
    bed = profiles.bed_of(FIXTURES / "flat.json")
    assert bed == {"x_mm": 220.0, "y_mm": 220.0, "z_mm": 250.0, "origin": [0.0, 0.0]}


def test_bed_of_string_form():
    bed = profiles.bed_of(FIXTURES / "stringbed.json")
    assert bed["x_mm"] == 180.0 and bed["z_mm"] == 200.0


def test_bed_of_follows_inherits():
    bed = profiles.bed_of(FIXTURES / "child.json")
    assert bed["x_mm"] == 200.0 and bed["z_mm"] == 180.0


def test_bed_of_returns_empty_when_no_bed_anywhere_in_the_chain():
    assert profiles.bed_of(FIXTURES / "nobed.json") == {}


def test_classify_recognises_a_machine():
    rec = profiles.classify(FIXTURES / "flat.json")
    assert rec["kind"] == "machine" and rec["name"] == "Flat Bed"


def test_classify_ignores_json_that_is_not_a_profile(tmp_path):
    other = tmp_path / "notaprofile.json"
    other.write_text('{"hello": "world"}')
    assert profiles.classify(other) is None


def test_machine_facts_reads_all_five():
    facts = profiles.machine_facts(FIXTURES / "flat.json",
                                   FIXTURES / "proc.json",
                                   FIXTURES / "fila.json")
    assert facts["printer_model"] == "Fixture Printer"
    assert facts["nozzle_mm"] == 0.4
    assert facts["gcode_flavor"] == "marlin2"
    assert facts["layer_height_mm"] == 0.2
    assert facts["filament_type"] == "PLA"
    assert facts["bed"]["x_mm"] == 220.0


def test_machine_facts_leaves_unknowns_as_none():
    facts = profiles.machine_facts(FIXTURES / "child.json",
                                   FIXTURES / "proc.json",
                                   FIXTURES / "fila.json")
    assert facts["nozzle_mm"] is None
    assert facts["bed"]["x_mm"] == 200.0


def test_machine_facts_is_json_serialisable():
    import json
    facts = profiles.machine_facts(FIXTURES / "flat.json",
                                   FIXTURES / "proc.json",
                                   FIXTURES / "fila.json")
    json.loads(json.dumps(facts))


def test_install_of_answers_the_deepest_install_holding_a_path(
        tmp_path, monkeypatch):
    apps, orca, crea = _two_roots(tmp_path, monkeypatch, nest=True)
    assert profiles.install_of(orca / "Printer X.json")["root"] == str(orca)


def test_install_of_returns_none_for_a_path_no_install_holds(
        tmp_path, monkeypatch):
    _two_roots(tmp_path, monkeypatch)
    stray = tmp_path / "elsewhere" / "M.json"
    stray.parent.mkdir()
    stray.write_text('{"type": "machine", "name": "M"}')
    assert profiles.install_of(stray) is None


def test_profiles_in_lists_one_install_only(tmp_path, monkeypatch):
    apps, orca, crea = _two_roots(tmp_path, monkeypatch)
    for root in (orca, crea):
        (root / "Shared.json").write_text(
            json.dumps({"type": "process", "name": "Shared"}))
    got = profiles.profiles_in({"root": str(orca), "name": "orca"}, "process")
    assert [g["path"] for g in got] == [str(orca / "Shared.json")]


def test_profiles_in_is_json_serialisable(tmp_path, monkeypatch):
    apps, orca, crea = _two_roots(tmp_path, monkeypatch)
    (orca / "P.json").write_text(json.dumps({"type": "process", "name": "P"}))
    json.loads(json.dumps(
        profiles.profiles_in({"root": str(orca), "name": "orca"}, "process")))


def test_profiles_in_is_sorted_by_name(tmp_path, monkeypatch):
    """Filenames deliberately disagree with the `name` field's order, so a
    result that merely preserved root_profiles()'s path order would still
    come out wrong here."""
    apps, orca, crea = _two_roots(tmp_path, monkeypatch)
    for filename, name in (("a_file", "Zeta"), ("b_file", "Mu"),
                           ("c_file", "Alpha")):
        (orca / f"{filename}.json").write_text(
            json.dumps({"type": "process", "name": name}))
    got = profiles.profiles_in({"root": str(orca), "name": "orca"}, "process")
    assert [g["name"] for g in got] == ["Alpha", "Mu", "Zeta"]
