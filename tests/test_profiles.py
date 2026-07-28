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
