#!/usr/bin/env python3
"""
Creality Print MCP server.

Creality Print is an OrcaSlicer fork, so it carries the same headless CLI:
--slice, --load-settings, --load-filaments, --export-3mf and friends. This
server puts that CLI behind MCP tools so a model can find profiles, slice a
model, and read back filament usage and print time without a GUI.

It also drives OrcaSlicer, Bambu Studio and ElegooSlicer, which share the
CLI. Point SLICER_BIN wherever you like.

Environment:
  SLICER_BIN           path to the slicer binary (auto-detected if unset)
  SLICER_WORKSPACE     the only directory this server reads and writes
                       (default ~/slicing)
  SLICER_PROFILE_DIRS  extra profile roots, os.pathsep separated
  SLICER_TIMEOUT       seconds before a slice is killed (default 600)

Run:  python creality_mcp.py
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from contextlib import ExitStack
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from mcp.server.fastmcp import FastMCP

from .common import (find_binary, measure_stl, run as _sh,
                     safe_path, tail as _tail_, workspace)

WORKSPACE = workspace("SLICER_WORKSPACE", "cad")
DEFAULT_TIMEOUT = int(os.environ.get("SLICER_TIMEOUT", "600"))

mcp = FastMCP("creality-slicer")

# Orca and its forks come first. Creality Print is last on purpose: its CLI
# does not work headless on macOS at all, so picking it by default there
# hands back a binary that segfaults on every call. Set SLICER_BIN to
# override any of this.
_BIN_CANDIDATES = [
    "/Applications/OrcaSlicer.app/Contents/MacOS/OrcaSlicer",
    "/Applications/BambuStudio.app/Contents/MacOS/BambuStudio",
    "/Applications/ElegooSlicer.app/Contents/MacOS/ElegooSlicer",
    "/usr/bin/orca-slicer",
    "/usr/bin/OrcaSlicer",
    r"C:\Program Files\OrcaSlicer\orca-slicer.exe",
    r"C:\Program Files\Bambu Studio\bambu-studio.exe",
    "/Applications/Creality Print.app/Contents/MacOS/CrealityPrint",
    "/Applications/CrealityPrint.app/Contents/MacOS/CrealityPrint",
    r"C:\Program Files\Creality\Creality Print 7.0\CrealityPrint.exe",
    r"C:\Program Files\Creality\Creality Print 6.0\CrealityPrint.exe",
    "/usr/bin/CrealityPrint",
]

# Each of these slicers ships its vendor profiles inside the install as well
# as writing user ones to a config directory, and the bundled set is the one
# holding the stock machine definitions.
_PROFILE_CANDIDATES = [
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


# --------------------------------------------------------------------
# plumbing
# --------------------------------------------------------------------

def _binary() -> str:
    return find_binary(
        "SLICER_BIN",
        ["CrealityPrint", "creality-print", "orca-slicer", "OrcaSlicer"],
        _BIN_CANDIDATES)


def _run(args: list[str], timeout_s: int | None = None) -> dict[str, Any]:
    return _sh([_binary()] + args, timeout_s or DEFAULT_TIMEOUT)


def _safe(rel: str) -> Path:
    return safe_path(WORKSPACE, rel)


def _tail(log: str, n: int = 3000) -> str:
    return _tail_(log, n)


def _errors(log: str) -> list[str]:
    out = []
    for line in log.splitlines():
        s = line.strip()
        if re.search(r"\b(error|fatal|failed|exception|crash)\b", s, re.I):
            out.append(s)
    return out[-25:]


# --------------------------------------------------------------------
# profiles
# --------------------------------------------------------------------

def _profile_roots() -> list[Path]:
    roots: list[Path] = []
    for raw in os.environ.get("SLICER_PROFILE_DIRS", "").split(os.pathsep):
        if raw.strip():
            roots.append(Path(raw.strip()).expanduser())
    for c in _PROFILE_CANDIDATES:
        p = Path(c).expanduser()
        if p.exists():
            roots.append(p)
    seen, out = set(), []
    for r in roots:
        if r.exists() and str(r) not in seen:
            seen.add(str(r))
            out.append(r)
    return out


_PROFILE_INDEX: dict[str, Path] | None = None


def _profile_index() -> dict[str, Path]:
    """Map profile name to path across every root, so `inherits` can be
    followed. Built once per process; the profile tree does not move."""
    global _PROFILE_INDEX
    if _PROFILE_INDEX is None:
        idx: dict[str, Path] = {}
        for root in _profile_roots():
            for p in root.rglob("*.json"):
                try:
                    if p.stat().st_size > 2_000_000:
                        continue
                    data = json.loads(p.read_text(errors="replace"))
                except Exception:
                    continue
                if isinstance(data, dict) and data.get("name"):
                    idx.setdefault(str(data["name"]), p)
        _PROFILE_INDEX = idx
    return _PROFILE_INDEX


def _profile_chain(path: Path, limit: int = 12) -> list[dict[str, Any]]:
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
        cur = _profile_index().get(str(parent))
    return out


def _inherited(chain: list[dict[str, Any]], key: str) -> Any:
    """First non-empty value for key walking up the inheritance chain."""
    for data in chain:
        v = data.get(key)
        if v not in (None, "", [], {}):
            return v
    return None


def _area_points(raw: Any) -> list[tuple[float, float]]:
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


def _classify(path: Path) -> dict[str, Any] | None:
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


# --------------------------------------------------------------------
# output archive
# --------------------------------------------------------------------

def _parse_config(text: str) -> Any:
    text = text.strip()
    if text.startswith("{") or text.startswith("["):
        return json.loads(text)
    root = ElementTree.fromstring(text)
    out: dict[str, Any] = {"plates": []}
    for plate in root.iter("plate"):
        rec: dict[str, Any] = {"filaments": []}
        for md in plate.findall("metadata"):
            rec[md.get("key", "")] = md.get("value")
        for fil in plate.findall("filament"):
            rec["filaments"].append(dict(fil.attrib))
        out["plates"].append(rec)
    for hdr in root.iter("header_item"):
        out.setdefault("header", {})[hdr.get("key", "")] = hdr.get("value")
    return out


def _archive_summary(path: Path) -> dict[str, Any]:
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        out: dict[str, Any] = {
            "archive": str(path),
            "bytes": path.stat().st_size,
            "gcode_entries": sorted(n for n in names
                                    if n.lower().endswith(".gcode")),
            "entries": len(names),
        }
        for entry in ("Metadata/slice_info.config", "Metadata/slice_info.json"):
            if entry in names:
                try:
                    out["slice_info"] = _parse_config(
                        z.read(entry).decode("utf-8", "replace"))
                except Exception as exc:
                    out["slice_info_error"] = str(exc)
                break
        if "Metadata/project_settings.config" in names:
            try:
                cfg = json.loads(z.read("Metadata/project_settings.config")
                                 .decode("utf-8", "replace"))
                out["settings_used"] = {
                    k: cfg.get(k) for k in
                    ("printer_model", "printer_settings_id",
                     "print_settings_id", "filament_settings_id",
                     "layer_height", "sparse_infill_density",
                     "nozzle_diameter", "filament_type")
                    if k in cfg
                }
            except Exception:
                pass
    return out


# --------------------------------------------------------------------
# tools
# --------------------------------------------------------------------

@mcp.tool()
def slicer_info() -> dict[str, Any]:
    """Report the slicer binary, its version banner, and the full --help flag
    surface. Call this first. The Orca-family CLI changes between releases
    and Creality's fork diverges, so trust this over any assumed flag list."""
    r = _run(["--help"], timeout_s=60)
    flags = sorted(set(re.findall(r"--[a-z][a-z0-9-]+", r["log"] or "")))
    return {
        "binary": _binary(),
        "workspace": str(WORKSPACE),
        "profile_roots": [str(p) for p in _profile_roots()],
        "flags": flags,
        "help_text": _tail(r["log"] or "", 6000),
        "default_timeout_s": DEFAULT_TIMEOUT,
    }


@mcp.tool()
def list_profiles(kind: str = "all", query: str | None = None,
                  limit: int = 60) -> dict[str, Any]:
    """Find machine, process and filament profiles. kind is machine, process,
    filament or all. query filters on the profile name, case-insensitive."""
    found: list[dict[str, Any]] = []
    for root in _profile_roots():
        for p in root.rglob("*.json"):
            rec = _classify(p)
            if not rec:
                continue
            if kind != "all" and rec["kind"] != kind:
                continue
            if query and query.lower() not in (rec["name"] or "").lower():
                continue
            found.append(rec)
    found.sort(key=lambda r: (r["kind"], r["name"] or ""))
    return {"count": len(found), "profiles": found[:limit],
            "truncated": len(found) > limit,
            "roots_searched": [str(p) for p in _profile_roots()]}


@mcp.tool()
def list_files(pattern: str = "**/*") -> list[str]:
    """List files in the slicing workspace."""
    if not WORKSPACE.exists():
        return []
    return sorted(str(p.relative_to(WORKSPACE))
                  for p in WORKSPACE.glob(pattern) if p.is_file())


@mcp.tool()
def model_info(model: str, timeout_s: int | None = None) -> dict[str, Any]:
    """Report what the slicer sees in a model or project file without
    slicing it. Uses --info."""
    src = _safe(model)
    r = _run(["--info", str(src)], timeout_s=timeout_s or 120)
    return {"ok": r["returncode"] == 0, "timed_out": r["timed_out"],
            "output": _tail(r["log"])}


def _bed(machine_profile: str) -> dict[str, Any]:
    chain = _profile_chain(Path(machine_profile).expanduser())
    if not chain:
        return {}
    pts = _area_points(_inherited(chain, "printable_area"))
    if len(pts) < 2:
        return {}
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    h = _inherited(chain, "printable_height")
    return {"x_mm": round(max(xs) - min(xs), 2),
            "y_mm": round(max(ys) - min(ys), 2),
            "z_mm": float(h) if h else None,
            "origin": [min(xs), min(ys)]}


@mcp.tool()
def check_bed_fit(model: str, machine_profile: str,
                  margin_mm: float = 3.0) -> dict[str, Any]:
    """Compare an STL's footprint against the machine profile's printable
    area before slicing. The slicer will happily emit out-of-bounds G-code
    without complaining, so this is worth doing first.

    Rotating 45 degrees in Z is checked too, since a part that misses the
    bed square-on often fits on the diagonal."""
    stl = _safe(model)
    mesh = measure_stl(stl)
    if not mesh.get("triangles"):
        return {"ok": None, "reason": "no geometry in that mesh", "mesh": mesh}
    bed = _bed(machine_profile)
    if not bed:
        return {"ok": None, "reason": "no printable_area in machine profile",
                "mesh": mesh}
    sx, sy, sz = mesh["size_mm"]
    diag = (sx + sy) * 0.70711        # footprint of a 45 degree rotation
    fits = (sx + margin_mm <= bed["x_mm"] and sy + margin_mm <= bed["y_mm"])
    fits_rot = (diag + margin_mm <= bed["x_mm"]
                and diag + margin_mm <= bed["y_mm"])
    tall = bed["z_mm"] is not None and sz > bed["z_mm"]
    return {
        "ok": bool((fits or fits_rot) and not tall),
        "fits_square": fits,
        "fits_rotated_45": fits_rot,
        "too_tall": bool(tall),
        "part_size_mm": mesh["size_mm"],
        "bed": bed,
        "margin_mm": margin_mm,
        "volume_mm3": mesh["volume_mm3"],
    }


@mcp.tool()
def slice_model(
    models: list[str],
    machine_profile: str,
    process_profile: str,
    filament_profiles: list[str],
    output: str,
    plate: int = 1,
    arrange: int = 1,
    orient: int = 0,
    ensure_on_bed: bool = True,
    allow_newer_file: bool = True,
    min_save: bool = True,
    overrides: dict[str, Any] | None = None,
    extra_args: list[str] | None = None,
    timeout_s: int | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Slice one or more models to a .gcode.3mf and report the result.

    machine_profile, process_profile and filament_profiles are paths to
    profile JSONs, as returned by list_profiles. Load order matters: the
    machine profile is passed before the process profile.

    overrides is a map of any slicer setting key to a value, passed as
    command-line flags. These beat both the profiles and anything embedded
    in a project file. Values go through as strings, which is what the
    Orca-family CLI expects.

    plate 0 slices every plate. arrange 1 packs multiple models onto the
    bed; without it they stack at the origin. Nothing here checks that the
    parts fit the bed, so measure them first.

    Set dry_run to see the exact command without running it."""
    srcs = [_safe(m) for m in models]
    for s in srcs:
        if not s.exists():
            raise FileNotFoundError(f"{s} not found")
    dst = _safe(output)
    dst.parent.mkdir(parents=True, exist_ok=True)

    settings = f"{Path(machine_profile).expanduser()};{Path(process_profile).expanduser()}"
    filaments = ";".join(str(Path(f).expanduser()) for f in filament_profiles)

    args = ["--slice", str(plate),
            "--load-settings", settings,
            "--load-filaments", filaments,
            "--arrange", str(arrange),
            "--orient", str(orient),
            "--export-3mf", str(dst)]
    if ensure_on_bed:
        args.append("--ensure-on-bed")
    if allow_newer_file:
        args.append("--allow-newer-file")
    if min_save:
        args.append("--min-save")
    for k, v in (overrides or {}).items():
        args += [f"--{k.replace('_', '-') if k.startswith('-') else k}", str(v)]
    args += list(extra_args or [])
    args += [str(s) for s in srcs]

    if dry_run:
        return {"dry_run": True, "command": [_binary()] + args}

    r = _run(args, timeout_s=timeout_s)
    res: dict[str, Any] = {
        "ok": r["returncode"] == 0 and dst.exists(),
        "returncode": r["returncode"],
        "timed_out": r["timed_out"],
        "command": r["command"],
        "output": str(dst) if dst.exists() else None,
        "errors": _errors(r["log"]),
        "log_tail": _tail(r["log"], 2000),
    }
    if dst.exists() and zipfile.is_zipfile(dst):
        try:
            res["summary"] = _archive_summary(dst)
        except Exception as exc:
            res["summary_error"] = str(exc)
    return res


@mcp.tool()
def slice_summary(archive: str) -> dict[str, Any]:
    """Read filament usage, print time estimate and the settings actually
    used out of a sliced .gcode.3mf, without unpacking it."""
    p = _safe(archive)
    if not zipfile.is_zipfile(p):
        raise ValueError(f"{archive} is not a 3MF archive")
    return _archive_summary(p)


@mcp.tool()
def extract_gcode(archive: str, output: str, plate: int = 1) -> dict[str, Any]:
    """Pull the raw G-code for a plate out of a .gcode.3mf. Creality
    printers generally want a plain .gcode file, unlike Bambu machines
    which take the archive directly."""
    src = _safe(archive)
    dst = _safe(output)
    dst.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(src) as z:
        names = [n for n in z.namelist() if n.lower().endswith(".gcode")]
        want = [n for n in names if f"plate_{plate}" in n] or names
        if not want:
            raise FileNotFoundError(f"no gcode inside {archive}")
        dst.write_bytes(z.read(want[0]))
    head = dst.read_text(errors="replace")[:4000]
    meta = dict(re.findall(r"^;\s*([a-z_ ]+?)\s*[:=]\s*(.+)$",
                           head, re.M | re.I))
    return {"entry": want[0], "output": str(dst),
            "bytes": dst.stat().st_size,
            "header": {k.strip(): v.strip() for k, v in list(meta.items())[:20]}}


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
