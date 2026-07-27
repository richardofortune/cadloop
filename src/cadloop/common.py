"""Shared plumbing: workspace confinement, subprocess running, mesh measuring."""

from __future__ import annotations

import os
import re
import shutil
import struct
import subprocess
from pathlib import Path
from typing import Any


def workspace(env_var: str, default: str) -> Path:
    return Path(os.environ.get(env_var, Path.home() / default)).expanduser().resolve()


def safe_path(root: Path, rel: str) -> Path:
    """Resolve rel against root and refuse anything that escapes it."""
    p = Path(rel).expanduser()
    p = p if p.is_absolute() else (root / p)
    p = p.resolve()
    if root != p and root not in p.parents:
        raise ValueError(f"{rel} is outside the workspace ({root})")
    return p


def find_binary(env_var: str, names: list[str], candidates: list[str]) -> str:
    env = os.environ.get(env_var)
    if env:
        # An override that points at nothing is worth saying out loud. Handed
        # back unchecked it surfaces later as a FileNotFoundError from
        # subprocess, naming the path but not the variable that supplied it.
        p = Path(env).expanduser()
        if p.exists():
            return str(p)
        found = shutil.which(env)
        if found:
            return found
        raise RuntimeError(
            f"{env_var} is set to {env!r}, which is not an executable here. "
            f"Unset it to auto-detect.")
    for n in names:
        found = shutil.which(n)
        if found:
            return found
    for c in candidates:
        if Path(c).expanduser().exists():
            return str(Path(c).expanduser())
    raise RuntimeError(f"not found on PATH; set {env_var} to the binary path")


def run(cmd: list[str], timeout_s: int) -> dict[str, Any]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s)
    except subprocess.TimeoutExpired:
        return {"timed_out": True, "returncode": None, "log": "", "command": cmd}
    return {"timed_out": False, "returncode": p.returncode,
            "log": (p.stdout or "") + (p.stderr or ""), "command": cmd}


def tail(text: str, n: int = 3000) -> str:
    return text[-n:] if len(text) > n else text


def scad_literal(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, str):
        return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'
    if isinstance(v, (list, tuple)):
        return "[" + ",".join(scad_literal(x) for x in v) + "]"
    raise ValueError(f"cannot pass {type(v).__name__} to OpenSCAD")


# ---------------------------------------------------------------- meshes

def stl_triangles(path: Path) -> list[list[float]]:
    data = path.read_bytes()
    if data[:5] == b"solid" and b"facet" in data[:512]:
        nums = re.findall(rb"vertex\s+(\S+)\s+(\S+)\s+(\S+)", data)
        flat = [float(x) for t in nums for x in t]
    else:
        n = struct.unpack("<I", data[80:84])[0]
        flat = []
        for i in range(n):
            o = 84 + i * 50
            flat += list(struct.unpack("<9f", data[o + 12:o + 48]))
    return [flat[i:i + 9] for i in range(0, len(flat) - 8, 9)]


def measure_stl(path: Path) -> dict[str, Any]:
    tris = stl_triangles(path)
    if not tris:
        return {"triangles": 0}
    vol = 0.0
    lo = [float("inf")] * 3
    hi = [float("-inf")] * 3
    for t in tris:
        ax, ay, az, bx, by, bz, cx, cy, cz = t
        vol += (ax * (by * cz - bz * cy)
                - ay * (bx * cz - bz * cx)
                + az * (bx * cy - by * cx)) / 6.0
        for k in range(3):
            for j in (0, 3, 6):
                lo[k] = min(lo[k], t[j + k])
                hi[k] = max(hi[k], t[j + k])
    return {"triangles": len(tris),
            "volume_mm3": round(abs(vol), 3),
            "bbox_min": [round(v, 3) for v in lo],
            "bbox_max": [round(v, 3) for v in hi],
            "size_mm": [round(hi[k] - lo[k], 3) for k in range(3)]}
