"""Vendor profile resolution: JSON on disk in, machine facts out.

Kept apart from the MCP server because the packer, the verifier and the
machine record all need it, and because it is the part most exposed to
vendor idiosyncrasy: printable_area is written two different ways, and most
machines inherit their bed rather than declaring it.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

# Each of these slicers ships its vendor profiles inside the install as well
# as writing user ones to a config directory, and the bundled set is the one
# holding the stock machine definitions.
PROFILE_CANDIDATES = [
    "/Applications/OrcaSlicer.app/Contents/Resources/profiles",
    "/Applications/BambuStudio.app/Contents/Resources/profiles",
    "/Applications/ElegooSlicer.app/Contents/Resources/profiles",
    "/Applications/Creality Print.app/Contents/Resources/profiles",
    "~/Library/Application Support/OrcaSlicer",
    "~/Library/Application Support/BambuStudio",
    "~/Library/Application Support/ElegooSlicer",
    "~/Library/Application Support/Creality/Creality Print",
    r"C:\Program Files\OrcaSlicer\resources\profiles",
    r"C:\Program Files\Bambu Studio\resources\profiles",
    r"C:\Program Files\Creality\Creality Print 7.0\resources\profiles",
    "~/AppData/Roaming/OrcaSlicer",
    "~/AppData/Roaming/BambuStudio",
    "~/AppData/Roaming/Creality/Creality Print",
    "/usr/share/OrcaSlicer/resources/profiles",
    "/usr/share/CrealityPrint/resources/profiles",
    "~/.config/OrcaSlicer",
    "~/.config/CrealityPrint",
]


def profile_roots() -> list[Path]:
    roots: list[Path] = []
    for raw in os.environ.get("SLICER_PROFILE_DIRS", "").split(os.pathsep):
        if raw.strip():
            roots.append(Path(raw.strip()).expanduser())
    for c in PROFILE_CANDIDATES:
        p = Path(c).expanduser()
        if p.exists():
            roots.append(p)
    seen, out = set(), []
    for r in roots:
        if r.exists() and str(r) not in seen:
            seen.add(str(r))
            out.append(r)
    return out


_INDEX: dict[str, Path] | None = None


def reset_cache() -> None:
    """Drop the memoised name index. Tests and re-setup need this."""
    global _INDEX
    _INDEX = None


def profile_index() -> dict[str, Path]:
    """Map profile name to path across every root, so `inherits` can be
    followed. Built once per process; the profile tree does not move."""
    global _INDEX
    if _INDEX is None:
        idx: dict[str, Path] = {}
        for root in profile_roots():
            for p in root.rglob("*.json"):
                try:
                    if p.stat().st_size > 2_000_000:
                        continue
                    data = json.loads(p.read_text(errors="replace"))
                except Exception:
                    continue
                if isinstance(data, dict) and data.get("name"):
                    idx.setdefault(str(data["name"]), p)
        _INDEX = idx
    return _INDEX


def profile_chain(path: Path, limit: int = 12) -> list[dict[str, Any]]:
    """A profile followed by its `inherits` ancestors, nearest first.

    Stock profiles are layered: most Bambu machines carry no printable_area
    of their own and pick it up from a common base."""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    cur: Path | None = Path(path).expanduser()
    while cur is not None and len(out) < limit:
        try:
            data = json.loads(Path(cur).read_text(errors="replace"))
        except Exception:
            break
        if not isinstance(data, dict):
            break
        out.append(data)
        parent = data.get("inherits")
        if not parent or str(parent) in seen:
            break
        seen.add(str(parent))
        cur = profile_index().get(str(parent))
    return out


def inherited(chain: list[dict[str, Any]], key: str) -> Any:
    """First non-empty value for key walking up the inheritance chain."""
    for data in chain:
        v = data.get(key)
        if v not in (None, "", [], {}):
            return v
    return None


def area_points(raw: Any) -> list[tuple[float, float]]:
    """printable_area is a list of "XxY" strings in some stock profiles and a
    single comma-separated string in others. Both ship with Creality Print."""
    if isinstance(raw, str):
        items: list[Any] = raw.split(",")
    elif isinstance(raw, (list, tuple)):
        items = list(raw)
    else:
        return []
    pts: list[tuple[float, float]] = []
    for item in items:
        m = re.findall(r"-?\d+(?:\.\d+)?", str(item))
        if len(m) >= 2:
            pts.append((float(m[0]), float(m[1])))
    return pts


def classify(path: Path) -> dict[str, Any] | None:
    """Orca-family profile JSONs carry a "type" key: machine, process or
    filament. Anything else is not a profile we care about."""
    try:
        if path.stat().st_size > 2_000_000:
            return None
        data = json.loads(path.read_text(errors="replace"))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    kind = data.get("type")
    if kind not in ("machine", "process", "filament"):
        return None
    return {
        "kind": kind,
        "name": data.get("name") or path.stem,
        "path": str(path),
        "inherits": data.get("inherits"),
        "printer_model": data.get("printer_model"),
        "nozzle": data.get("nozzle_diameter"),
        "from": data.get("from"),
    }


def bed_of(machine_profile: str | Path) -> dict[str, Any]:
    chain = profile_chain(Path(machine_profile).expanduser())
    if not chain:
        return {}
    pts = area_points(inherited(chain, "printable_area"))
    if len(pts) < 2:
        return {}
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    h = inherited(chain, "printable_height")
    return {"x_mm": round(max(xs) - min(xs), 2),
            "y_mm": round(max(ys) - min(ys), 2),
            "z_mm": float(h) if h else None,
            "origin": [min(xs), min(ys)]}
