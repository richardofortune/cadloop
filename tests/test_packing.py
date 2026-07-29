from cadloop import packing

BED = {"x_mm": 220.0, "y_mm": 220.0, "z_mm": 250.0}


def _names(plate):
    return sorted(p["name"] for p in plate["parts"])


def test_one_small_part_takes_one_plate():
    r = packing.pack([{"name": "a", "w": 40.0, "d": 40.0}], BED)
    assert len(r["plates"]) == 1
    assert _names(r["plates"][0]) == ["a"]


def test_parts_that_fit_together_share_a_plate():
    parts = [{"name": n, "w": 40.0, "d": 40.0} for n in "abcd"]
    r = packing.pack(parts, BED)
    assert len(r["plates"]) == 1


def test_a_part_too_big_for_the_bed_is_unplaceable_not_crammed():
    r = packing.pack([{"name": "huge", "w": 400.0, "d": 400.0}], BED)
    assert r["plates"] == []
    assert r["unplaceable"][0]["name"] == "huge"
    assert "bed" in r["unplaceable"][0]["reason"]


# A square bed cannot exercise rotation: with symmetric margins the usable
# region is square too, and a box that does not fit a square does not fit it
# turned. Real beds are often not square — a Prusa MK3 is 250 x 210 — and
# that is where turning a part is the difference between printing and not.
TALL_BED = {"x_mm": 250.0, "y_mm": 210.0, "z_mm": 250.0}


def test_rotation_is_used_when_a_part_only_fits_turned():
    # usable is 234 x 194; 100 x 200 is 6 mm too deep square, and fits turned
    r = packing.pack([{"name": "long", "w": 100.0, "d": 200.0}], TALL_BED)
    assert r["plates"], r["unplaceable"]
    assert r["plates"][0]["parts"][0]["rotated"] is True


def test_rotation_is_not_used_when_a_part_already_fits():
    r = packing.pack([{"name": "square", "w": 40.0, "d": 60.0}], BED)
    assert r["plates"][0]["parts"][0]["rotated"] is False, \
        "rotating a part that fits changes its layer direction for nothing"


def test_placements_do_not_overlap():
    # pack() stores "w"/"d" as the on-bed footprint *after* rotation is
    # already applied, so the box here must use them as-is. Re-swapping
    # them "if rotated" double-flips a rotated part back to its pre-rotation
    # shape -- wrong, and invisible on square parts (w == d) alone, which is
    # why a non-square rotated part ("long") is included: on the square BED
    # it can never rotate (see test_rotation_is_used_when_a_part_only_fits_
    # turned), so TALL_BED is used here too.
    parts = [{"name": str(i), "w": 60.0, "d": 60.0} for i in range(9)] + \
        [{"name": "long", "w": 100.0, "d": 200.0}]
    r = packing.pack(parts, TALL_BED)
    assert any(p["rotated"] for plate in r["plates"] for p in plate["parts"]), \
        "this test is only meaningful if a rotated part is actually placed"
    for plate in r["plates"]:
        boxes = []
        for p in plate["parts"]:
            boxes.append((p["x"], p["y"], p["x"] + p["w"], p["y"] + p["d"]))
        for i, a in enumerate(boxes):
            for b in boxes[i + 1:]:
                assert a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1], \
                    f"overlap between {a} and {b}"


def test_everything_placed_is_inside_the_bed():
    # See test_placements_do_not_overlap: "w"/"d" are already the on-bed
    # footprint, and a rotated non-square part is included so the bounds
    # check is actually exercised for a rotated box, not just squares.
    parts = [{"name": str(i), "w": 70.0, "d": 70.0} for i in range(6)] + \
        [{"name": "long", "w": 100.0, "d": 200.0}]
    r = packing.pack(parts, TALL_BED)
    assert any(p["rotated"] for plate in r["plates"] for p in plate["parts"]), \
        "this test is only meaningful if a rotated part is actually placed"
    for plate in r["plates"]:
        for p in plate["parts"]:
            assert p["x"] >= 0 and p["y"] >= 0
            assert p["x"] + p["w"] <= TALL_BED["x_mm"]
            assert p["y"] + p["d"] <= TALL_BED["y_mm"]


def test_every_part_is_placed_or_reported_never_vanished():
    # Reviewer finding: a mutation that stops pack() from ever opening a
    # second plate leaves the suite green, because the earlier tests all
    # fit on one plate. 20 parts at 90x90 need 5 plates on this bed (4 per
    # plate: two shelves of two), so any dropped part is caught here.
    parts = [{"name": str(i), "w": 90.0, "d": 90.0} for i in range(20)]
    r = packing.pack(parts, BED)
    placed_names = [p["name"] for plate in r["plates"] for p in plate["parts"]]
    unplaceable_names = [u["name"] for u in r["unplaceable"]]
    assert sorted(placed_names + unplaceable_names) == sorted(p["name"] for p in parts), \
        "every input part must appear exactly once across plates and unplaceable"
    assert len(placed_names) + len(unplaceable_names) == len(parts)
    assert len(r["plates"]) > 1, "fixture should need several plates"


def test_gap_forces_a_new_shelf_when_widths_sum_to_exactly_usable_width():
    # Usable width is 204 (220 - 2*8 margin). Two 102-wide parts sum to
    # exactly that, so without counting the 3mm gap they would (wrongly)
    # appear to fit side by side on one shelf.
    parts = [{"name": "x", "w": 102.0, "d": 50.0}, {"name": "y", "w": 102.0, "d": 50.0}]
    r = packing.pack(parts, BED)
    assert len(r["plates"]) == 1
    a, b = sorted(r["plates"][0]["parts"], key=lambda p: p["name"])
    assert a["y"] != b["y"], \
        "parts should need separate shelves once the gap between them is counted"


def test_parts_sharing_a_shelf_are_separated_by_the_gap():
    """Touching is not fitting.

    Two parts flush against each other print as one fused object, and
    test_placements_do_not_overlap cannot see it: a shared edge is not an
    overlap under any of its four comparisons. The gap has to be paid where
    the part is actually placed, not only in the check that decides whether
    it fits — dropping it from the placement leaves that whole test green.
    """
    parts = [{"name": "a", "w": 60.0, "d": 40.0},
             {"name": "b", "w": 60.0, "d": 40.0}]
    r = packing.pack(parts, BED)
    assert len(r["plates"]) == 1
    a, b = sorted(r["plates"][0]["parts"], key=lambda p: p["x"])
    assert a["y"] == b["y"], "fixture is only meaningful if they share a shelf"
    assert b["x"] - (a["x"] + a["w"]) >= 3.0 - 1e-9, \
        "the second part on a shelf must clear the first by the gap"


def test_shelf_depth_check_is_load_bearing():
    # pack() sorts every part by depth descending before placing any of
    # them, so through the public API a shelf already on a plate can never
    # be shallower than the part currently being considered -- the depth
    # check in _place can never actually fire as a blocker that way (this
    # was verified empirically: 200k adversarial-fuzzed inputs produced
    # zero behavioural difference with the check deleted). To make the
    # check itself load-bearing, this drives _place directly -- the same
    # function pack() calls -- offering a part to a shelf shallower or
    # deeper than itself.
    uw, ud, gap, margin = 204.0, 204.0, 3.0, 8.0

    # A part deeper than an existing (shallow) shelf must not join it --
    # joining would let it overhang into whatever shelf comes next.
    plate = {"parts": [], "shelves": [{"y": 8.0, "depth": 30.0, "used": 40.0}]}
    deep = {"name": "deep", "w": 20.0, "d": 80.0, "rotated": False}
    assert packing._place(plate, deep, uw, ud, gap, margin) is True
    assert len(plate["shelves"]) == 2, \
        "a part deeper than the existing shelf must open a new shelf, not join it"

    # A part shallower than an existing (deep) shelf may safely share it.
    plate2 = {"parts": [], "shelves": [{"y": 8.0, "depth": 80.0, "used": 40.0}]}
    shallow = {"name": "shallow", "w": 20.0, "d": 30.0, "rotated": False}
    assert packing._place(plate2, shallow, uw, ud, gap, margin) is True
    assert len(plate2["shelves"]) == 1, \
        "a shallow part should share the existing deep shelf, not open a new one"


def test_bed_with_no_dimensions_is_reported_not_raised():
    # profiles.bed_of() can genuinely return {} for a machine profile with
    # no printable-area geometry. pack() must not raise, and must not
    # silently return no plates -- every part is reported unplaceable,
    # naming the missing bed.
    r = packing.pack([{"name": "a", "w": 40.0, "d": 40.0}], {})
    assert r["plates"] == []
    assert r["unplaceable"][0]["name"] == "a"
    assert "bed" in r["unplaceable"][0]["reason"]


def test_half_a_bed_is_reported_not_raised():
    """One dimension is not a bed either.

    An empty bed was tested; a bed with x and no y was not, and that is the
    shape a machine profile really produces when printable_area parses far
    enough to give one axis. The guard has to ask for both, because either
    one missing means _usable() reaches for a key that is not there."""
    for bed in ({"x_mm": 220.0}, {"y_mm": 220.0},
                {"x_mm": 220.0, "z_mm": 250.0}):
        r = packing.pack([{"name": "a", "w": 40.0, "d": 40.0}], bed)
        assert r["plates"] == [], bed
        assert r["unplaceable"][0]["name"] == "a", bed
        assert "bed" in r["unplaceable"][0]["reason"], bed


def test_nothing_in_is_nothing_out():
    r = packing.pack([], BED)
    assert r == {"plates": [], "unplaceable": []}


def test_result_is_json_serialisable():
    import json
    r = packing.pack([{"name": "a", "w": 40.0, "d": 40.0}], BED)
    json.loads(json.dumps(r))
