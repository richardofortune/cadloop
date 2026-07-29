"""Read a finished G-code file and say where it actually prints.

The naive reading — every G0/G1 in the file — is wrong twice over, and
wrong in a way that looks right. A Creality start macro draws its prime
line at X-2.4, deliberately off the side of the bed, and the end macro
runs the bed out to Y220 to hand you the part. Neither is the object.
Only extruding moves after the first object marker count, comments are
stripped before anything is parsed, and extrusion is tracked numerically
so it works under both relative (M83) and absolute (M82) extruder modes.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

# A bare leading dot is a legal G-code number and slicers do emit one. Both
# of these read it, and they have to agree: when _XYZ refused "X.5" the
# token did not fail, it was simply skipped, and the *previous* X carried
# forward as though the move had not happened — a wrong coordinate in the
# extent A5 is proven from, rather than a refusal anyone could see.
_NUMBER = r"-?(?:\d+\.?\d*|\.\d+)"
_XYZ = re.compile(rf"([XYZ])({_NUMBER})")
_E_VALUE = re.compile(rf"E({_NUMBER})")
_HEADER_EQ = re.compile(r"^;\s*([a-z_][a-z0-9_ ]*?)\s*=\s*(.+?)\s*$", re.I)
_HEADER_COLON = re.compile(r"^;\s*([a-z_][a-z0-9_ ]*?)\s*:\s*(.+?)\s*$", re.I)
# Every Orca-family slicer writes this before the first object it prints.
_OBJECT = "; printing object"
_HEADER_BLOCK_START = "; HEADER_BLOCK_START"
_HEADER_BLOCK_END = "; HEADER_BLOCK_END"


def header(path: str | Path) -> dict[str, str]:
    """The `; key = value` / `; key: value` metadata slicers write.

    OrcaSlicer writes two things that both look like "the header": a
    `HEADER_BLOCK_START`/`END` pair at the very top using colon syntax
    (that is where `max_z_height` and the generator banner live), and the
    authoritative full `key = value` config block at the *end* of the
    file. A decoy `; ... extrusion width = ...` block sits between them
    at the top and must not be mistaken for either. The whole file is
    scanned — never just the top — and later occurrences of a key win,
    since the trailing block is the definitive one.
    """
    out: dict[str, str] = {}
    in_block = False
    with open(path, errors="replace") as fh:
        for line in fh:
            if not line.startswith(";"):
                continue
            if line.startswith(_HEADER_BLOCK_START):
                in_block = True
                continue
            if line.startswith(_HEADER_BLOCK_END):
                in_block = False
                continue
            if in_block:
                m = _HEADER_COLON.match(line)
                if m:
                    out[m.group(1)] = m.group(2)
            m = _HEADER_EQ.match(line)
            if m:
                out[m.group(1)] = m.group(2)
    return out


def extent(path: str | Path) -> dict[str, Any]:
    """Where the object prints: the box its extruding moves occupy.

    ok is None (not False) when the file has no object marker, or when
    one is present but nothing after it ever extrudes — either means the
    file is not slicer output we recognise, and an extent nobody can
    vouch for is worse than none. Comments are stripped before any move
    is parsed, so a travel line whose comment happens to mention "E" is
    never mistaken for an extruding one. Extrusion is tracked numerically:
    in relative mode (M83) a move extrudes when its E is positive; in
    absolute mode (M82) when its E exceeds the last E seen. G92 E.. resets
    the running total.
    """
    xs: list[float] = []
    ys: list[float] = []
    zs: list[float] = []
    x = y = z = None
    started = False
    relative_e = False
    last_e = 0.0
    with open(path, errors="replace") as fh:
        for raw in fh:
            if raw.startswith(_OBJECT):
                started = True
                continue
            code = raw.split(";", 1)[0].strip()
            if not code:
                continue
            if code.startswith("M83"):
                relative_e = True
                continue
            if code.startswith("M82"):
                relative_e = False
                continue
            if code.startswith("G92"):
                m = _E_VALUE.search(code)
                if m:
                    last_e = float(m.group(1))
                continue
            if not started or not code.startswith(("G0", "G1")):
                continue
            for axis, value in _XYZ.findall(code):
                v = float(value)
                if axis == "X":
                    x = v
                elif axis == "Y":
                    y = v
                else:
                    z = v
            extruding = False
            m = _E_VALUE.search(code)
            if m:
                e_val = float(m.group(1))
                if relative_e:
                    extruding = e_val > 0
                else:
                    extruding = e_val > last_e
                    last_e = e_val
            if extruding and x is not None and y is not None:
                xs.append(x)
                ys.append(y)
                if z is not None:
                    zs.append(z)
    if not xs:
        reason = ("no object marker in this G-code, so there is nothing to "
                   "measure" if not started else
                   "an object marker was found but nothing after it extrudes")
        return {"ok": None, "x": [], "y": [], "z_max": 0.0, "moves": 0,
                "reason": reason}
    return {"ok": True, "x": [min(xs), max(xs)], "y": [min(ys), max(ys)],
            "z_max": max(zs) if zs else 0.0, "moves": len(xs)}


def fits(path: str | Path, bed: dict[str, Any],
         margin_mm: float = 0.0) -> dict[str, Any]:
    """Whether every printed feature lands inside the machine's bed.

    ok is None when the question cannot be settled: no extent to measure,
    no bed to measure against, or a bed with no printable height under a
    file that climbs. That last one used to answer True, because a height
    of None simply skipped the z comparison — an absent number turning a
    check off rather than failing it, and the caller told every feature was
    inside a bed whose height nobody knew. A file that never leaves z0 is
    still provable, since there is no height to check.

    A definite failure outranks an unknown: a plate that overruns x is off
    the bed whatever its height, so that answers False rather than None.
    """
    e = extent(path)
    if not e["ok"]:
        return {"ok": None, "reason": e.get("reason", "nothing to measure"),
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
    if bad:
        return {"ok": False, "reason": "; ".join(bad), "extent": e, "bed": bed}
    if e["z_max"] and not bed.get("z_mm"):
        return {"ok": None,
                "reason": f"x and y are inside the bed, but this bed declares "
                          f"no printable height, so the {e['z_max']} mm this "
                          f"reaches in z is unchecked",
                "extent": e, "bed": bed}
    return {"ok": True, "reason": "", "extent": e, "bed": bed}
