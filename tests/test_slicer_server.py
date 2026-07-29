"""check_bed_fit: the other place a part's height could go unchecked.

This tool shipped in 0.1.0 answering ok True for a part of any height
whenever the machine profile declared no printable_height, because the
height test was written as "is it taller than the limit" and a limit of
None simply made that False. Fixing gcode.fits does not reach here; it is
a second, independent copy of the same mistake.
"""

import json

import pytest

from cadloop import machine, profiles, slicer_server


@pytest.fixture(autouse=True)
def only_declared_roots(monkeypatch):
    """No test here may see the developer's own installed slicers."""
    monkeypatch.setattr(profiles, "PROFILE_CANDIDATES", [])
    monkeypatch.setenv("SLICER_PROFILE_DIRS", "")
    profiles.reset_cache()
    yield
    profiles.reset_cache()


def _stl(path, size_mm):
    """An ASCII STL of a box size_mm[0] x size_mm[1] x size_mm[2]."""
    x, y, z = size_mm
    tris = [((0, 0, 0), (x, 0, 0), (x, y, 0)),
            ((0, 0, 0), (x, y, 0), (0, y, 0)),
            ((0, 0, 0), (x, 0, 0), (0, 0, z)),
            ((x, 0, 0), (x, y, 0), (0, 0, z))]
    body = "".join(
        "facet normal 0 0 1\n outer loop\n"
        + "".join("  vertex %f %f %f\n" % v for v in t)
        + " endloop\nendfacet\n" for t in tris)
    path.write_text("solid part\n" + body + "endsolid part\n")
    return path


@pytest.fixture
def ws(tmp_path, monkeypatch):
    """A workspace the server will read, and profiles to name explicitly."""
    workspace = tmp_path / "cad"
    workspace.mkdir()
    monkeypatch.setattr(slicer_server, "WORKSPACE", workspace)
    monkeypatch.setenv("CADLOOP_MACHINE", str(tmp_path / "record.json"))
    return workspace


def _profile(tmp_path, name, **extra):
    p = tmp_path / f"{name}.json"
    p.write_text(json.dumps({
        "type": "machine", "name": name,
        "printable_area": ["0x0", "220x0", "220x220", "0x220"], **extra}))
    return str(p)


def test_a_part_taller_than_anyone_can_check_is_not_reported_printable(
        tmp_path, ws):
    _stl(ws / "column.stl", (100.0, 100.0, 900.0))
    prof = _profile(tmp_path, "noheight")          # no printable_height
    got = slicer_server.check_bed_fit("column.stl", machine_profile=prof)
    assert got["ok"] is None                       # never True
    assert got["height_checked"] is False
    assert "no printable height" in got["reason"]
    assert "z is unchecked" in got["reason"]
    assert got["fits_square"] is True              # the footprint is fine


def test_a_known_height_still_answers_normally(tmp_path, ws):
    _stl(ws / "column.stl", (100.0, 100.0, 900.0))
    prof = _profile(tmp_path, "tall", printable_height="250")
    got = slicer_server.check_bed_fit("column.stl", machine_profile=prof)
    assert got["ok"] is False
    assert got["too_tall"] is True and got["height_checked"] is True

    _stl(ws / "small.stl", (40.0, 40.0, 30.0))
    got = slicer_server.check_bed_fit("small.stl", machine_profile=prof)
    assert got["ok"] is True
    assert got["too_tall"] is False and got["reason"] == ""


def test_a_footprint_that_does_not_fit_fails_even_with_an_unknown_height(
        tmp_path, ws):
    # A definite answer outranks an unknown one, the same way gcode.fits
    # ranks them: this part is off the bed whatever its height.
    _stl(ws / "wide.stl", (400.0, 400.0, 900.0))
    prof = _profile(tmp_path, "noheight")
    got = slicer_server.check_bed_fit("wide.stl", machine_profile=prof)
    assert got["ok"] is False
    assert got["fits_square"] is False and got["fits_rotated_45"] is False


def test_a_flat_part_needs_no_height(tmp_path, ws):
    _stl(ws / "flat.stl", (40.0, 40.0, 0.0))
    prof = _profile(tmp_path, "noheight")
    got = slicer_server.check_bed_fit("flat.stl", machine_profile=prof)
    assert got["ok"] is True       # nothing to check in z


def test_the_bed_comes_from_the_shared_merge_not_a_second_copy(tmp_path, ws):
    # The profile has lost its printable_height; the record still remembers
    # one. check_bed_fit and the pipeline must agree, and both get it from
    # machine.bed(), so a height the profile dropped is still checked here.
    prof = _profile(tmp_path, "noheight")
    machine.save({"schema": machine.SCHEMA, "name": "B",
                  "slicer": {"binary": "", "version": "1"},
                  "profiles": {"machine": prof, "process": "", "filament": ""},
                  "derived": {"bed": {"x_mm": 220.0, "y_mm": 220.0,
                                      "z_mm": 250.0, "origin": [0.0, 0.0]}},
                  "fingerprint": {}})
    _stl(ws / "column.stl", (100.0, 100.0, 900.0))
    got = slicer_server.check_bed_fit("column.stl")
    assert got["bed"]["z_mm"] == 250.0
    assert got["ok"] is False and got["too_tall"] is True
    assert got["bed"] == machine.bed(machine.load())


def test_a_named_profile_is_not_topped_up_from_the_stored_record(tmp_path, ws):
    # The record describes a printer 250 mm tall. The caller is asking about
    # a different profile, which declares no height. Borrowing this
    # machine's height to answer about that one would be answering about
    # neither, so the height stays unchecked.
    mine = _profile(tmp_path, "mine", printable_height="250")
    machine.save({"schema": machine.SCHEMA, "name": "mine",
                  "slicer": {"binary": "", "version": "1"},
                  "profiles": {"machine": mine, "process": "", "filament": ""},
                  "derived": {"bed": {"x_mm": 220.0, "y_mm": 220.0,
                                      "z_mm": 250.0, "origin": [0.0, 0.0]}},
                  "fingerprint": {}})
    theirs = _profile(tmp_path, "theirs")
    _stl(ws / "column.stl", (100.0, 100.0, 900.0))
    got = slicer_server.check_bed_fit("column.stl", machine_profile=theirs)
    assert got["ok"] is None
    assert got["height_checked"] is False
    assert "z_mm" not in got["bed"]


# --------------------------------------------------------------------
# a no that says why, on every return
# --------------------------------------------------------------------

def test_a_part_too_tall_says_by_how_much(tmp_path, ws):
    _stl(ws / "column.stl", (100.0, 100.0, 900.0))
    prof = _profile(tmp_path, "tall", printable_height="250")
    got = slicer_server.check_bed_fit("column.stl", machine_profile=prof)
    assert got["ok"] is False
    # The one answer a user most needs explained used to come back with an
    # empty reason, because only the None path filled it in.
    assert got["reason"]
    assert "900.0 mm tall" in got["reason"]
    assert "650.0 mm more" in got["reason"]
    assert "250.0 mm" in got["reason"]


def test_a_footprint_that_does_not_fit_says_which_axis_and_by_how_much(
        tmp_path, ws):
    _stl(ws / "wide.stl", (400.0, 120.0, 10.0))
    prof = _profile(tmp_path, "tall", printable_height="250")
    got = slicer_server.check_bed_fit("wide.stl", machine_profile=prof)
    assert got["ok"] is False
    assert "x overruns by 183.0 mm" in got["reason"]     # 400 + 3 - 220
    assert "y overruns" not in got["reason"]             # 120 fits
    assert "45 degree diagonal" in got["reason"]


def test_both_axes_overrunning_are_both_named(tmp_path, ws):
    _stl(ws / "huge.stl", (400.0, 300.0, 10.0))
    prof = _profile(tmp_path, "tall", printable_height="250")
    got = slicer_server.check_bed_fit("huge.stl", machine_profile=prof)
    assert "x overruns by 183.0 mm" in got["reason"]
    assert "y overruns by 83.0 mm" in got["reason"]


def test_a_part_that_fits_has_nothing_to_explain(tmp_path, ws):
    _stl(ws / "small.stl", (40.0, 40.0, 30.0))
    prof = _profile(tmp_path, "tall", printable_height="250")
    got = slicer_server.check_bed_fit("small.stl", machine_profile=prof)
    assert got["ok"] is True and got["reason"] == ""


@pytest.mark.parametrize("case", ["refusal", "no_mesh", "no_bed", "no_width"])
def test_height_checked_is_present_on_every_return(tmp_path, ws, case,
                                                   monkeypatch):
    # A caller reading r["height_checked"] must never meet a KeyError, least
    # of all on the paths that answer nothing.
    if case == "refusal":
        got = slicer_server.check_bed_fit("nothing.stl")   # no printer set up
    elif case == "no_mesh":
        (ws / "empty.stl").write_bytes(b"\0" * 80 + (0).to_bytes(4, "little"))
        got = slicer_server.check_bed_fit(
            "empty.stl", machine_profile=_profile(tmp_path, "p"))
    elif case == "no_bed":
        _stl(ws / "part.stl", (40.0, 40.0, 30.0))
        blank = tmp_path / "blank.json"
        blank.write_text(json.dumps({"type": "machine", "name": "blank"}))
        got = slicer_server.check_bed_fit("part.stl", machine_profile=str(blank))
    else:
        _stl(ws / "part.stl", (40.0, 40.0, 30.0))
        monkeypatch.setattr(machine, "bed", lambda *a, **k: {"z_mm": 250.0})
        got = slicer_server.check_bed_fit(
            "part.stl", machine_profile=_profile(tmp_path, "p"))
    assert got["ok"] is None
    assert got["height_checked"] is False
    assert got["reason"]


def test_a_bed_with_a_height_but_no_width_is_refused_not_a_traceback(
        tmp_path, ws, monkeypatch):
    # A record whose cached bed kept a height after the profile stopped
    # declaring a printable_area merges to {"z_mm": 250.0}: truthy, and with
    # nothing to measure a footprint against. That used to raise KeyError
    # straight out of the tool call.
    _stl(ws / "part.stl", (40.0, 40.0, 30.0))
    monkeypatch.setattr(machine, "bed", lambda *a, **k: {"z_mm": 250.0})
    got = slicer_server.check_bed_fit("part.stl",
                                      machine_profile=_profile(tmp_path, "p"))
    assert got["ok"] is None
    assert "no width or depth" in got["reason"]
    assert got["bed"] == {"z_mm": 250.0}
