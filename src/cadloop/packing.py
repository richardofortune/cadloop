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
