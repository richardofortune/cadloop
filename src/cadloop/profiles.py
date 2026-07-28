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


def _resolved(p: Path) -> Path:
    """expanduser plus resolve, falling back to the literal path when the
    filesystem will not answer. Roots have to be comparable as strings for
    containment to mean anything, and a symlinked spelling and a real one are
    the same install."""
    try:
        return Path(p).expanduser().resolve()
    except OSError:
        return Path(p).expanduser()


def profile_roots() -> list[Path]:
    """Every directory searched for vendor profiles, resolved and deduped.

    Roots nest: SLICER_PROFILE_DIRS may reasonably name /Applications or
    ~/Library/Application Support while the auto-detected candidates name
    individual installs inside them. Both are kept here, because both may
    hold profiles nothing else would find; telling the installs apart is
    root_of()'s job, not this one's."""
    roots: list[Path] = []
    for raw in os.environ.get("SLICER_PROFILE_DIRS", "").split(os.pathsep):
        if raw.strip():
            roots.append(_resolved(Path(raw.strip())))
    for c in PROFILE_CANDIDATES:
        roots.append(_resolved(Path(c)))
    seen, out = set(), []
    for r in roots:
        if str(r) not in seen and r.is_dir():
            seen.add(str(r))
            out.append(r)
    return out


def root_of(path: str | Path) -> Path | None:
    """The install a profile belongs to: the most specific root holding it.

    Returning the first root that happens to contain the path answers
    "/Applications" whenever that is configured, which makes "are these two
    profiles from the same install" unanswerable and quietly disables every
    check built on it. The longest match is the only one that carries
    information, so that is the answer. None when no root holds the path,
    which is a "cannot tell", not a "no"."""
    p = _resolved(Path(path))
    best: Path | None = None
    for root in profile_roots():
        try:
            p.relative_to(root)
        except ValueError:
            continue
        if best is None or len(str(root)) > len(str(best)):
            best = root
    return best


def same_install(a: str | Path, b: str | Path) -> bool | None:
    """Whether two profiles come from one install. None when it cannot be
    told, because neither path sits under a configured root."""
    ra, rb = root_of(a), root_of(b)
    if ra is None or rb is None:
        return None
    return str(ra) == str(rb)


_INDEX: dict[str, Path] | None = None
_ROOT_INDEX: dict[str, list[dict[str, Any]]] = {}


def reset_cache() -> None:
    """Drop the memoised name index and the per-root ones. Tests and
    re-setup need this: profiles installed after a failed setup are
    invisible until the memo is dropped."""
    global _INDEX
    _INDEX = None
    _ROOT_INDEX.clear()


def root_profiles(root: Path) -> list[dict[str, Any]]:
    """Every profile under one root, classified, sorted by path, memoised.

    profile_index() keeps one path per name across every root, so it can only
    ever show one vendor's copy of a name two vendors ship. Any question about
    a single install has to be answered from that install's own files, and
    answering it by walking the tree afresh each time reads every profile on
    disk several times over. Sorted so the answer does not depend on the
    order the filesystem hands directory entries back."""
    key = str(root)
    if key not in _ROOT_INDEX:
        out: list[dict[str, Any]] = []
        for p in sorted(Path(root).rglob("*.json")):
            rec = classify(p)
            if rec:
                out.append(rec)
        _ROOT_INDEX[key] = out
    return _ROOT_INDEX[key]


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


def _first_scalar(value: Any) -> Any:
    """Vendor profiles write single values as one-element lists as often as
    not: nozzle_diameter is ["0.4"], filament_type is ["PLA"]."""
    if isinstance(value, (list, tuple)):
        return value[0] if value else None
    return value


def _as_float(value: Any) -> float | None:
    try:
        return float(_first_scalar(value))
    except (TypeError, ValueError):
        return None


def machine_facts(machine: str | Path, process: str | Path,
                  filament: str | Path) -> dict[str, Any]:
    """The handful of facts the rest of the system needs, read once.

    Everything downstream asks for these rather than parsing vendor JSON,
    so the two spellings of printable_area and the inheritance walk are
    handled in one place instead of being rediscovered per caller."""
    m = profile_chain(Path(machine).expanduser())
    p = profile_chain(Path(process).expanduser())
    f = profile_chain(Path(filament).expanduser())
    return {
        "printer_model": _first_scalar(inherited(m, "printer_model")),
        "nozzle_mm": _as_float(inherited(m, "nozzle_diameter")),
        "gcode_flavor": _first_scalar(inherited(m, "gcode_flavor")),
        "layer_height_mm": _as_float(inherited(p, "layer_height")),
        "filament_type": _first_scalar(inherited(f, "filament_type")),
        "bed": bed_of(machine),
    }
