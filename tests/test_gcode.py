from pathlib import Path

import pytest

from cadloop import gcode

# The shape a real Creality start macro has: a prime line that extrudes at
# negative X on purpose, then the object, then a present-print move to the
# far edge of the bed. Only the middle section is the part.
SAMPLE = """; HEADER_BLOCK_START
; layer_height = 0.2
; printer_model = Creality Ender-3 V3 SE
G28 ;Home
G92 E0
G1 X-2.1 Y20 Z0.28 F5000.0 ;Move to start position
G1 X-2.1 Y145.0 Z0.28 F1500.0 E15 ;Draw the first line
G1 X-2.4 Y145.0 Z0.28 F5000.0 ;Move to side a little
G1 X-2.4 Y20 Z0.28 F1500.0 E30 ;Draw the second line
G92 E0
M83
; printing object part.stl id:0 copy 0
G1 X30 Y30 F9000
G1 Z0.2
G1 X60 Y30 E.4
G1 X60 Y70 E.4
G1 X30 Y70 E.4
G1 X30 Y30 E.4
G1 E-1.2 F2400
G1 X0 Y220 ;Present print
M104 S0 ;Turn-off hotend
"""


@pytest.fixture
def sample(tmp_path):
    p = tmp_path / "part.gcode"
    p.write_text(SAMPLE)
    return p


def test_extent_ignores_the_prime_line(sample):
    e = gcode.extent(sample)
    assert e["x"][0] == 30.0, "the prime line at X-2.4 is not part of the part"


def test_extent_ignores_the_present_print_move(sample):
    e = gcode.extent(sample)
    assert e["y"][1] == 70.0, "the end macro's move to Y220 is not the part"


def test_extent_counts_only_extruding_moves(sample):
    e = gcode.extent(sample)
    assert e["moves"] == 4


def test_extent_reports_z(sample):
    assert gcode.extent(sample)["z_max"] == 0.2


def test_extent_of_a_file_with_no_object_marker_is_unknown(tmp_path):
    p = tmp_path / "empty.gcode"
    p.write_text("G28 ;Home\nG1 X10 Y10 E1\n")
    e = gcode.extent(p)
    assert e["ok"] is False


def test_header_reads_the_semicolon_block(sample):
    h = gcode.header(sample)
    assert h["layer_height"] == "0.2"
    assert h["printer_model"] == "Creality Ender-3 V3 SE"


def test_fits_passes_a_part_inside_the_bed(sample):
    r = gcode.fits(sample, {"x_mm": 220.0, "y_mm": 220.0, "z_mm": 250.0})
    assert r["ok"] is True


def test_fits_fails_a_part_over_the_edge(sample):
    r = gcode.fits(sample, {"x_mm": 50.0, "y_mm": 50.0, "z_mm": 250.0})
    assert r["ok"] is False
    assert "60.0" in r["reason"]


def test_fits_is_unknown_without_a_bed(sample):
    r = gcode.fits(sample, {})
    assert r["ok"] is None
    assert "bed" in r["reason"]
