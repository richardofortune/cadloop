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
                       (default ~/cad)
  SLICER_PROFILE_DIRS  extra profile roots, os.pathsep separated
  SLICER_TIMEOUT       seconds before a slice is killed (default 600)
  CADLOOP_MACHINE      where the machine record setup_printer writes lives
                       (default $XDG_CONFIG_HOME/cadloop/machine.json)

Run:  python creality_mcp.py
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import zipfile
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from mcp.server.fastmcp import FastMCP

from . import machine as _machine
from . import slicers as _slicers
from .common import (find_binary, measure_stl, run as _sh,
                     safe_path, tail as _tail_, workspace)
from .profiles import (classify, machine_facts, profile_roots,
                       reset_cache, root_of)

WORKSPACE = workspace("SLICER_WORKSPACE", "cad")
DEFAULT_TIMEOUT = int(os.environ.get("SLICER_TIMEOUT", "600"))

mcp = FastMCP("creality-slicer")


class Refused(RuntimeError):
    """This call cannot be answered from what is known, and answering it
    anyway would produce G-code nobody validated."""


# --------------------------------------------------------------------
# plumbing
# --------------------------------------------------------------------

_PROBED: dict[str, dict[str, Any]] = {}


def _probe_once(binary: str) -> dict[str, Any]:
    """probe() runs the binary, so the answer is cached for the life of the
    process. A slicer does not grow or lose CLI flags while we watch."""
    if binary not in _PROBED:
        _PROBED[binary] = _slicers.probe(binary)
    return _PROBED[binary]


def _unusable(binary: str) -> str | None:
    """Why this path cannot be handed to subprocess as a program, or None."""
    p = Path(binary)
    if p.is_dir():
        return "is a directory, not a program"
    if not p.exists():
        return "is not there any more"
    if not p.is_file():
        return "is not a regular file"
    if not os.access(str(p), os.X_OK):
        return "is not executable"
    return None


def _detect() -> str:
    """A slicer to run when nothing has been set up and nothing overridden.

    Every candidate is probed and one that passes is used. If several pass,
    this refuses rather than picking: the order of a list in this file is not
    a reason to prefer one vendor's slicer over another's, and the whole
    point of setup_printer is to make that choice deliberately, once."""
    found = _slicers.installed()
    if not found:
        raise RuntimeError(
            "no slicer found in any known install location; set SLICER_BIN "
            "to the binary path")
    working = [c["binary"] for c in found if _probe_once(c["binary"])["ok"]]
    if not working:
        raise RuntimeError(
            "no installed slicer answered --help with the flags needed to "
            "slice; set SLICER_BIN to the binary path")
    if len(working) > 1:
        raise RuntimeError(
            "more than one installed slicer can slice ("
            + ", ".join(working)
            + "); run setup_printer, or set SLICER_BIN, rather than letting "
              "the order of a list decide which one your G-code comes from")
    return working[0]


def _binary() -> str:
    """The slicer to run: an explicit SLICER_BIN always wins, then the
    binary the stored machine record proved works, then detection.

    A record that staleness() rejects supplies nothing. Falling through to
    detection there was how a slice could end up running on a slicer that
    was never proven against these profiles, silently."""
    env = os.environ.get("SLICER_BIN")
    if env:
        # names and candidates are unreachable with the variable set; the
        # search space lives in slicers.SLICER_CANDIDATES now.
        return find_binary("SLICER_BIN", [], [])
    rec = _machine.load()
    if not rec:
        return _detect()
    why = _machine.staleness(rec)
    if why:
        raise Refused(
            f"the stored printer no longer matches what is on disk: {why}. "
            "Run setup_printer. Refusing to substitute a different slicer.")
    recorded = (rec.get("slicer") or {}).get("binary")
    if not recorded:
        return _detect()
    bad = _unusable(recorded)
    if bad:
        raise Refused(f"the recorded slicer {recorded} {bad}. Run setup_printer.")
    probed = _probe_once(recorded)
    if not probed["ok"]:
        raise Refused(f"the recorded slicer {recorded} no longer runs: "
                      f"{probed['reason']}. Run setup_printer.")
    was = (rec.get("fingerprint") or {}).get("version")
    if was and probed["version"] and probed["version"] != was:
        raise Refused(
            f"the recorded slicer is now version {probed['version']}, and the "
            f"printer was proven against {was}. Run setup_printer.")
    return recorded


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


def _active_profiles(machine_profile: str | None = None,
                     process_profile: str | None = None,
                     filament_profiles: list[str] | None = None,
                     needs: tuple[str, ...] = ("machine", "process", "filament"),
                     ) -> tuple[dict[str, Any], str | None]:
    """Profiles for this call: whatever was passed, then the stored machine.

    Explicit arguments still win, field by field, so every 0.1.0 call site
    keeps working. Two things that field-by-field filling gets wrong on its
    own are corrected here.

    A record that no longer matches the disk is refused rather than reported.
    A3 says a setup that does not match reality is never used, and a warning
    attached to the result of the call that already did the work is not that:
    the bed had already been read from a stale cache and the G-code already
    written by the time anyone could act on it.

    And a machine profile given explicitly is not paired with a stored
    process and filament from some other install. That combination is exactly
    the cross-vendor triple this record exists to eliminate, arrived at from
    the other direction.

    needs is the kinds this particular call actually consumes. It decides
    which profiles must be supplied before the record is consulted, and what
    a refusal names as missing. It does not narrow the staleness check:
    staleness() reports on every fingerprinted profile, so check_bed_fit is
    refused over a stale process profile it never reads. That errs towards
    refusing, which is the safe direction, and a record stale in any part is
    a record whose printer moved."""
    given = {
        "machine": machine_profile,
        "process": process_profile,
        "filament": list(filament_profiles) if filament_profiles else [],
    }
    supplied = {k for k in needs if given[k]}
    rec: dict[str, Any] | None = None
    warn: str | None = None
    if supplied != set(needs):
        rec = _machine.load()
        if rec is None:
            raise Refused(
                "no printer set up and no "
                + " or ".join(sorted(set(needs) - supplied))
                + " profile given. Run setup_printer.")
        warn = _machine.staleness(rec)
        if warn:
            raise Refused(
                f"the stored printer no longer matches what is on disk: {warn}. "
                "Run setup_printer, or pass the profiles explicitly.")
    stored = (rec or {}).get("profiles") or {}
    out = {
        "machine": given["machine"] or stored.get("machine"),
        "process": given["process"] or stored.get("process"),
        "filament": given["filament"] or ([stored["filament"]]
                                          if stored.get("filament") else []),
    }
    missing = [k for k in needs if not out[k]]
    if missing:
        raise Refused("no printer set up and no " + " or ".join(missing)
                      + " profile given. Run setup_printer.")

    if supplied and rec is not None:
        roots = {}
        for kind in needs:
            value = out[kind][0] if kind == "filament" else out[kind]
            r = root_of(value)
            if r is not None:
                roots[kind] = str(r)
        if len(set(roots.values())) > 1:
            raise Refused(
                "the profiles given and the ones stored come from different "
                "slicer installs, and a triple assembled across two installs "
                "is not a configuration anyone tested: "
                + "; ".join(f"{k} from {v}" for k, v in sorted(roots.items()))
                + ". Pass all of them, or none.")
    return out, warn


def _refusal(exc: Exception) -> dict[str, Any]:
    """A refusal is not a failure: nothing was concluded, so ok is null."""
    return {"ok": None, "reason": str(exc), "stale": str(exc)}


# --------------------------------------------------------------------
# tools
# --------------------------------------------------------------------

@mcp.tool()
def slicer_info() -> dict[str, Any]:
    """Report the slicer binary, its version banner, and the full --help flag
    surface. Call this first. The Orca-family CLI changes between releases
    and Creality's fork diverges, so trust this over any assumed flag list."""
    try:
        binary = _binary()
    except (Refused, _machine.RecordError) as exc:
        return {**_refusal(exc), "workspace": str(WORKSPACE),
                "profile_roots": [str(p) for p in profile_roots()]}
    r = _sh([binary, "--help"], 60)
    flags = sorted(set(re.findall(r"--[a-z][a-z0-9-]+", r["log"] or "")))
    return {
        "binary": binary,
        "workspace": str(WORKSPACE),
        "profile_roots": [str(p) for p in profile_roots()],
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
    for root in profile_roots():
        for p in root.rglob("*.json"):
            rec = classify(p)
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
            "roots_searched": [str(p) for p in profile_roots()]}


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
    try:
        r = _run(["--info", str(src)], timeout_s=timeout_s or 120)
    except (Refused, _machine.RecordError) as exc:
        return _refusal(exc)
    return {"ok": r["returncode"] == 0, "timed_out": r["timed_out"],
            "output": _tail(r["log"])}


@mcp.tool()
def check_bed_fit(model: str, machine_profile: str | None = None,
                  margin_mm: float = 3.0) -> dict[str, Any]:
    """Compare an STL's footprint against the machine profile's printable
    area before slicing. The slicer will happily emit out-of-bounds G-code
    without complaining, so this is worth doing first.

    machine_profile defaults to the stored machine record, set up once by
    setup_printer. Pass it explicitly to check against a different printer.
    A stored printer that no longer matches the disk is refused here rather
    than used, because answering from a cached bed is how a part that does
    not fit gets told that it does.

    Rotating 45 degrees in Z is checked too, since a part that misses the
    bed square-on often fits on the diagonal."""
    try:
        profs, warn = _active_profiles(machine_profile, needs=("machine",))
    except (Refused, _machine.RecordError) as exc:
        # height_checked rides along on every return, including the ones that
        # answer nothing, so a caller can read it without guarding first.
        return {**_refusal(exc), "height_checked": False}
    stl = _safe(model)
    mesh = measure_stl(stl)
    if not mesh.get("triangles"):
        return {"ok": None, "reason": "no geometry in that mesh", "mesh": mesh,
                "height_checked": False, "stale": warn}
    # One answer to "how big is this bed", shared with the pipeline, so the
    # two tools cannot disagree: the live profile over the record's cached
    # copy, merged field by field. A profile named explicitly is read on its
    # own terms, since the stored record describes a different machine.
    bed = _machine.bed(None if machine_profile else _machine.load(),
                       machine_profile=profs["machine"])
    # A bed can be non-empty and still have nothing to measure against: a
    # record whose cached bed kept a height after the profile stopped
    # declaring a printable_area merges to {"z_mm": 250.0}, which is truthy
    # and has no width. Guard on the dimensions this needs, not on the dict
    # having something in it, or the next line raises KeyError out of an MCP
    # tool call instead of saying what is wrong.
    if not bed.get("x_mm") or not bed.get("y_mm"):
        return {"ok": None,
                "reason": ("no printable_area in machine profile" if not bed
                           else "this machine profile's printable area has no "
                                "width or depth, so there is nothing to "
                                "measure a footprint against"),
                "mesh": mesh, "bed": bed, "height_checked": False,
                "stale": warn}
    sx, sy, sz = mesh["size_mm"]
    diag = (sx + sy) * 0.70711        # footprint of a 45 degree rotation
    fits = (sx + margin_mm <= bed["x_mm"] and sy + margin_mm <= bed["y_mm"])
    fits_rot = (diag + margin_mm <= bed["x_mm"]
                and diag + margin_mm <= bed["y_mm"])
    height = bed.get("z_mm")
    tall = height is not None and sz > height
    on_bed = (fits or fits_rot) and not tall
    # A part that fits the footprint but whose height nobody can check is
    # not a part that fits. Answering True here skipped the height test
    # rather than failing it, so a 900 mm column came back printable on a
    # profile that declares no printable_height. A part with no height at
    # all is still answerable: there is nothing to check.
    unchecked_z = height is None and sz > 0
    # A no is worth as much as a yes only if it says why. These read in the
    # same voice as the unchecked-height reason above, and in the same voice
    # as gcode.fits, so a part refused here and a plate refused there explain
    # themselves the same way.
    bad: list[str] = []
    if not (fits or fits_rot):
        over = []
        if sx + margin_mm > bed["x_mm"]:
            over.append(f"x overruns by "
                        f"{round(sx + margin_mm - bed['x_mm'], 3)} mm")
        if sy + margin_mm > bed["y_mm"]:
            over.append(f"y overruns by "
                        f"{round(sy + margin_mm - bed['y_mm'], 3)} mm")
        bad.append(f"the {sx} x {sy} mm footprint plus a {margin_mm} mm "
                   f"margin does not fit this {bed['x_mm']} x {bed['y_mm']} mm "
                   f"bed square or turned 45 degrees ({', '.join(over)}; the "
                   f"45 degree diagonal is {round(diag, 3)} mm)")
    if tall:
        bad.append(f"the part is {sz} mm tall, {round(sz - height, 3)} mm "
                   f"more than this printer's {height} mm")
    return {
        "ok": None if (on_bed and unchecked_z) else bool(on_bed),
        "reason": (f"the footprint fits, but this machine profile declares no "
                   f"printable height, so the part's {sz} mm in z is unchecked"
                   if on_bed and unchecked_z else "; ".join(bad)),
        "fits_square": fits,
        "fits_rotated_45": fits_rot,
        "too_tall": bool(tall),
        "height_checked": height is not None,
        "part_size_mm": mesh["size_mm"],
        "bed": bed,
        "margin_mm": margin_mm,
        "volume_mm3": mesh["volume_mm3"],
        "stale": warn,
    }


@mcp.tool()
def slice_model(
    models: list[str],
    machine_profile: str | None = None,
    process_profile: str | None = None,
    filament_profiles: list[str] | None = None,
    output: str | None = None,
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
    machine profile is passed before the process profile. All three default
    to the stored machine record, set up once by setup_printer, so a normal
    call needs none of them. output defaults to None only to preserve the
    published positional parameter order; it is still required and raises
    if missing.

    overrides is a map of any slicer setting key to a value, passed as
    command-line flags. These beat both the profiles and anything embedded
    in a project file. Values go through as strings, which is what the
    Orca-family CLI expects. Write the key either way round: the profiles
    spell settings with underscores and the CLI only accepts hyphens, so
    they are hyphenated here. Not every setting is exposed as a flag, and
    one that is not fails the whole run with "Invalid option", so check a
    new key with dry_run first. Note the CLI uses the current name rather
    than the PrusaSlicer one, `layer_change_gcode` rather than `layer_gcode`.

    plate 0 slices every plate. arrange 1 packs multiple models onto the
    bed; without it they stack at the origin. Nothing here checks that the
    parts fit the bed, so measure them first.

    Set dry_run to see the exact command without running it."""
    if not output:
        raise ValueError("output is required (it defaults to None only to keep "
                         "the published positional order)")
    # Before anything is written: a setup that does not match reality has to
    # surface here, not in the returned summary of a file already on disk.
    try:
        profs, warn = _active_profiles(machine_profile, process_profile,
                                       filament_profiles)
        # The binary is settled here too. A record that cannot supply one is
        # a refusal with a reason, not a traceback out of the tool call.
        binary = _binary()
    except (Refused, _machine.RecordError) as exc:
        return _refusal(exc)
    srcs = [_safe(m) for m in models]
    for s in srcs:
        if not s.exists():
            raise FileNotFoundError(f"{s} not found")
    dst = _safe(output)
    dst.parent.mkdir(parents=True, exist_ok=True)

    settings = f"{Path(profs['machine']).expanduser()};{Path(profs['process']).expanduser()}"
    filaments = ";".join(str(Path(f).expanduser()) for f in profs["filament"])

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
        # Settings are named with underscores in the profile JSON and with
        # hyphens on the command line, so take either spelling and emit the
        # one the CLI accepts. Anything else is rejected as an invalid option.
        args += [f"--{k.lstrip('-').replace('_', '-')}", str(v)]
    args += list(extra_args or [])
    args += [str(s) for s in srcs]

    if dry_run:
        return {"dry_run": True, "command": [binary] + args, "stale": warn}

    r = _sh([binary] + args, timeout_s or DEFAULT_TIMEOUT)
    res: dict[str, Any] = {
        "ok": r["returncode"] == 0 and dst.exists(),
        "returncode": r["returncode"],
        "timed_out": r["timed_out"],
        "command": r["command"],
        "output": str(dst) if dst.exists() else None,
        "errors": _errors(r["log"]),
        "log_tail": _tail(r["log"], 2000),
        "stale": warn,
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


@mcp.tool()
def make_printable(source: str, parts: list[str] | None = None) -> dict[str, Any]:
    """Take a model to files this printer can run, in one call.

    Renders each part, checks it against the bed, packs what fits onto as
    few plates as it can, slices them, and proves every printed feature
    lands on the bed. Reports what it arranged for you and what wants a
    human. Never edits the model: a part that cannot print as designed is
    reported, not altered."""
    from . import pipeline
    report = pipeline.run(source, WORKSPACE, parts)
    # The report carries every fact; "summary" is the one screen of it that
    # a reader acts on, and it travels with the report because the caller
    # on the other side of this is usually a model reading JSON. Asking it
    # to render the report itself to find out what to do next is asking it
    # to fetch something, which is the one thing the summary exists to
    # avoid.
    return {**report, "summary": pipeline.summary(report)}


# --------------------------------------------------------------------
# machine setup
# --------------------------------------------------------------------

def _prove(binary: str, profs: dict) -> dict[str, Any]:
    """Slice a 20mm cube. The only check that would have caught a stock
    printer failing slicer validation, which happens at slice time and is
    invisible to any static inspection of the profiles."""
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        stl = d / "cube.stl"
        stl.write_text(_machine.CUBE_STL)
        out = d / "cube.gcode.3mf"
        args = ["--slice", "1",
                "--load-settings", f"{profs['machine']};{profs['process']}",
                "--load-filaments", profs["filament"],
                "--export-3mf", str(out), str(stl)]
        r = _sh([binary] + args, 300)
        if r["timed_out"]:
            return {"ok": False, "reason": "test slice timed out"}
        if not out.exists():
            return {"ok": False,
                    "reason": f"test slice exited {r['returncode']}: "
                              f"{_tail(r['log'], 200).strip() or 'no output'}"}
        return {"ok": True, "reason": "", "bytes": out.stat().st_size}


@mcp.tool()
def setup_printer(printer: str | None = None,
                  filament: str | None = None,
                  process: str | None = None) -> dict[str, Any]:
    """Work out which printer this is for, once, and remember it.

    Reads what your slicer is already configured with, resolves it to a
    profile triple, proves the combination by slicing a 20mm cube, and
    stores the result. Every other tool then works without profile
    arguments. Pass printer, filament or process only to override what is
    configured, or when nothing is; each overrides that field alone, and the
    rest still come from your slicer's own settings.

    Reports the printer, the quality and the filament it settled on, and
    stores nothing at all unless the test slice succeeded."""
    # Profiles installed since this process started are otherwise invisible,
    # so a setup that failed for want of them keeps failing until restart.
    reset_cache()
    report: dict[str, Any] = {"adopted": None, "steps": [], "notes": []}

    want_printer, want_filament, want_process = printer, filament, process
    for cand in _slicers.adopt():
        report["adopted"] = {"from": cand["source"],
                             "printer": cand["printer"],
                             "process": cand["process"],
                             "filament": cand["filament"]}
        # Field by field, so naming a printer does not throw away the quality
        # setting the user chose in their slicer's own interface.
        want_printer = want_printer or cand["printer"]
        want_filament = want_filament or cand["filament"]
        want_process = want_process or cand["process"]
        break

    demanded = [k for k, v in (("process", process), ("filament", filament))
                if v]
    res = _machine.resolve(want_printer, want_filament, process=want_process,
                           explicit=demanded)
    if not res["ok"]:
        report["notes"] = res.get("notes") or []
        return {"ok": False, "reason": res["reason"],
                "candidates": res["candidates"], **report}
    report["notes"] = res.get("notes") or []
    report["chose"] = {"printer": res["printer"],
                       "quality": res["names"]["process"],
                       "filament": res["names"]["filament"]}

    # An explicit SLICER_BIN wins here as it does everywhere else: proving a
    # printer against a slicer the caller has told us not to use proves
    # nothing about the one it will actually run on.
    try:
        chosen = find_binary("SLICER_BIN", [], []) if os.environ.get(
            "SLICER_BIN") else None
    except RuntimeError as exc:
        return {"ok": False, "reason": str(exc), **report}
    found = ([{"family": _slicers.family_of(chosen), "binary": chosen}]
             if chosen else _slicers.installed())
    if not found:
        return {"ok": None,
                "reason": "no slicer found in any known install location, so "
                          "whether this printer works here cannot be "
                          "established; set SLICER_BIN or install one",
                **report}

    working = []
    for cand in found:
        p = _slicers.probe(cand["binary"])
        report["steps"].append({"binary": cand["binary"], "ok": p["ok"],
                                "reason": p["reason"], "version": p["version"]})
        if p["ok"]:
            working.append({**cand, "version": p["version"]})
    if not working:
        return {"ok": False, "reason": "no installed slicer answered --help "
                                       "with the flags needed to slice", **report}

    for cand in working:
        proof = _prove(cand["binary"], res["profiles"])
        if proof["ok"]:
            facts = machine_facts(res["profiles"]["machine"],
                                  res["profiles"]["process"],
                                  res["profiles"]["filament"])
            rec = {"schema": _machine.SCHEMA, "name": res["printer"],
                   "slicer": {"family": cand["family"], "binary": cand["binary"],
                              "version": cand["version"]},
                   "profiles": res["profiles"], "derived": facts,
                   "fingerprint": _machine.fingerprint(
                       cand["binary"], cand["version"], res["profiles"]),
                   "proof": {"test_slice_ok": True,
                             "gcode_bytes": proof.get("bytes")}}
            try:
                _machine.save(rec)
            except _machine.RecordError as exc:
                # Every read path answers a bad CADLOOP_MACHINE with a reason;
                # the one path that writes was still raising. Nothing is
                # stored either way, so failing loudly here costs only the
                # proof slice already run.
                return {"ok": None, "reason": str(exc), **report}
            return {"ok": True, "machine": rec, **report}
        report["steps"].append({"binary": cand["binary"], "ok": False,
                                "reason": proof["reason"]})

    return {"ok": False,
            "reason": "every working slicer failed to slice the test cube",
            **report}


@mcp.tool()
def machine_info() -> dict[str, Any]:
    """The printer this workspace is set up for, and whether it is still
    current. Returns ok: null when setup has never run."""
    try:
        rec = _machine.load()
    except _machine.RecordError as exc:
        return {"ok": None, "reason": str(exc)}
    if rec is None:
        return {"ok": None, "reason": "no printer set up yet, run setup_printer"}
    why = _machine.staleness(rec)
    return {"ok": why is None, "reason": why or "",
            "name": rec.get("name"), "slicer": rec.get("slicer"),
            "derived": rec.get("derived")}


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
