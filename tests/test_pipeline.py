"""Orchestration and refusal.

The four refusal tests are pure unit tests. Everything below them drives the
whole pipeline through two real subprocesses — a stand-in OpenSCAD that emits
STLs of a requested size, and a stand-in slicer that emits a real .gcode.3mf
— because the interesting failures here live at those two boundaries, and a
test that mocks them away proves only that the orchestration talks to itself.
"""

import json
import os
import struct
import sys
import zipfile
from pathlib import Path

import pytest

from cadloop import machine, pipeline, slicer_server


def test_run_refuses_without_a_machine(tmp_path, monkeypatch):
    monkeypatch.setenv("CADLOOP_MACHINE", str(tmp_path / "none.json"))
    src = tmp_path / "m.scad"
    src.write_text("cube(10);")
    r = pipeline.run("m.scad", tmp_path)
    assert r["ok"] is None
    assert "setup_printer" in r["reason"]


def test_run_refuses_a_stale_machine(tmp_path, monkeypatch):
    monkeypatch.setenv("CADLOOP_MACHINE", str(tmp_path / "rec.json"))
    machine.save({"schema": machine.SCHEMA, "name": "X",
                  "slicer": {"binary": "/nonexistent", "version": "1"},
                  "profiles": {}, "derived": {}, "fingerprint": {}})
    src = tmp_path / "m.scad"
    src.write_text("cube(10);")
    r = pipeline.run("m.scad", tmp_path)
    assert r["ok"] is None
    assert "stale" in r["reason"] or "no longer" in r["reason"]


def test_run_refuses_a_source_outside_the_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("CADLOOP_MACHINE", str(tmp_path / "none.json"))
    ws = tmp_path / "cad"
    ws.mkdir()
    # A real file, really outside the workspace, so it is the confinement
    # guard that stops this and not the does-this-exist check behind it.
    # Both refuse and both mention the workspace, so the assertion has to
    # pin the escape's own words or it passes with the guard deleted.
    (tmp_path / "real.scad").write_text("cube(10);")
    r = pipeline.run("../real.scad", ws)
    assert r["ok"] is None
    assert "outside the workspace" in r["reason"]
    assert "real.scad" in r["reason"]

    r = pipeline.run("../../etc/passwd", tmp_path)
    assert r["ok"] is None
    assert "outside the workspace" in r["reason"]
    assert "workspace" in r["reason"]


def test_report_is_json_serialisable(tmp_path, monkeypatch):
    monkeypatch.setenv("CADLOOP_MACHINE", str(tmp_path / "none.json"))
    src = tmp_path / "m.scad"
    src.write_text("cube(10);")
    json.loads(json.dumps(pipeline.run("m.scad", tmp_path)))


# --------------------------------------------------------------------
# stand-in binaries
# --------------------------------------------------------------------

# Reads -D part=<n> and writes a binary STL of a box n mm square. A part
# named "empty" gets a valid STL with no triangles, which is what OpenSCAD
# really does when the model's dispatch matches nothing. A part named
# "broken" gets an ERROR on stderr and no file.
FAKE_OPENSCAD = r'''
import re, struct, sys
args = sys.argv[1:]
out = args[args.index("-o") + 1]
define = ""
if "-D" in args:
    define = args[args.index("-D") + 1].split("=", 1)[1].strip('"')
if define == "broken":
    sys.stderr.write("ERROR: Assertion failed\n")
    sys.exit(1)
size = 40.0
m = re.match(r"^-?[\d.]+$", define or "")
if m:
    size = float(define)
elif define == "huge":
    size = 400.0
tris = []
if define != "empty":
    a, b = (0.0, 0.0, 0.0), (size, 0.0, 0.0)
    c, d = (size, size, 0.0), (0.0, size, 0.0)
    top = (0.0, 0.0, 10.0)
    tris = [(a, b, c), (a, c, d), (a, b, top), (b, c, top)]
with open(out, "wb") as fh:
    fh.write(b"\0" * 80 + struct.pack("<I", len(tris)))
    for t in tris:
        fh.write(struct.pack("<3f", 0.0, 0.0, 1.0))
        for v in t:
            fh.write(struct.pack("<3f", *v))
        fh.write(b"\0\0")
print("Top level object is a 3D object:")
'''

# Orca-shaped help, and a .gcode.3mf whose G-code has a real object marker
# and extruding moves. Every knob is an environment variable so a test can
# ask for one specific kind of bad output without a second script:
#   SLICER_OFFSET    shift the moves, to send a plate off the bed
#   SLICER_Z         the height the moves reach, to exercise the Z bound
#   SLICER_FAIL      exit non-zero without writing anything
#   SLICER_NO_MARKER omit the object marker, so the extent is unreadable
#   SLICER_NO_GCODE  write an archive with no .gcode entry in it
#   SLICER_ARGV_LOG  append the argv it was called with to this file
FAKE_SLICER = r'''
import json, os, pathlib, sys, zipfile
a = sys.argv[1:]
if "--help" in a:
    print("--slice arg --load-settings arg --load-filaments arg "
          "--export-3mf arg --arrange arg --orient arg --ensure-on-bed "
          "--allow-newer-file --min-save")
    sys.exit(0)
if "--slice" not in a:
    sys.stderr.write("unknown invocation\n"); sys.exit(2)
log = os.environ.get("SLICER_ARGV_LOG")
if log:
    with open(log, "a") as fh:
        fh.write(json.dumps(a) + "\n")
off = float(os.environ.get("SLICER_OFFSET", "0"))
z_mm = float(os.environ.get("SLICER_Z", "0.2"))
if os.environ.get("SLICER_FAIL"):
    sys.stderr.write("ERROR: slicing failed\n"); sys.exit(1)
out = a[a.index("--export-3mf") + 1]
pathlib.Path(out).parent.mkdir(parents=True, exist_ok=True)
lines = ["; generated by fake", "; layer_height = 0.2", "G28", "M83"]
if not os.environ.get("SLICER_NO_MARKER"):
    lines.append("; printing object part id:0 copy 0")
for x, y in ((20, 20), (60, 20), (60, 60), (20, 60), (20, 20)):
    lines.append("G1 X%.1f Y%.1f Z%.1f E0.5" % (x + off, y + off, z_mm))
with zipfile.ZipFile(out, "w") as z:
    if not os.environ.get("SLICER_NO_GCODE"):
        z.writestr("Metadata/plate_1.gcode", "\n".join(lines) + "\n")
    z.writestr("Metadata/slice_info.config",
               '<?xml version="1.0"?><config><plate>'
               '<metadata key="index" value="1"/>'
               '<metadata key="prediction" value="7412"/>'
               '<filament id="1" type="PLA" used_m="9.94" used_g="29.8"/>'
               '</plate></config>')
    z.writestr("Metadata/project_settings.config",
               json.dumps({"printer_model": "Fake", "layer_height": "0.2"}))
print("slicing finished")
'''


@pytest.fixture
def rig(tmp_path, monkeypatch):
    """A workspace, a fake OpenSCAD, a fake slicer, and a machine record
    fingerprinted over real profile files so staleness() is satisfied."""
    ws = tmp_path / "cad"
    ws.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()

    def script(name, body):
        p = bin_dir / name
        p.write_text("#!" + sys.executable + "\n" + body)
        p.chmod(0o755)
        return p

    openscad = script("openscad", FAKE_OPENSCAD)
    slicer = script("slicer", FAKE_SLICER)

    prof = tmp_path / "profiles"
    prof.mkdir()
    (prof / "machine.json").write_text(json.dumps({
        "type": "machine", "name": "Fake 0.4 nozzle",
        "printable_area": ["0x0", "220x0", "220x220", "0x220"],
        "printable_height": "250", "nozzle_diameter": ["0.4"]}))
    (prof / "process.json").write_text(json.dumps(
        {"type": "process", "name": "0.20mm", "layer_height": "0.2"}))
    (prof / "filament.json").write_text(json.dumps(
        {"type": "filament", "name": "PLA", "filament_type": ["PLA"]}))
    profiles = {"machine": str(prof / "machine.json"),
                "process": str(prof / "process.json"),
                "filament": str(prof / "filament.json")}

    monkeypatch.setenv("CADLOOP_MACHINE", str(tmp_path / "machine.json"))
    monkeypatch.setenv("OPENSCAD_BIN", str(openscad))
    monkeypatch.setenv("SLICER_BIN", str(slicer))
    for knob in ("SLICER_OFFSET", "SLICER_Z", "SLICER_FAIL",
                 "SLICER_NO_MARKER", "SLICER_NO_GCODE", "SLICER_ARGV_LOG"):
        monkeypatch.delenv(knob, raising=False)
    machine.save({
        "schema": machine.SCHEMA, "name": "Fake 0.4 nozzle",
        "slicer": {"family": "fake", "binary": str(slicer), "version": "1.0"},
        "profiles": profiles,
        "derived": {"printer_model": "Fake", "nozzle_mm": 0.4,
                    "filament_type": "PLA",
                    "bed": {"x_mm": 220.0, "y_mm": 220.0, "z_mm": 250.0,
                            "origin": [0.0, 0.0]}},
        "fingerprint": machine.fingerprint(str(slicer), "1.0", profiles)})

    (ws / "m.scad").write_text('part = "all";\ncube(10);\n')
    return ws


def test_a_whole_run_renders_packs_slices_and_proves_the_bed(rig):
    r = pipeline.run("m.scad", rig, ["30", "40"])
    assert r["ok"] is True, r["reason"]
    assert [p["name"] for p in r["parts"]] == ["30", "40"]
    assert all(p["rendered"] and p["fits"] for p in r["parts"])
    assert r["parts"][0]["size_mm"][:2] == [30.0, 30.0]
    assert len(r["plates"]) == 1
    assert r["plates"][0]["on_bed"] is True
    assert r["totals"] == {"plates": 1, "minutes": 124, "filament_m": 9.94}
    # The G-code and the archive are really on disk, under the workspace.
    plate = Path(r["output"]) / "plate_1.gcode"
    assert plate.is_file() and plate.stat().st_size
    assert zipfile.is_zipfile(Path(r["output"]) / "plate_1.gcode.3mf")
    json.loads(json.dumps(r))


def test_no_parts_argument_renders_the_whole_file(rig):
    r = pipeline.run("m.scad", rig)
    assert r["ok"] is True, r["reason"]
    assert [p["name"] for p in r["parts"]] == ["m"]
    assert (Path(r["output"]) / "m.stl").is_file()


def test_a_plate_off_the_bed_fails_the_run(rig, monkeypatch):
    monkeypatch.setenv("SLICER_OFFSET", "200")
    r = pipeline.run("m.scad", rig, ["30"])
    assert r["ok"] is False
    assert "off the bed" in r["reason"]
    assert r["plates"][0]["on_bed"] is False
    assert any("off the bed" in note for note in r["attention"])


def test_a_part_too_big_for_the_bed_is_reported_not_squeezed(rig):
    r = pipeline.run("m.scad", rig, ["huge", "30"])
    assert r["ok"] is False
    assert "does not fit this bed" in r["reason"]
    big = [p for p in r["parts"] if p["name"] == "huge"][0]
    assert big["rendered"] is True and big["fits"] is False
    assert big["plate"] is None
    assert any("huge does not fit" in note for note in r["attention"])
    # and the part that does fit still got sliced
    assert len(r["plates"]) == 1
    assert r["plates"][0]["parts"] == ["30"]


def test_a_part_that_renders_nothing_is_named(rig):
    r = pipeline.run("m.scad", rig, ["empty", "30"])
    assert r["ok"] is False
    assert "empty did not render" in r["reason"]
    assert any("no geometry" in note for note in r["attention"])
    assert r["plates"][0]["parts"] == ["30"]


def test_an_openscad_error_is_a_failure_not_a_crash(rig):
    r = pipeline.run("m.scad", rig, ["broken"])
    assert r["ok"] is False
    assert "broken did not render" in r["reason"]
    assert r["plates"] == []


def test_a_failed_slice_is_a_failure(rig, monkeypatch):
    monkeypatch.setenv("SLICER_FAIL", "1")
    r = pipeline.run("m.scad", rig, ["30"])
    assert r["ok"] is False
    assert "did not slice" in r["reason"]
    assert r["plates"][0]["sliced"] is False
    assert r["plates"][0]["on_bed"] is None


def test_parts_are_packed_onto_as_few_plates_as_fit(rig):
    r = pipeline.run("m.scad", rig, ["100", "100", "100", "100", "100"])
    # Duplicate names render once, and say so.
    assert len(r["parts"]) == 1
    assert any("more than once" in note for note in r["attention"])
    r = pipeline.run("m.scad", rig, ["90", "91", "92", "93", "94"])
    assert r["ok"] is True, r["reason"]
    # 204 mm of usable bed: two per shelf, two shelves, so one plate holds
    # four and the fifth opens a second.
    assert len(r["plates"]) == 2
    assert sum(len(p["parts"]) for p in r["plates"]) == 5
    assert r["totals"]["plates"] == 2


def test_a_part_that_only_fits_turned_is_reported_as_turned(rig, monkeypatch):
    # 120 x 280 mm on a 300 x 150 bed: too deep square, fine on its side.
    # A square bed can never need a turn, so this one is not square, and the
    # fake OpenSCAD only makes squares, so widen the mesh after it renders.
    monkeypatch.setattr(machine, "bed", lambda *a, **k: {
        "x_mm": 300.0, "y_mm": 150.0, "z_mm": 250.0, "origin": [0.0, 0.0]})
    real_render = pipeline._render

    def oblong(binary, source, outdir, workspace, name, define):
        rec = real_render(binary, source, outdir, workspace, name, define)
        if name == "oblong":
            rec["size_mm"] = [120.0, 280.0, 10.0]
        return rec

    monkeypatch.setattr(pipeline, "_render", oblong)
    r = pipeline.run("m.scad", rig, ["oblong"])
    assert r["ok"] is True, r["reason"]
    assert r["arranged"] == ["oblong turned 90 degrees, it does not fit square"]


def test_a_plate_near_the_edge_suggests_a_brim(rig, monkeypatch):
    monkeypatch.setenv("SLICER_OFFSET", "-15")
    r = pipeline.run("m.scad", rig, ["30"])
    assert r["ok"] is True, r["reason"]
    assert any("consider a brim" in note for note in r["attention"])


def test_unreadable_gcode_is_unknown_not_a_failure(rig, monkeypatch):
    def blank(path, bed, margin_mm=0.0):
        return {"ok": None, "reason": "no object marker in this G-code",
                "extent": {}, "bed": bed}

    monkeypatch.setattr(pipeline._gcode, "fits", blank)
    r = pipeline.run("m.scad", rig, ["30"])
    assert r["ok"] is None
    assert "could not be proven on the bed" in r["reason"]
    assert r["plates"][0]["on_bed"] is None


def test_a_slicer_refusal_stops_the_run(rig, monkeypatch):
    monkeypatch.setattr(slicer_server, "slice_model",
                        lambda **kw: {"ok": None, "reason": "no printer set "
                                                            "up, run setup_printer"})
    r = pipeline.run("m.scad", rig, ["30"])
    assert r["ok"] is None
    assert "setup_printer" in r["reason"]
    assert r["plates"] == []


def test_a_bed_with_no_geometry_refuses_before_rendering(rig, monkeypatch):
    monkeypatch.setattr(machine, "bed", lambda *a, **k: {})
    r = pipeline.run("m.scad", rig, ["30"])
    assert r["ok"] is None
    assert "printable area" in r["reason"]
    assert r["parts"] == [] and not (rig / "plates").exists()


def test_an_empty_parts_list_refuses(rig):
    r = pipeline.run("m.scad", rig, [])
    assert r["ok"] is None
    assert "nothing to render" in r["reason"]


def test_a_missing_source_refuses(rig):
    r = pipeline.run("gone.scad", rig)
    assert r["ok"] is None
    assert "gone.scad" in r["reason"]


def test_a_numeric_part_is_passed_to_openscad_as_a_number(rig, monkeypatch):
    seen = []
    real = pipeline._sh
    monkeypatch.setattr(pipeline, "_sh",
                        lambda cmd, t: (seen.append(cmd), real(cmd, t))[1])
    pipeline.run("m.scad", rig, ["36", "ring"])
    defines = [c[c.index("-D") + 1] for c in seen if "-D" in c]
    assert defines == ["part=36", 'part="ring"']


def test_the_run_leaves_the_slicer_workspace_where_it_found_it(rig):
    was = slicer_server.WORKSPACE
    pipeline.run("m.scad", rig, ["30"])
    assert slicer_server.WORKSPACE == was


# --------------------------------------------------------------------
# the bed's third dimension
# --------------------------------------------------------------------

def _reprint_record(tmp_path, mutate_profile=None, mutate_bed=None):
    """Edit the machine profile and/or the record's cached bed, then
    re-fingerprint so the record is current again rather than stale."""
    prof = tmp_path / "profiles" / "machine.json"
    if mutate_profile:
        cfg = json.loads(prof.read_text())
        mutate_profile(cfg)
        prof.write_text(json.dumps(cfg))
    rec = json.loads((tmp_path / "machine.json").read_text())
    if mutate_bed:
        mutate_bed(rec["derived"]["bed"])
    rec["fingerprint"] = machine.fingerprint(rec["slicer"]["binary"],
                                             rec["slicer"]["version"],
                                             rec["profiles"])
    (tmp_path / "machine.json").write_text(json.dumps(rec))


def test_a_height_the_profile_lost_is_still_checked_from_the_record(
        rig, tmp_path, monkeypatch):
    # The live profile no longer declares printable_height, so bed_of()
    # reports z_mm None. The record still remembers 250. Taking the live bed
    # whole would silently turn the Z bound off; merging keeps it on.
    _reprint_record(tmp_path, mutate_profile=lambda c: c.pop("printable_height"))
    assert machine.bed(machine.load())["z_mm"] == 250.0
    monkeypatch.setenv("SLICER_Z", "900")
    r = pipeline.run("m.scad", rig, ["30"])
    assert r["ok"] is False
    assert "off the bed" in r["reason"]
    assert r["plates"][0]["on_bed"] is False
    assert "z reaches 900.0 of 250.0" in r["plates"][0]["reason"]


def test_a_plate_whose_height_cannot_be_checked_is_unknown_not_proven(
        rig, tmp_path, monkeypatch):
    # Nothing anywhere knows how tall this printer is. X and Y are provable
    # and Z is not, and two axes out of three is not the proof A5 promises.
    _reprint_record(tmp_path,
                    mutate_profile=lambda c: c.pop("printable_height"),
                    mutate_bed=lambda b: b.pop("z_mm"))
    assert not machine.bed(machine.load()).get("z_mm")
    monkeypatch.setenv("SLICER_Z", "900")
    r = pipeline.run("m.scad", rig, ["30"])
    assert r["ok"] is None
    assert r["plates"][0]["on_bed"] is None
    assert "no printable height" in r["plates"][0]["reason"]
    assert "z is unchecked" in r["plates"][0]["reason"]
    assert any("could not be proven on the bed" in n for n in r["attention"])


# --------------------------------------------------------------------
# the difference between "no deliverable" and "cannot tell"
# --------------------------------------------------------------------

def test_an_archive_with_no_gcode_in_it_is_a_failure(rig, monkeypatch):
    monkeypatch.setenv("SLICER_NO_GCODE", "1")
    r = pipeline.run("m.scad", rig, ["30"])
    assert r["ok"] is False
    assert "produced no G-code" in r["reason"]
    plate = r["plates"][0]
    assert plate["sliced"] is True and plate["gcode"] is None
    assert plate["on_bed"] is None
    assert any("no G-code came out of the archive" in n for n in r["attention"])


def test_gcode_with_no_object_marker_is_unknown_not_a_failure(rig, monkeypatch):
    # The same on_bed: None as the case above, but the G-code is on disk and
    # the plate may well be fine. Read through the real gcode.extent reader,
    # not a stub, because telling these two apart is the whole point.
    monkeypatch.setenv("SLICER_NO_MARKER", "1")
    r = pipeline.run("m.scad", rig, ["30"])
    assert r["ok"] is None
    plate = r["plates"][0]
    assert plate["sliced"] is True and plate["gcode"] is not None
    assert plate["on_bed"] is None
    assert "no object marker" in plate["reason"]
    assert "could not be proven on the bed" in r["reason"]


# --------------------------------------------------------------------
# a refusal does not erase what was already established
# --------------------------------------------------------------------

def test_a_slicer_refusal_keeps_the_failures_already_found(rig, monkeypatch):
    monkeypatch.setattr(slicer_server, "slice_model",
                        lambda **kw: {"ok": None, "reason": "no printer set "
                                                            "up, run setup_printer"})
    r = pipeline.run("m.scad", rig, ["huge", "broken", "30"])
    assert r["ok"] is None
    assert "setup_printer" in r["reason"]
    # Both of these were checked and found wanting before the slicer was
    # asked anything, and the refusal does not get to demote them.
    assert "broken did not render" in r["reason"]
    assert "huge does not fit this bed" in r["reason"]


# --------------------------------------------------------------------
# what the slicer is actually told
# --------------------------------------------------------------------

def test_the_slicer_is_told_to_arrange_and_not_to_reorient(rig, tmp_path,
                                                           monkeypatch):
    log = tmp_path / "argv.log"
    monkeypatch.setenv("SLICER_ARGV_LOG", str(log))
    r = pipeline.run("m.scad", rig, ["30", "40"])
    assert r["ok"] is True, r["reason"]
    argv = json.loads(log.read_text().splitlines()[0])
    # arrange 1 packs the plate; without it the parts stack at the origin.
    assert argv[argv.index("--arrange") + 1] == "1"
    # orient 0 is A6: the pipeline may choose where a part sits, never how
    # it is turned in the slicer's own opinion. Rotation is the packer's
    # decision alone, and it is reported when it happens.
    assert argv[argv.index("--orient") + 1] == "0"


# --------------------------------------------------------------------
# refusals that had no test
# --------------------------------------------------------------------

def test_no_openscad_is_a_refusal_not_a_failure(rig, tmp_path, monkeypatch):
    monkeypatch.setenv("OPENSCAD_BIN", str(tmp_path / "no-such-openscad"))
    r = pipeline.run("m.scad", rig, ["30"])
    assert r["ok"] is None          # nothing was attempted, so not False
    assert "OpenSCAD" in r["reason"]
    assert r["parts"] == [] and r["plates"] == []
    assert not (rig / "plates").exists()


def test_an_unreadable_machine_variable_says_so(rig, tmp_path, monkeypatch):
    bad = tmp_path / "a-directory.json"
    bad.mkdir()
    monkeypatch.setenv("CADLOOP_MACHINE", str(bad))
    r = pipeline.run("m.scad", rig, ["30"])
    assert r["ok"] is None
    assert "CADLOOP_MACHINE" in r["reason"] and "directory" in r["reason"]


def test_a_corrupt_record_is_not_reported_as_a_missing_one(rig, tmp_path,
                                                           monkeypatch):
    (tmp_path / "machine.json").write_text("{ this is not json")
    r = pipeline.run("m.scad", rig, ["30"])
    assert r["ok"] is None
    assert "cannot be read" in r["reason"]
    # The user made a setup; sending them to make another one is the wrong
    # next action, and "no printer set up yet" says exactly that.
    assert "no printer set up yet" not in r["reason"]


# --------------------------------------------------------------------
# totals
# --------------------------------------------------------------------

def test_totals_count_plates_that_came_out_not_plates_attempted(rig,
                                                                monkeypatch):
    monkeypatch.setenv("SLICER_FAIL", "1")
    r = pipeline.run("m.scad", rig, ["30"])
    assert r["ok"] is False
    assert len(r["plates"]) == 1            # it is still reported, with a reason
    assert r["plates"][0]["sliced"] is False
    assert r["totals"]["plates"] == 0       # but nobody can print it


def test_a_bed_with_a_height_but_no_width_refuses_rather_than_raising(
        rig, monkeypatch):
    # The same shape that used to crash check_bed_fit: a merged bed that is
    # truthy but has nothing to measure a footprint against. run() guards on
    # the dimensions rather than on the dict, so it refuses; this pins that
    # it keeps doing so, and that packing.pack is never reached with it.
    monkeypatch.setattr(machine, "bed", lambda *a, **k: {"z_mm": 250.0})
    r = pipeline.run("m.scad", rig, ["30"])
    assert r["ok"] is None
    assert "printable area" in r["reason"]
    assert r["machine"]["bed"] == {"z_mm": 250.0}
    assert r["parts"] == [] and r["plates"] == []
    assert not (rig / "plates").exists()
