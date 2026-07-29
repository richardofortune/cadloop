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
    parts = [{"name": str(i), "w": 60.0, "d": 60.0} for i in range(9)]
    r = packing.pack(parts, BED)
    for plate in r["plates"]:
        boxes = []
        for p in plate["parts"]:
            w, d = (p["d"], p["w"]) if p["rotated"] else (p["w"], p["d"])
            boxes.append((p["x"], p["y"], p["x"] + w, p["y"] + d))
        for i, a in enumerate(boxes):
            for b in boxes[i + 1:]:
                assert a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1], \
                    f"overlap between {a} and {b}"


def test_everything_placed_is_inside_the_bed():
    parts = [{"name": str(i), "w": 70.0, "d": 70.0} for i in range(6)]
    r = packing.pack(parts, BED)
    for plate in r["plates"]:
        for p in plate["parts"]:
            w, d = (p["d"], p["w"]) if p["rotated"] else (p["w"], p["d"])
            assert p["x"] >= 0 and p["y"] >= 0
            assert p["x"] + w <= BED["x_mm"]
            assert p["y"] + d <= BED["y_mm"]


def test_nothing_in_is_nothing_out():
    r = packing.pack([], BED)
    assert r == {"plates": [], "unplaceable": []}


def test_result_is_json_serialisable():
    import json
    r = packing.pack([{"name": "a", "w": 40.0, "d": 40.0}], BED)
    json.loads(json.dumps(r))
