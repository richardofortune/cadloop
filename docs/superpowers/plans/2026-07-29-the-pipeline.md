# The Pipeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn forty tool calls into one: `make_printable("model.scad")` renders every part, verifies them, packs what fits onto plates, slices, proves the G-code is on the bed, and reports what it did in under thirty lines.

**Architecture:** Three new modules under `src/cadloop/` — `gcode.py` (read a finished G-code file and say where it prints), `packing.py` (parts and a bed in, plates out), `pipeline.py` (the orchestration and the report). `slicer_server.py` gains one thin MCP tool and nothing else; it is already 727 lines. Task 1 first promotes *install* to a real concept in `profiles.py`, because `machine.py` currently carries thirteen call sites compensating for its absence and the packer must not inherit that.

**Tech Stack:** Python 3.10+, `mcp` (FastMCP, pinned `<2`), `pytest`, existing `tests/smoke.py` for end-to-end MCP coverage.

## Global Constraints

- Python floor `>=3.10`. No `match`, no `tomllib`, no `Self`. `X | None` annotations are fine.
- Dependencies stay `mcp>=1.2,<2`; `shapely>=2.0` under `verify`; `pytest>=8` under `dev`. Add nothing else — in particular, no packing or geometry library.
- Every MCP tool return value must be JSON-serialisable — no Path objects, no sets, no dataclasses.
- Unknown is a third answer. A check that cannot conclude returns `ok: None` with a `reason`, never `False`.
- A stale machine record refuses the call. It is never used and never silently repaired.
- **The pipeline may rearrange parts but never edits geometry.** Plate and position are applied freely. **Rotation is applied only where a part would not otherwise fit**, and is reported. Slicing settings that are necessary are applied and reported; ones that merely improve are reported only.
- Acceptance criteria: `docs/superpowers/specs/2026-07-28-acceptance-criteria.md`. This landing delivers **A4** (≤3 tool calls), **A5** (proven on the bed), **A8** (one-screen report), under **A6** (never edits the model). A choice that moves none of A1–A8 is an implementation detail: pick the better option and record it in the commit.
- Spec: `docs/superpowers/specs/2026-07-28-machine-aware-pipeline-design.md`

---

### Task 1: Give `profiles` an install, and take the juggling out of `machine`

`machine.py` has thirteen call sites doing root arithmetic — `_root_of`, `_under`, `_match_in`, `_named`, `_rank` — because the resolver models profiles as a flat name→path index over loose roots, when reality is *installs*: a vendor tree with its own profile namespace. Every fix in the last review compensated for that. The packer needs the bed and the nozzle, not this.

This task is a refactor with no behaviour change. The full suite is the guard.

**Files:**
- Modify: `src/cadloop/profiles.py`
- Modify: `src/cadloop/machine.py`
- Modify: `tests/test_profiles.py`, `tests/test_machine.py`

**Interfaces:**
- Consumes: existing `profile_roots()`, `root_of()`, `root_profiles()`, `classify()`.
- Produces: `installs() -> list[dict]` where each is `{"root": str, "name": str}` (`name` is the install directory's own name, for reports); `install_of(path) -> dict | None`; `profiles_in(install: dict, kind: str, needle: str | None = None) -> list[dict]` returning `[{"name","path","kind"}]` sorted by name. All JSON-serialisable, all `str` paths.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_profiles.py`:

```python
def test_installs_are_the_most_specific_roots(tmp_path, monkeypatch):
    apps, orca, crea = _two_roots(tmp_path, monkeypatch, nest=True)
    found = {i["root"] for i in profiles.installs()}
    assert str(orca) in found and str(crea) in found


def test_installs_are_ordered_most_specific_first(tmp_path, monkeypatch):
    apps, orca, crea = _two_roots(tmp_path, monkeypatch, nest=True)
    roots = [i["root"] for i in profiles.installs()]
    assert roots.index(str(orca)) < roots.index(str(apps))


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
```

`_two_roots(tmp_path, monkeypatch, nest=False)` already exists at
`tests/test_profiles.py:22`. It builds `Applications/orca` and
`Applications/crea`, each holding a machine profile named "Printer X", points
`SLICER_PROFILE_DIRS` at them, and returns `(apps, orca, crea)`. With
`nest=True` it also configures the wrapping `Applications` directory as a
root — which is what `SLICER_PROFILE_DIRS=/Applications` amounts to, and the
shape of the Critical finding from the previous landing. `json` and `os` are
already imported in that file. Do not write a new fixture.

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/pytest tests/test_profiles.py -q -k "install or profiles_in"`
Expected: FAIL, `AttributeError: module 'cadloop.profiles' has no attribute 'installs'`

- [ ] **Step 3: Implement**

Add to `src/cadloop/profiles.py`:

```python
def installs() -> list[dict[str, Any]]:
    """The profile trees on this machine, most specific first.

    A root that contains another root is not an install in its own right for
    anything the deeper one holds: pointing SLICER_PROFILE_DIRS at
    /Applications must not make two vendors look like one place."""
    roots = sorted((str(r) for r in profile_roots()), key=len, reverse=True)
    return [{"root": r, "name": Path(r).name} for r in roots]


def install_of(path: str | Path) -> dict[str, Any] | None:
    """The install holding this profile, or None if none does."""
    root = root_of(Path(path))
    if root is None:
        return None
    return {"root": str(root), "name": Path(root).name}


def profiles_in(install: dict[str, Any], kind: str,
                needle: str | None = None) -> list[dict[str, Any]]:
    """Profiles of one kind belonging to one install, by name.

    Reads the install's own files rather than the flattened name index,
    which keeps only one path per name and so hides a second vendor's copy."""
    out = []
    for p in root_profiles(Path(install["root"])):
        rec = classify(p)
        if not rec or rec["kind"] != kind:
            continue
        if needle and needle.lower() not in rec["name"].lower():
            continue
        out.append({"name": rec["name"], "path": str(p), "kind": kind})
    return sorted(out, key=lambda r: r["name"])
```

Then in `src/cadloop/machine.py`, replace the bodies of `_root_of`, `_under`, `_match_in` and `_named` with calls to `install_of` and `profiles_in`, keeping their names and signatures so `resolve()` is untouched. Delete any that become one-line pass-throughs and inline them.

- [ ] **Step 4: Run the whole suite**

Run: `.venv/bin/pytest -q && .venv/bin/python tests/smoke.py`
Expected: all pass, smoke prints "all checks passed". Any behavioural change here is a bug in the refactor.

- [ ] **Step 5: Verify against the real machine**

Run:

```bash
rm -f /tmp/t1.json
CADLOOP_MACHINE=/tmp/t1.json SLICER_WORKSPACE="$HOME/cad" .venv/bin/python -c "
import sys; sys.path.insert(0,'src')
from pathlib import Path
from cadloop.slicer_server import setup_printer
s = setup_printer()
print('ok:', s['ok'])
print('installs:', {str(Path(p).parents[1]) for p in s['machine']['profiles'].values()})"
```

Expected: `ok: True` and exactly one install path. This is the invariant the previous landing's Critical finding was about; the refactor must not lose it.

- [ ] **Step 6: Commit**

```bash
git add src/cadloop/profiles.py src/cadloop/machine.py tests/test_profiles.py tests/test_machine.py
git commit -m "Give profiles an install, and take the root arithmetic out of machine

Thirteen call sites in machine.py were doing root arithmetic because the
resolver modelled profiles as a flat index over loose roots rather than as
installs. The packer needs a bed and a nozzle, not that."
```

---

### Task 2: Read a finished G-code file and say where it prints

A5 requires the pipeline to prove its own output is on the bed. I have written this by hand twice and got it wrong the first time both times: counting every move includes the vendor's prime line (which extrudes at X-2.4, off the bed by design) and the end macro's *present print* move to Y220. Only extruding moves inside the object body count.

**Files:**
- Create: `src/cadloop/gcode.py`
- Create: `tests/test_gcode.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `extent(path: str | Path) -> dict` returning `{"ok": bool, "x": [min, max], "y": [min, max], "z_max": float, "moves": int}`; `header(path) -> dict[str, str]`; `fits(path, bed: dict, margin_mm: float = 0.0) -> dict` returning `{"ok": bool | None, "reason": str, "extent": dict, "bed": dict}`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_gcode.py`:

```python
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
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/pytest tests/test_gcode.py -q`
Expected: FAIL, `ModuleNotFoundError: No module named 'cadloop.gcode'`

- [ ] **Step 3: Implement**

Create `src/cadloop/gcode.py`:

```python
"""Read a finished G-code file and say where it actually prints.

The naive reading — every G0/G1 in the file — is wrong twice over, and
wrong in a way that looks right. A Creality start macro draws its prime
line at X-2.4, deliberately off the side of the bed, and the end macro
runs the bed out to Y220 to hand you the part. Neither is the object.
Only extruding moves after the first object marker count.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

_XYZ = re.compile(r"([XYZ])(-?\d+\.?\d*)")
_HEADER = re.compile(r"^;\s*([a-z_][a-z0-9_ ]*?)\s*=\s*(.+?)\s*$", re.I)
# Every Orca-family slicer writes this before the first object it prints.
_OBJECT = "; printing object"


def header(path: str | Path) -> dict[str, str]:
    """The `; key = value` block slicers write at the top."""
    out: dict[str, str] = {}
    with open(path, errors="replace") as fh:
        for line in fh:
            if not line.startswith(";"):
                if out:
                    break
                continue
            m = _HEADER.match(line)
            if m:
                out[m.group(1).strip()] = m.group(2).strip()
    return out


def extent(path: str | Path) -> dict[str, Any]:
    """Where the object prints: the box its extruding moves occupy.

    ok is False when the file has no object marker, which means either it
    is not slicer output or the slicer used a dialect we do not know. An
    extent nobody can vouch for is worse than none."""
    xs: list[float] = []
    ys: list[float] = []
    zs: list[float] = []
    x = y = z = None
    started = False
    with open(path, errors="replace") as fh:
        for line in fh:
            if line.startswith(_OBJECT):
                started = True
                continue
            if not started or not line.startswith(("G0", "G1")):
                continue
            for axis, value in _XYZ.findall(line):
                v = float(value)
                if axis == "X":
                    x = v
                elif axis == "Y":
                    y = v
                else:
                    z = v
            # " E" with no minus: laying plastic, not retracting or travelling
            if " E" in line and "E-" not in line and x is not None and y is not None:
                xs.append(x)
                ys.append(y)
                if z is not None:
                    zs.append(z)
    if not xs:
        return {"ok": False, "x": [], "y": [], "z_max": 0.0, "moves": 0}
    return {"ok": True, "x": [min(xs), max(xs)], "y": [min(ys), max(ys)],
            "z_max": max(zs) if zs else 0.0, "moves": len(xs)}


def fits(path: str | Path, bed: dict[str, Any],
         margin_mm: float = 0.0) -> dict[str, Any]:
    """Whether every printed feature lands inside the machine's bed."""
    e = extent(path)
    if not e["ok"]:
        return {"ok": None, "reason": "no object marker in this G-code, so "
                                      "there is nothing to measure",
                "extent": e, "bed": bed}
    if not bed.get("x_mm") or not bed.get("y_mm"):
        return {"ok": None, "reason": "no bed to compare against", "extent": e,
                "bed": bed}
    bad = []
    if e["x"][0] < margin_mm:
        bad.append(f"x starts at {e['x'][0]}")
    if e["x"][1] > bed["x_mm"] - margin_mm:
        bad.append(f"x reaches {e['x'][1]} of {bed['x_mm']}")
    if e["y"][0] < margin_mm:
        bad.append(f"y starts at {e['y'][0]}")
    if e["y"][1] > bed["y_mm"] - margin_mm:
        bad.append(f"y reaches {e['y'][1]} of {bed['y_mm']}")
    if bed.get("z_mm") and e["z_max"] > bed["z_mm"]:
        bad.append(f"z reaches {e['z_max']} of {bed['z_mm']}")
    return {"ok": not bad, "reason": "; ".join(bad), "extent": e, "bed": bed}
```

- [ ] **Step 4: Run to verify they pass**

Run: `.venv/bin/pytest tests/test_gcode.py -q`
Expected: PASS

- [ ] **Step 5: Verify against a real sliced file**

`~/cad/plates_v3se/plate1_ring.gcode` exists from an earlier session. If it does not, skip this step and say so in the report.

```bash
.venv/bin/python -c "
import sys; sys.path.insert(0,'src')
from cadloop import gcode
e = gcode.extent('$HOME/cad/plates_v3se/plate1_ring.gcode')
print('extent:', e['x'], e['y'], e['moves'], 'moves')
print('fits  :', gcode.fits('$HOME/cad/plates_v3se/plate1_ring.gcode',
                            {'x_mm':220.0,'y_mm':220.0,'z_mm':250.0})['ok'])"
```

Expected: x and y both roughly 26.5 to 193.5, `fits` True. A naive reader gives x starting at -2.4 and y reaching 220; if you see those numbers the object-marker gate is not working.

- [ ] **Step 6: Commit**

```bash
git add src/cadloop/gcode.py tests/test_gcode.py
git commit -m "Read a finished G-code file and say where it prints

The naive reading is wrong twice and looks right: the prime line extrudes
at X-2.4 off the bed on purpose, and the end macro runs the bed out to
Y220. Only extruding moves after the first object marker are the part."
```

---

### Task 3: Pack parts onto plates

**Files:**
- Create: `src/cadloop/packing.py`
- Create: `tests/test_packing.py`

**Interfaces:**
- Consumes: nothing (takes footprints and a bed as plain data).
- Produces: `pack(parts: list[dict], bed: dict, gap_mm: float = 3.0, margin_mm: float = 8.0) -> dict` where each part is `{"name": str, "w": float, "d": float}` and the return is `{"plates": [{"parts": [{"name","x","y","rotated"}]}], "unplaceable": [{"name","reason"}]}`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_packing.py`:

```python
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


def test_rotation_is_used_when_a_part_only_fits_turned():
    # 210 x 30 does not fit in 204 of usable width, but does turned
    r = packing.pack([{"name": "long", "w": 210.0, "d": 30.0}], BED)
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
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/pytest tests/test_packing.py -q`
Expected: FAIL, `ModuleNotFoundError: No module named 'cadloop.packing'`

- [ ] **Step 3: Implement**

Create `src/cadloop/packing.py`. A shelf packer: sort by depth descending, lay parts left to right on a shelf, start a new shelf when the row is full, start a new plate when the shelves are.

```python
"""Put parts on plates.

Bounding boxes, not outlines. A gear's box wastes the corners, but a packer
that reasons about outlines needs the outlines, and the parts are STLs by
the time we get here. Wasting a corner costs bed space; getting an outline
wrong costs a collision.

Rotation is deliberately grudging. Which plate a part lands on and where it
sits do not change the part; turning it ninety degrees turns the layers
relative to the geometry, and a gear rotated for tighter packing is a gear
weaker in a direction nobody chose. So a part is turned only when it does
not otherwise fit.
"""

from __future__ import annotations

from typing import Any


def _usable(bed: dict[str, Any], margin_mm: float) -> tuple[float, float]:
    return (float(bed["x_mm"]) - 2 * margin_mm,
            float(bed["y_mm"]) - 2 * margin_mm)


def pack(parts: list[dict[str, Any]], bed: dict[str, Any],
         gap_mm: float = 3.0, margin_mm: float = 8.0) -> dict[str, Any]:
    """Lay parts out on as few plates as a shelf packer manages.

    Returns placements in bed coordinates. A part larger than the bed in
    both orientations is unplaceable and is reported, never squeezed."""
    if not parts:
        return {"plates": [], "unplaceable": []}
    uw, ud = _usable(bed, margin_mm)

    todo, unplaceable = [], []
    for p in parts:
        w, d = float(p["w"]), float(p["d"])
        if w <= uw and d <= ud:
            todo.append({**p, "w": w, "d": d, "rotated": False})
        elif d <= uw and w <= ud:
            todo.append({**p, "w": d, "d": w, "rotated": True})
        else:
            unplaceable.append({"name": p["name"],
                                "reason": f"{w:.1f} x {d:.1f} mm does not fit "
                                          f"a {bed['x_mm']:.0f} x "
                                          f"{bed['y_mm']:.0f} mm bed either way"})

    todo.sort(key=lambda p: (-p["d"], -p["w"]))
    plates: list[dict[str, Any]] = []
    for part in todo:
        for plate in plates:
            if _place(plate, part, uw, ud, gap_mm, margin_mm):
                break
        else:
            plate = {"parts": [], "shelves": []}
            _place(plate, part, uw, ud, gap_mm, margin_mm)
            plates.append(plate)
    for plate in plates:
        del plate["shelves"]
    return {"plates": plates, "unplaceable": unplaceable}


def _place(plate: dict[str, Any], part: dict[str, Any], uw: float, ud: float,
           gap: float, margin: float) -> bool:
    """Put a part on an existing shelf, or open a new one. False if neither."""
    for shelf in plate["shelves"]:
        if shelf["used"] + gap + part["w"] <= uw and part["d"] <= shelf["depth"]:
            x = margin + shelf["used"] + (gap if shelf["parts"] else 0)
            plate["parts"].append({"name": part["name"], "x": round(x, 3),
                                   "y": round(shelf["y"], 3),
                                   "rotated": part["rotated"],
                                   "w": part["w"], "d": part["d"]})
            shelf["used"] = x - margin + part["w"]
            shelf["parts"] += 1
            return True
    top = plate["shelves"][-1] if plate["shelves"] else None
    y = (top["y"] + top["depth"] + gap) if top else margin
    if y - margin + part["d"] > ud:
        return False
    plate["shelves"].append({"y": y, "depth": part["d"],
                             "used": part["w"], "parts": 1})
    plate["parts"].append({"name": part["name"], "x": round(margin, 3),
                           "y": round(y, 3), "rotated": part["rotated"],
                           "w": part["w"], "d": part["d"]})
    return True
```

- [ ] **Step 4: Run to verify they pass**

Run: `.venv/bin/pytest tests/test_packing.py -q`
Expected: PASS. If `test_placements_do_not_overlap` fails, the shelf bookkeeping is wrong — fix the packer, never the test.

- [ ] **Step 5: Commit**

```bash
git add src/cadloop/packing.py tests/test_packing.py
git commit -m "Pack parts onto plates, turning one only when it must

Bounding boxes: wasting a gear's corners costs bed space, getting an
outline wrong costs a collision. Rotation is grudging, because turning a
part turns its layers relative to its geometry and a gear rotated for
packing is weaker in a direction nobody chose."
```

---

### Task 4: `make_printable`

One call from `.scad` to plates that will print. This is A4.

**Files:**
- Create: `src/cadloop/pipeline.py`
- Modify: `src/cadloop/slicer_server.py` (one thin tool, nothing else — it is already 727 lines)
- Create: `tests/test_pipeline.py`

**Interfaces:**
- Consumes: `machine.load`/`staleness`, `packing.pack`, `gcode.fits`, `common.measure_stl`, and the OpenSCAD binary via `openscad_server._binary`.
- Produces: `pipeline.run(source: str, workspace: Path, parts: list[str] | None = None) -> dict` returning the report described in Task 5; and the MCP tool `make_printable(source: str, parts: list[str] | None = None) -> dict`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_pipeline.py`. This tests orchestration and refusal, not slicing — the slicing path is covered end to end in `tests/smoke.py` against the mock slicer in Task 5.

```python
import json
from pathlib import Path

import pytest

from cadloop import pipeline


def test_run_refuses_without_a_machine(tmp_path, monkeypatch):
    monkeypatch.setenv("CADLOOP_MACHINE", str(tmp_path / "none.json"))
    src = tmp_path / "m.scad"
    src.write_text("cube(10);")
    r = pipeline.run("m.scad", tmp_path)
    assert r["ok"] is None
    assert "setup_printer" in r["reason"]


def test_run_refuses_a_stale_machine(tmp_path, monkeypatch):
    from cadloop import machine
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
    r = pipeline.run("../../etc/passwd", tmp_path)
    assert r["ok"] is None
    assert "workspace" in r["reason"]


def test_report_is_json_serialisable(tmp_path, monkeypatch):
    monkeypatch.setenv("CADLOOP_MACHINE", str(tmp_path / "none.json"))
    src = tmp_path / "m.scad"
    src.write_text("cube(10);")
    json.loads(json.dumps(pipeline.run("m.scad", tmp_path)))
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/pytest tests/test_pipeline.py -q`
Expected: FAIL, `ModuleNotFoundError: No module named 'cadloop.pipeline'`

- [ ] **Step 3: Implement**

Create `src/cadloop/pipeline.py` with `run()` performing, in order, and stopping at the first refusal:

1. `safe_path(workspace, source)` — refuse anything outside the workspace, reason names it
2. `machine.load()`; refuse with `ok: None` if absent (naming `setup_printer`) or if `staleness()` returns a reason
3. Render each part: if `parts` is None, render the whole source once; otherwise `-D part="<name>"` per entry. Collect `{"name", "stl", "size_mm", "volume_mm3"}` via `measure_stl`
4. `packing.pack(...)` with `w`/`d` from each part's `size_mm[0]`/`size_mm[1]`, and the bed from `rec["derived"]["bed"]`
5. Slice each plate with `slice_model`'s underlying call, passing every part of that plate as models with `arrange=1`
6. `gcode.fits(...)` on each extracted `.gcode` against the bed; a plate whose G-code leaves the bed is a failure of the pipeline, reported loudly, not a warning
7. Assemble the report (Task 5)

Add to `src/cadloop/slicer_server.py`, near the other tools:

```python
@mcp.tool()
def make_printable(source: str, parts: list[str] | None = None) -> dict[str, Any]:
    """Take a model to files this printer can run, in one call.

    Renders each part, checks it against the bed, packs what fits onto as
    few plates as it can, slices them, and proves every printed feature
    lands on the bed. Reports what it arranged for you and what wants a
    human. Never edits the model: a part that cannot print as designed is
    reported, not altered."""
    from . import pipeline
    return pipeline.run(source, WORKSPACE, parts)
```

- [ ] **Step 4: Run to verify they pass**

Run: `.venv/bin/pytest tests/test_pipeline.py -q && .venv/bin/pytest -q`
Expected: PASS, and the full suite still green.

- [ ] **Step 5: Commit**

```bash
git add src/cadloop/pipeline.py src/cadloop/slicer_server.py tests/test_pipeline.py
git commit -m "make_printable: one call from a model to plates that will print

Render, measure, pack, slice, and prove every printed feature is on the
bed. Refuses before doing any of it when the machine record is missing or
stale, because a plate sliced for a printer that moved is worse than none."
```

---

### Task 5: The report, and end-to-end coverage

A8: what it did, what it changed, what needs a human, in under thirty lines, with the next action inferable without fetching anything.

**Files:**
- Modify: `src/cadloop/pipeline.py`
- Modify: `tests/test_pipeline.py`, `tests/smoke.py`
- Modify: `README.md`

**Interfaces:**
- Produces: the report shape — `{"ok": bool | None, "reason": str, "machine": {"name","bed","filament"}, "parts": [...], "plates": [...], "arranged": [str], "attention": [str], "output": str, "totals": {"plates": int, "minutes": int, "filament_m": float}}`, plus `pipeline.summary(report) -> str` rendering it as the human-readable block.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_pipeline.py`:

```python
def test_summary_fits_one_screen():
    report = {
        "ok": True, "reason": "", "output": "~/cad/plates",
        "machine": {"name": "Ender-3 V3 SE", "bed": {"x_mm": 220.0, "y_mm": 220.0},
                    "filament": "PLA"},
        "parts": [{"name": f"w{i}", "fits": True} for i in range(16)],
        "plates": [{"name": f"plate{i}", "parts": ["a", "b"], "minutes": 90,
                    "filament_m": 7.2, "on_bed": True} for i in range(6)],
        "arranged": ["w80 turned 90 degrees, it does not fit square"],
        "attention": ["plate3 sits 9.7 mm from the bed edge, consider a brim"],
        "totals": {"plates": 6, "minutes": 788, "filament_m": 66.5},
    }
    text = pipeline.summary(report)
    assert len(text.splitlines()) <= 30, text
    assert "Ender-3 V3 SE" in text
    assert "w80 turned" in text
    assert "brim" in text


def test_summary_of_a_refusal_says_what_to_do():
    text = pipeline.summary({"ok": None, "reason": "no printer set up yet, "
                                                   "run setup_printer"})
    assert "setup_printer" in text
    assert len(text.splitlines()) <= 5


def test_a_plate_off_the_bed_is_a_failure_not_a_note():
    report = {"ok": False, "reason": "plate2 prints off the bed",
              "plates": [{"name": "plate2", "on_bed": False}],
              "attention": [], "arranged": []}
    assert "off the bed" in pipeline.summary(report)
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/pytest tests/test_pipeline.py -q -k summary`
Expected: FAIL, `AttributeError: module 'cadloop.pipeline' has no attribute 'summary'`

- [ ] **Step 3: Implement `summary`**

Render the block shown in the spec: a `machine` line, one line each for parts, plates, sliced and proven, an `output` line, then `arranged for you:` and `worth a look:` sections, each omitted when empty. Keep the whole thing at or under thirty lines by truncating the per-part list to a count rather than an enumeration.

- [ ] **Step 4: Add end-to-end smoke coverage**

In `tests/smoke.py`, inside `test_slicer`, after the existing A1/A2 block, add a `make_printable` run against the mock slicer using the mock profiles already set up there, with a two-part `.scad` written into the workspace. Assert: `ok` is True, at least one plate, every plate reports `on_bed`, and the summary is at most thirty lines. This is the only automated coverage of A4/A5/A8 end to end.

- [ ] **Step 5: Run everything**

Run: `.venv/bin/pytest -q && .venv/bin/python tests/smoke.py && .venv/bin/cadloop-verify`
Expected: all green; smoke prints "all checks passed"; verifier 14/14 with no overlaps.

- [ ] **Step 6: Verify against the real machine — this is the acceptance test**

```bash
rm -rf ~/cad/plates && cp models/spirograph.scad ~/cad/
CADLOOP_MACHINE=/tmp/pipe.json SLICER_WORKSPACE="$HOME/cad" .venv/bin/python -c "
import sys; sys.path.insert(0,'src')
from cadloop.slicer_server import setup_printer, make_printable
from cadloop import pipeline
setup_printer()
r = make_printable('spirograph.scad',
                   ['ring','outer_ring',24,30,32,36,40,45,52,56,63,72,80,
                    'ellipse','egg','trefoil'])
print(pipeline.summary(r))"
```

Expected: **two tool calls** from model to print-ready plates (A4), every plate proven on the bed (A5), and a summary of thirty lines or fewer (A8). Record the call count and the line count in the report.

- [ ] **Step 7: Update the README and commit**

Document `make_printable` in the tools list, and replace the worked-example section's `make render PART=ring` walkthrough with the one-call version.

```bash
git add src/cadloop/pipeline.py tests/test_pipeline.py tests/smoke.py README.md
git commit -m "Report what the pipeline did in one screen

What it made, what it arranged for you, and what wants a human, with the
next action inferable without fetching anything else."
```

---

## What lands after this

A4, A5 and A8 are met and the whole of the spec is delivered. The remaining
queue, in order:

1. **Release 0.2.0** to PyPI and re-publish both MCP registry entries, which
   currently pin package version 0.1.0.
2. **Project B, printability checks** — overhangs, first-layer contact area,
   thin walls against `derived.nozzle_mm`. Its own spec; it consumes what this
   landing built.
3. **The mcp 2.0 migration** — `mcp` is pinned `<2` because 2.0.0 removed
   `mcp.server.fastmcp` entirely. Not urgent behind the pin; the 1.x line will
   stop getting fixes eventually.
