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
import shutil
import sys
import tempfile
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parent.parent
MOCK = ROOT / "tests" / "mock"
FAILURES: list[str] = []

sys.path.insert(0, str(ROOT / "src"))
from cadloop import pipeline  # noqa: E402
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

            # preview hands the picture back; render has to leave it behind,
            # which is the only way to build a page out of several of them.
            got = payload(await s.call_tool("render",
                          {"file": "box.scad", "output": "box.png",
                           "camera": "0,0,0,55,0,25,120", "imgsize": "240,180"}))
            png = ws / "box.png"
            head = png.read_bytes()[:24] if png.exists() else b""
            # Width and height live in the IHDR at bytes 16..24. Reading them
            # back proves imgsize reached OpenSCAD, so a png that quietly
            # ignored the view options fails here rather than looking fine.
            size = (int.from_bytes(head[16:20], "big"),
                    int.from_bytes(head[20:24], "big")) if head else (0, 0)
            check("render keeps a png, framed as asked",
                  got["ok"] and got["bytes"] > 0
                  and head[:8] == b"\x89PNG\r\n\x1a\n" and size == (240, 180),
                  str(got.get("errors") or size))

            res = await s.call_tool("preview", {"file": "box.scad"})
            check("preview returns an image", len(images(res)) == 1)

            res = await s.call_tool("read_file", {"path": "../../etc/passwd"})
            blocked = bool(getattr(res, "isError", False)) or any(
                "outside the workspace" in c.text
                for c in res.content if c.type == "text")
            check("workspace guard blocks traversal", blocked)


def mock_install(ws: Path) -> tuple[Path, Path]:
    """A slicer install that exists only for this run: the mock profiles,
    copied so a test may edit one, and a slicer config naming the presets a
    user would have selected in the GUI.

    HOME is redirected at the server so adopt() reads this config rather than
    the developer's own, which is what makes the zero-argument path testable
    at all. The profile names are unique to the mock, so a real install on
    the same machine contributes nothing that can be mistaken for it."""
    profiles = ws / "profiles"
    shutil.copytree(MOCK / "profiles", profiles)
    home = ws / "home"
    selected = json.dumps({"presets": {
        "machine": "Mock K1 0.4 nozzle",
        "print": "0.20mm Standard @Mock K1 0.4 nozzle",
        "filaments": ["Hyper PLA @Mock K1 0.4 nozzle"]}})
    for rel in ("Library/Application Support/OrcaSlicer", ".config/OrcaSlicer"):
        d = home / rel
        d.mkdir(parents=True, exist_ok=True)
        (d / "OrcaSlicer.conf").write_text(selected)
    return profiles, home


async def test_slicer(ws: Path) -> None:
    print("slicer server")
    stl = ws / "part.stl"
    if not stl.exists():
        stl.write_text(TETRA)
    prof_dir, home = mock_install(ws)
    params = await session("cadloop.slicer_server", {
        "SLICER_WORKSPACE": str(ws),
        "SLICER_BIN": str(MOCK / "slicer.py"),
        "SLICER_PROFILE_DIRS": str(prof_dir),
        "CADLOOP_MACHINE": str(ws / "machine.json"),
        "HOME": str(home),
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
            # run's own copy is ours to assert on. notaprofile.json must be
            # absent.
            got = payload(await s.call_tool("list_profiles", {"limit": 100000}))
            mine = {Path(p["path"]).name: p["kind"] for p in got["profiles"]
                    if p["path"].startswith(str(prof_dir))}
            check("profiles classified",
                  mine == {"k1.json": "machine", "std.json": "process",
                           "fine.json": "process", "pla.json": "filament",
                           "bedstr.json": "machine", "bedbase.json": "machine",
                           "bedchild.json": "machine"},
                  str(sorted(mine.items())))

            machine = str(prof_dir / "k1.json")
            process = str(prof_dir / "std.json")
            filament = str(prof_dir / "pla.json")

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
                "machine_profile": str(prof_dir / "bedstr.json")}))
            check("bed read from a comma-separated printable_area",
                  got.get("ok") is True and got["bed"]["x_mm"] == 180.0
                  and got["bed"]["z_mm"] == 200.0,
                  str(got.get("bed") or got.get("reason")))

            got = payload(await s.call_tool("check_bed_fit", {
                "model": "part.stl",
                "machine_profile": str(prof_dir / "bedchild.json")}))
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

            got = payload(await s.call_tool("machine_info", {}))
            check("machine_info answers before setup",
                  got["ok"] is None and "setup_printer" in got["reason"],
                  str(got)[:80])

            # ---------------------------------------------------------
            # A1 and A2: the zero-argument path, which is what the branch
            # is judged on and what nothing automated used to touch. No
            # profile path, no binary path and no vendor spelling appears
            # in any call from here down.
            #
            # The mock ships two qualities and the config names the coarser
            # one, which sorts second - so settling for the printer's first
            # process rather than the configured one is visible here.
            # ---------------------------------------------------------
            got = payload(await s.call_tool("setup_printer", {}))
            check("setup_printer succeeds with no arguments",
                  got["ok"] is True, str(got.get("reason"))[:120])
            if got["ok"]:
                chose = got.get("chose") or {}
                check("setup names the printer, quality and filament",
                      chose.get("printer") == "Mock K1 0.4 nozzle"
                      and chose.get("quality") ==
                          "0.20mm Standard @Mock K1 0.4 nozzle"
                      and chose.get("filament") ==
                          "Hyper PLA @Mock K1 0.4 nozzle",
                      str(chose))
                check("setup adopts what the slicer was configured with",
                      (got.get("adopted") or {}).get("printer")
                      == "Mock K1 0.4 nozzle",
                      str(got.get("adopted"))[:100])
                triple = got["machine"]["profiles"]
                check("the stored triple comes from one install",
                      len({str(Path(p).parent) for p in triple.values()}) == 1,
                      str(sorted(triple.values()))[:120])
                check("setup proved the printer by slicing",
                      got["machine"]["proof"]["test_slice_ok"] is True)

            got = payload(await s.call_tool("machine_info", {}))
            check("machine_info names the printer after setup",
                  got["ok"] is True and got["name"] == "Mock K1 0.4 nozzle"
                  and got["derived"]["bed"]["x_mm"] == 220.0,
                  str(got.get("reason") or got.get("name")))

            got = payload(await s.call_tool("check_bed_fit",
                                            {"model": "part.stl"}))
            check("bed fit works with no profile argument",
                  got.get("ok") is True and got["bed"]["x_mm"] == 220.0,
                  str(got.get("reason") or got.get("bed")))

            got = payload(await s.call_tool("slice_model", {
                "models": ["part.stl"], "output": "out/auto.gcode.3mf"}))
            check("slice works with no profile arguments",
                  got.get("ok") is True, str(got.get("reason"))[:120])
            check("the slice used the stored triple",
                  got.get("command") is not None
                  and str(prof_dir) in " ".join(got["command"]),
                  str(got.get("command", []))[:80])

            # ---------------------------------------------------------
            # A4, A5 and A8: one call from a .scad to plates, every plate
            # proven against the bed by reading its own G-code back, and a
            # report that fits a screen and says what to do next. This is
            # the only automated end-to-end coverage of the three, and it
            # goes through the same MCP session a model would use.
            #
            # It needs a real OpenSCAD, because rendering is the half of
            # the pipeline no mock stands in for here.
            # ---------------------------------------------------------
            try:
                _openscad_binary()
            except RuntimeError:
                print("  skip  no openscad, make_printable not exercised")
            else:
                (ws / "pair.scad").write_text(
                    'part = "small";\n'
                    'if (part == "small") cube([20, 20, 10]);\n'
                    'else cube([35, 25, 8]);\n')
                got = payload(await s.call_tool(
                    "make_printable", {"source": "pair.scad",
                                       "parts": ["small", "large"]}))
                check("make_printable takes a model to plates in one call",
                      got.get("ok") is True, str(got.get("reason"))[:120])
                parts, plates = got.get("parts") or [], got.get("plates") or []
                check("both parts rendered and fit the bed",
                      [p["name"] for p in parts] == ["small", "large"]
                      and all(p["rendered"] and p["fits"] for p in parts),
                      str([(p["name"], p["fits"]) for p in parts]))
                check("it produced at least one plate", len(plates) >= 1,
                      f"{len(plates)} plate(s)")
                # A5 is proven, not assumed: on_bed comes from reading the
                # extruding moves out of the G-code the slicer just wrote.
                check("every plate is proven on the bed",
                      bool(plates) and all(p["on_bed"] is True for p in plates),
                      str([(p["name"], p["on_bed"]) for p in plates]))
                check("the plates are really on disk",
                      bool(plates) and all((ws / p["gcode"]).stat().st_size
                                           for p in plates),
                      str([p["gcode"] for p in plates]))
                # A8: the screen of text comes back with the report, over
                # the wire, so a model reading the JSON has it in hand.
                report = got.get("summary") or ""
                lines = report.splitlines()
                check("the report comes back rendered, not just as facts",
                      report == pipeline.summary(got),
                      lines[0][:60] if lines else "no summary")
                check("the report fits one screen", 0 < len(lines) <= 30,
                      f"{len(lines)} lines")
                check("the report says what to do next without fetching more",
                      any(l.strip().startswith("next:") for l in lines)
                      and got["output"] in report,
                      lines[-1].strip()[:60] if lines else "")

            # A3: a setup that no longer matches reality is refused, not
            # warned about, and refused before anything is written.
            (prof_dir / "k1.json").write_text(json.dumps(
                {"type": "machine", "name": "Mock K1 0.4 nozzle",
                 "printable_area": ["0x0", "60x0", "60x60", "0x60"],
                 "printable_height": "60"}))
            got = payload(await s.call_tool("check_bed_fit",
                                            {"model": "part.stl"}))
            check("an edited profile refuses the bed check",
                  got.get("ok") is None and "machine profile changed"
                  in (got.get("reason") or ""),
                  str(got.get("reason"))[:100])

            before = set(p.name for p in (ws / "out").iterdir())
            got = payload(await s.call_tool("slice_model", {
                "models": ["part.stl"], "output": "out/stale.gcode.3mf"}))
            check("an edited profile refuses the slice",
                  got.get("ok") is None, str(got.get("reason"))[:100])
            check("the refused slice wrote nothing",
                  set(p.name for p in (ws / "out").iterdir()) == before)


async def main() -> int:
    # Nothing here knows about the spirograph. cadloop's tests exercise
    # cadloop; the example checks itself, in models/verify_spirograph.py.
    with tempfile.TemporaryDirectory() as tmp:
        # Resolved: the servers resolve their workspace and their profile
        # roots, and on macOS /var is a symlink to /private/var, so an
        # unresolved fixture path matches nothing they report back.
        ws = Path(tmp).resolve()
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
