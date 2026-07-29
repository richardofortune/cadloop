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
