#!/usr/bin/env python3
"""End to end smoke test: drives both servers over real MCP stdio sessions.

The OpenSCAD half runs against a real OpenSCAD, found the same way the server
finds it (OPENSCAD_BIN, then PATH, then the usual install locations), and skips
if there is none. The slicer half always runs, against a mock binary that emits an
Orca-shaped help text and a representative .gcode.3mf, so argument
construction and archive parsing are covered without a slicer installed.

    python tests/smoke.py
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import tempfile
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parent.parent
MOCK = ROOT / "tests" / "mock"
FAILURES: list[str] = []

sys.path.insert(0, str(ROOT / "src"))
from cadloop.openscad_server import _binary as _openscad_binary  # noqa: E402

# a 40 x 40 x 30 tetrahedron, so bed-fit has something real to measure
TETRA = """solid part
facet normal 0 0 -1
  outer loop
    vertex 0 0 0
    vertex 40 0 0
    vertex 0 40 0
  endloop
endfacet
facet normal 0 -1 0
  outer loop
    vertex 0 0 0
    vertex 0 0 30
    vertex 40 0 0
  endloop
endfacet
facet normal -1 0 0
  outer loop
    vertex 0 0 0
    vertex 0 40 0
    vertex 0 0 30
  endloop
endfacet
facet normal 1 1 1
  outer loop
    vertex 40 0 0
    vertex 0 0 30
    vertex 0 40 0
  endloop
endfacet
endsolid part
"""


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if ok else 'FAIL'}  {label}{'  ' + detail if detail else ''}")
    if not ok:
        FAILURES.append(label)


def payload(result):
    for c in result.content:
        if c.type == "text":
            try:
                return json.loads(c.text)
            except json.JSONDecodeError:
                return c.text
    return None


def images(result):
    return [c for c in result.content if c.type == "image"]


async def session(module: str, env: dict[str, str]):
    return StdioServerParameters(
        command=sys.executable, args=["-m", module],
        env={**os.environ, "PYTHONPATH": str(ROOT / "src"), **env})


async def test_openscad(ws: Path) -> None:
    print("openscad server")
    try:
        _openscad_binary()
    except RuntimeError:
        print("  skip  no openscad found (set OPENSCAD_BIN)")
        return
    params = await session("cadloop.openscad_server",
                           {"OPENSCAD_WORKSPACE": str(ws)})
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            names = {t.name for t in (await s.list_tools()).tools}
            check("tools exposed", {"check", "echo", "render", "measure",
                                    "preview"} <= names, str(sorted(names)))

            (ws / "box.scad").write_text(
                'w=20; h=12;\necho(str("v=", w*w*h));\n'
                'cube([w,w,h], center=true);\n')

            got = payload(await s.call_tool("check", {"file": "box.scad"}))
            check("check passes clean source", got["ok"] is True)

            got = payload(await s.call_tool("check", {"source": "cube([1,2)"}))
            check("check catches a syntax error",
                  got["ok"] is False and bool(got["errors"]))

            got = payload(await s.call_tool("echo", {"file": "box.scad",
                                                     "defines": {"w": 30}}))
            check("defines reach the script", any("10800" in e for e in got["echo"]),
                  str(got["echo"]))

            got = payload(await s.call_tool("measure", {"file": "box.scad",
                                                        "defines": {"w": 30, "h": 5}}))
            check("measure returns the right box",
                  got["mesh"]["size_mm"] == [30.0, 30.0, 5.0]
                  and abs(got["mesh"]["volume_mm3"] - 4500) < 0.5,
                  str(got["mesh"]["size_mm"]))

            got = payload(await s.call_tool("render",
                          {"file": "box.scad", "output": "box.stl"}))
            check("render writes an stl", got["ok"] and got["bytes"] > 0)

            res = await s.call_tool("preview", {"file": "box.scad"})
            check("preview returns an image", len(images(res)) == 1)

            res = await s.call_tool("read_file", {"path": "../../etc/passwd"})
            blocked = bool(getattr(res, "isError", False)) or any(
                "outside the workspace" in c.text
                for c in res.content if c.type == "text")
            check("workspace guard blocks traversal", blocked)


async def test_slicer(ws: Path) -> None:
    print("slicer server")
    stl = ws / "part.stl"
    if not stl.exists():
        stl.write_text(TETRA)
    params = await session("cadloop.slicer_server", {
        "SLICER_WORKSPACE": str(ws),
        "SLICER_BIN": str(MOCK / "slicer.py"),
        "SLICER_PROFILE_DIRS": str(MOCK / "profiles"),
        "CADLOOP_MACHINE": str(ws / "machine.json"),
    })
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            names = {t.name for t in (await s.list_tools()).tools}
            check("tools exposed", {"slicer_info", "list_profiles",
                                    "check_bed_fit", "slice_model",
                                    "slice_summary", "extract_gcode"} <= names)

            got = payload(await s.call_tool("slicer_info", {}))
            check("help surface parsed", "--slice" in got["flags"],
                  f"{len(got['flags'])} flags")

            # SLICER_PROFILE_DIRS is additive, so a machine with a real slicer
            # installed contributes hundreds of its own profiles here. Only the
            # mock three are ours to assert on. notaprofile.json must be absent.
            got = payload(await s.call_tool("list_profiles", {"limit": 100000}))
            mock_dir = str(MOCK / "profiles")
            mine = {Path(p["path"]).name: p["kind"] for p in got["profiles"]
                    if p["path"].startswith(mock_dir)}
            check("profiles classified",
                  mine == {"k1.json": "machine", "std.json": "process",
                           "pla.json": "filament", "bedstr.json": "machine",
                           "bedbase.json": "machine", "bedchild.json": "machine"},
                  str(sorted(mine.items())))

            machine = str(MOCK / "profiles" / "k1.json")
            process = str(MOCK / "profiles" / "std.json")
            filament = str(MOCK / "profiles" / "pla.json")

            got = payload(await s.call_tool("slice_model", {
                "models": ["part.stl"], "machine_profile": machine,
                "process_profile": process, "filament_profiles": [filament],
                "output": "out/part.gcode.3mf", "dry_run": True}))
            cmd = got["command"]
            i = cmd.index("--load-settings")
            check("machine loads before process",
                  cmd[i + 1] == f"{machine};{process}")
            check("models come last", cmd[-1].endswith("part.stl"))

            # Settings are written with underscores in the profiles but the
            # CLI only accepts hyphens, and rejects the whole run as an
            # invalid option otherwise. Both spellings must come out hyphenated.
            got = payload(await s.call_tool("slice_model", {
                "models": ["part.stl"], "machine_profile": machine,
                "process_profile": process, "filament_profiles": [filament],
                "output": "out/part.gcode.3mf", "dry_run": True,
                "overrides": {"layer_change_gcode": "G92 E0",
                              "--sparse-infill-density": "15%"}}))
            cmd = got["command"]
            check("overrides are hyphenated for the CLI",
                  "--layer-change-gcode" in cmd
                  and "--sparse-infill-density" in cmd
                  and not any(a.startswith("--") and "_" in a for a in cmd),
                  str([a for a in cmd if a.startswith("--")][-4:]))
            check("override values follow their flag",
                  cmd[cmd.index("--layer-change-gcode") + 1] == "G92 E0")

            got = payload(await s.call_tool("slice_model", {
                "models": ["part.stl"], "machine_profile": machine,
                "process_profile": process, "filament_profiles": [filament],
                "output": "out/part.gcode.3mf"}))
            check("slice succeeds", got["ok"] is True)
            plate = got["summary"]["slice_info"]["plates"][0]
            check("filament usage parsed", plate["filaments"][0]["used_g"] == "29.8")
            check("settings recovered",
                  got["summary"]["settings_used"]["printer_model"] == "Creality K1")

            got = payload(await s.call_tool("extract_gcode", {
                "archive": "out/part.gcode.3mf", "output": "out/part.gcode"}))
            check("gcode extracted", got["bytes"] > 0 and
                  got["header"].get("layer_height") == "0.2")

            got = payload(await s.call_tool("check_bed_fit", {
                "model": "part.stl", "machine_profile": machine}))
            check("bed fit passes a 40mm part on a 220mm bed",
                  got.get("ok") is True and got["part_size_mm"] == [40.0, 40.0, 30.0],
                  f"bed {got.get('bed', {}).get('x_mm')}mm")

            # Stock profiles write printable_area either way, and most Bambu
            # machines carry none of their own at all. Both broke check_bed_fit.
            got = payload(await s.call_tool("check_bed_fit", {
                "model": "part.stl",
                "machine_profile": str(MOCK / "profiles" / "bedstr.json")}))
            check("bed read from a comma-separated printable_area",
                  got.get("ok") is True and got["bed"]["x_mm"] == 180.0
                  and got["bed"]["z_mm"] == 200.0,
                  str(got.get("bed") or got.get("reason")))

            got = payload(await s.call_tool("check_bed_fit", {
                "model": "part.stl",
                "machine_profile": str(MOCK / "profiles" / "bedchild.json")}))
            check("bed inherited from a parent profile",
                  got.get("ok") is True and got["bed"]["x_mm"] == 200.0
                  and got["bed"]["z_mm"] == 180.0,
                  str(got.get("bed") or got.get("reason")))

            big = ws / "huge.stl"
            big.write_text(TETRA.replace("40 0 0", "400 0 0")
                                .replace("0 40 0", "0 400 0"))
            got = payload(await s.call_tool("check_bed_fit", {
                "model": "huge.stl", "machine_profile": machine}))
            check("bed fit rejects an oversized part", got.get("ok") is False)

            # With a machine set up, the profile arguments become optional.
            # This is the whole point: a caller can no longer supply three
            # profiles that disagree, because it supplies none.
            got = payload(await s.call_tool("machine_info", {}))
            check("machine_info answers before setup",
                  got["ok"] is None and "setup_printer" in got["reason"],
                  str(got)[:80])


def test_model() -> None:
    """The verifier keeps its own copy of the wheel list, because it has to
    work from a wheel install where the model is not present. Two copies
    drift, so this is the thing that notices."""
    print("model")
    scad = ROOT / "models" / "spirograph.scad"
    if not scad.is_file():
        print("  skip  no spirograph.scad in this checkout")
        return
    m = re.search(r"^wheel_teeth\s*=\s*\[([^\]]*)\]", scad.read_text(), re.M)
    if not m:
        check("wheel_teeth found in the model", False)
        return
    from_scad = [int(n) for n in re.findall(r"\d+", m.group(1))]
    try:
        from cadloop.gearcheck import WHEELS
    except ImportError:
        print("  skip  shapely not installed, cannot compare wheel lists")
        return
    check("the verifier's wheel list matches the model's",
          from_scad == list(WHEELS), f"{from_scad} vs {list(WHEELS)}")


async def main() -> int:
    test_model()
    with tempfile.TemporaryDirectory() as tmp:
        ws = Path(tmp)
        await test_openscad(ws)
        await test_slicer(ws)
    print()
    if FAILURES:
        print(f"{len(FAILURES)} check(s) failed: {', '.join(FAILURES)}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
