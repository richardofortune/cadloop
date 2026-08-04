#!/usr/bin/env python3
"""
OpenSCAD MCP server.

Exposes an OpenSCAD install over MCP so a model can write a .scad file,
compile it, read its ECHO output, render it to a mesh, measure it, and
look at a PNG of it, without a human in the loop for each step.

Environment:
  OPENSCAD_BIN        path to the openscad binary (auto-detected if unset)
  OPENSCAD_WORKSPACE  directory the server may read and write (default ~/openscad)
  OPENSCAD_TIMEOUT    default seconds before a run is killed (default 300)

Run:  python openscad_mcp.py
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from contextlib import ExitStack
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP, Image

from .common import (find_binary, measure_stl, run as _sh, safe_path,
                     scad_literal, tail, workspace)

WORKSPACE = workspace("OPENSCAD_WORKSPACE", "cad")
DEFAULT_TIMEOUT = int(os.environ.get("OPENSCAD_TIMEOUT", "300"))
MESH_FORMATS = {"stl", "off", "amf", "3mf", "nef3"}
FLAT_FORMATS = {"dxf", "svg"}
TEXT_FORMATS = {"csg", "echo", "ast", "term"}
IMAGE_FORMATS = {"png"}

mcp = FastMCP("openscad")


# --------------------------------------------------------------------
# process plumbing
# --------------------------------------------------------------------

def _binary() -> str:
    return find_binary(
        "OPENSCAD_BIN",
        ["openscad", "openscad-nightly", "OpenSCAD"],
        ["/Applications/OpenSCAD.app/Contents/MacOS/OpenSCAD"])


def _needs_xvfb() -> bool:
    """PNG export wants a GL context. Linux without a display needs xvfb."""
    return (os.name == "posix"
            and not os.environ.get("DISPLAY")
            and not os.environ.get("WAYLAND_DISPLAY")
            and shutil.which("xvfb-run") is not None
            and not Path("/Applications").exists())


def _run(args: list[str], display: bool = False,
         timeout_s: int | None = None) -> dict[str, Any]:
    cmd = [_binary()] + args
    if display and _needs_xvfb():
        cmd = ["xvfb-run", "-a"] + cmd
    return _sh(cmd, timeout_s or DEFAULT_TIMEOUT)


_STAT = re.compile(r"^\s*(Vertices|Halfedges|Edges|Halffacets|Facets|Volumes|"
                   r"Simple|Top level object is a \dD object):\s*(.+?)\s*$")


def _parse(log: str) -> dict[str, Any]:
    stats: dict[str, str] = {}
    echoes: list[str] = []
    warnings: list[str] = []
    errors: list[str] = []
    for raw in log.splitlines():
        line = raw.rstrip()
        m = _STAT.match(line)
        if m:
            stats[m.group(1).lower()] = m.group(2)
        s = line.strip()
        if s.startswith("ECHO:"):
            echoes.append(s[5:].strip())
        elif "ERROR:" in s or "TRACE:" in s:
            errors.append(s)
        elif "WARNING:" in s:
            warnings.append(s)
    return {"stats": stats, "echo": echoes,
            "warnings": warnings, "errors": errors}


# --------------------------------------------------------------------
# workspace and parameter handling
# --------------------------------------------------------------------

def _safe(rel: str) -> Path:
    return safe_path(WORKSPACE, rel)


def _literal(v: Any) -> str:
    return scad_literal(v)


def _defines(d: dict[str, Any] | None) -> list[str]:
    out: list[str] = []
    for k, v in (d or {}).items():
        out += ["-D", f"{k}={_literal(v)}"]
    return out


def _view_args(camera: str | None, imgsize: str, projection: str,
               colorscheme: str, full_render: bool, viewall: bool) -> list[str]:
    """The camera half of an image run, shared by preview and render.

    Written once because the two differ only in where the PNG ends up: one
    hands it back to the caller, the other leaves it in the workspace."""
    args = ["--imgsize", imgsize, "--projection", projection,
            "--colorscheme", colorscheme]
    if camera:
        args += ["--camera", camera]
    elif viewall:
        args += ["--viewall", "--autocenter"]
    if full_render:
        args += ["--render"]
    return args


def _input(stack: ExitStack, file: str | None, source: str | None) -> Path:
    if (file is None) == (source is None):
        raise ValueError("pass exactly one of file or source")
    if file is not None:
        p = _safe(file)
        if not p.exists():
            raise FileNotFoundError(f"{file} not found in {WORKSPACE}")
        return p
    WORKSPACE.mkdir(parents=True, exist_ok=True)
    tmp = stack.enter_context(
        tempfile.NamedTemporaryFile("w", suffix=".scad", dir=WORKSPACE,
                                    delete=True))
    tmp.write(source or "")
    tmp.flush()
    return Path(tmp.name)


# --------------------------------------------------------------------
# mesh measurement
# --------------------------------------------------------------------

def _measure(path: Path) -> dict[str, Any]:
    return measure_stl(path)


# --------------------------------------------------------------------
# tools
# --------------------------------------------------------------------

@mcp.tool()
def openscad_info() -> dict[str, Any]:
    """Report the OpenSCAD binary, its version, and the workspace directory."""
    r = _run(["--version"], timeout_s=30)
    return {
        "binary": _binary(),
        "version": (r["log"] or "").strip(),
        "workspace": str(WORKSPACE),
        "workspace_exists": WORKSPACE.exists(),
        "png_via_xvfb": _needs_xvfb(),
        "default_timeout_s": DEFAULT_TIMEOUT,
    }


@mcp.tool()
def list_files(pattern: str = "**/*.scad") -> list[str]:
    """List files in the workspace matching a glob pattern."""
    if not WORKSPACE.exists():
        return []
    return sorted(str(p.relative_to(WORKSPACE))
                  for p in WORKSPACE.glob(pattern) if p.is_file())


@mcp.tool()
def read_file(path: str) -> str:
    """Read a text file from the workspace."""
    return _safe(path).read_text()


@mcp.tool()
def write_file(path: str, content: str) -> dict[str, Any]:
    """Write a text file into the workspace, creating parent directories."""
    p = _safe(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    return {"path": str(p.relative_to(WORKSPACE)), "bytes": len(content)}


@mcp.tool()
def check(file: str | None = None, source: str | None = None,
          defines: dict[str, Any] | None = None,
          hard_warnings: bool = False) -> dict[str, Any]:
    """Compile a script without evaluating geometry. Fast syntax and
    reference check. Returns errors, warnings and any ECHO output."""
    with ExitStack() as stack:
        src = _input(stack, file, source)
        out = stack.enter_context(
            tempfile.NamedTemporaryFile(suffix=".csg", delete=True))
        args = ["-o", out.name, str(src)] + _defines(defines)
        if hard_warnings:
            args.append("--hardwarnings")
        r = _run(args, timeout_s=120)
        parsed = _parse(r["log"])
        return {"ok": r["returncode"] == 0 and not parsed["errors"],
                "timed_out": r["timed_out"], **parsed}


@mcp.tool()
def echo(file: str | None = None, source: str | None = None,
         defines: dict[str, Any] | None = None) -> dict[str, Any]:
    """Evaluate a script and return only its ECHO output. Use this to read
    computed values out of a model without paying for a mesh render."""
    with ExitStack() as stack:
        src = _input(stack, file, source)
        out = stack.enter_context(
            tempfile.NamedTemporaryFile(suffix=".echo", delete=True))
        r = _run(["-o", out.name, str(src)] + _defines(defines), timeout_s=180)
        parsed = _parse(r["log"] + "\n" + Path(out.name).read_text())
        return {"ok": r["returncode"] == 0, "timed_out": r["timed_out"],
                "echo": parsed["echo"], "warnings": parsed["warnings"],
                "errors": parsed["errors"]}


@mcp.tool()
def render(output: str, file: str | None = None, source: str | None = None,
           defines: dict[str, Any] | None = None,
           timeout_s: int | None = None,
           measure: bool = True,
           camera: str | None = None,
           imgsize: str = "800,600",
           projection: str = "o",
           colorscheme: str = "Tomorrow",
           full_render: bool = False,
           viewall: bool = True) -> dict[str, Any]:
    """Render a script to a file in the workspace. The format is taken from
    the output extension: stl, off, amf, 3mf, dxf, svg, csg, png.

    Returns the manifold report OpenSCAD prints (simple, volumes, facets),
    plus bounding box and volume when the output is an STL.

    A png output keeps the picture instead of handing it back, which is what
    preview does. Use render when the image is the artefact — a before and
    after pair, a figure for a page — and preview when you only want to look.
    The view options below apply to png alone and mean what they mean in
    preview: camera is OpenSCAD's own string, seven numbers for gimbal style
    (translate x,y,z, rotate x,y,z, distance) or six for eye then centre.
    Leave camera unset and viewall frames the model. projection is "o" for
    orthogonal or "p" for perspective. full_render evaluates the real
    geometry rather than the preview approximation: slower, but it shows what
    would export, which matters for anything cut or differenced."""
    ext = Path(output).suffix.lstrip(".").lower()
    if ext not in MESH_FORMATS | FLAT_FORMATS | TEXT_FORMATS | IMAGE_FORMATS:
        raise ValueError(f"unsupported output format: {ext}")
    image = ext in IMAGE_FORMATS
    with ExitStack() as stack:
        src = _input(stack, file, source)
        dst = _safe(output)
        dst.parent.mkdir(parents=True, exist_ok=True)
        args = ["-o", str(dst), str(src)]
        if image:
            args += _view_args(camera, imgsize, projection, colorscheme,
                               full_render, viewall)
        # PNG export wants a GL context; the mesh path never does.
        r = _run(args + _defines(defines), display=image, timeout_s=timeout_s)
        parsed = _parse(r["log"])
        res: dict[str, Any] = {
            "ok": r["returncode"] == 0 and dst.exists() and not parsed["errors"],
            "timed_out": r["timed_out"],
            "output": str(dst.relative_to(WORKSPACE)),
            "bytes": dst.stat().st_size if dst.exists() else 0,
            **parsed,
        }
        if measure and ext == "stl" and dst.exists() and dst.stat().st_size:
            try:
                res["mesh"] = _measure(dst)
            except Exception as exc:
                res["mesh_error"] = str(exc)
        return res


@mcp.tool()
def measure(file: str | None = None, source: str | None = None,
            defines: dict[str, Any] | None = None,
            timeout_s: int | None = None) -> dict[str, Any]:
    """Render to a throwaway STL and report bounding box, size, volume and
    triangle count. Use it to check a part fits the print bed."""
    with ExitStack() as stack:
        src = _input(stack, file, source)
        out = stack.enter_context(
            tempfile.NamedTemporaryFile(suffix=".stl", delete=True))
        r = _run(["-o", out.name, str(src)] + _defines(defines),
                 timeout_s=timeout_s)
        parsed = _parse(r["log"])
        res = {"ok": r["returncode"] == 0, "timed_out": r["timed_out"],
               "stats": parsed["stats"], "warnings": parsed["warnings"],
               "errors": parsed["errors"]}
        if Path(out.name).stat().st_size:
            res["mesh"] = _measure(Path(out.name))
        return res


@mcp.tool()
def preview(file: str | None = None, source: str | None = None,
            defines: dict[str, Any] | None = None,
            camera: str | None = None,
            imgsize: str = "800,600",
            projection: str = "o",
            colorscheme: str = "Tomorrow",
            full_render: bool = False,
            viewall: bool = True,
            timeout_s: int | None = None) -> Image:
    """Render a PNG of the model and return it as an image.

    camera is OpenSCAD's own string. Seven numbers are gimbal style,
    translate x,y,z then rotate x,y,z then distance, for example
    "0,0,0,55,0,25,400". Six numbers are eye x,y,z then centre x,y,z.
    Leave it unset and viewall frames the object automatically.
    projection is "o" for orthogonal or "p" for perspective.
    Set full_render to evaluate the real geometry rather than the
    preview approximation. It is slower but shows what will export."""
    with ExitStack() as stack:
        src = _input(stack, file, source)
        out = stack.enter_context(
            tempfile.NamedTemporaryFile(suffix=".png", delete=True))
        args = (["-o", out.name, str(src)]
                + _view_args(camera, imgsize, projection, colorscheme,
                             full_render, viewall)
                + _defines(defines))
        r = _run(args, display=True, timeout_s=timeout_s)
        data = Path(out.name).read_bytes()
        if not data:
            raise RuntimeError("no image produced\n" + r["log"][-2000:])
        return Image(data=data, format="png")


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
