"""One call from a .scad file to plates this printer will actually run.

Render each part, measure it, group what fits onto as few plates as a shelf
packer manages, slice each plate, and then read the finished G-code back and
prove every extruding move lands inside the bed. The last step is the point:
a slicer will happily write G-code that prints off the edge, and the only
evidence that it did not is the file itself.

Two rules shape everything here.

The pipeline never edits the model. Which plate a part lands on and where it
sits are ours to choose; the geometry is not. A part that cannot print as
designed is reported, never quietly shrunk or split. The one change we do
make to a part is turning it ninety degrees when it does not fit square, and
that is named in the report rather than left for someone to notice.

A refusal is not a failure. Nothing set up, a machine record that no longer
matches the disk, no OpenSCAD to render with: those return ok None with a
reason and touch nothing, because a plate sliced for a printer that moved is
worse than no plate at all. ok False is reserved for work that was attempted
and did not come out right, and ok None also covers a plate whose G-code we
could not read well enough to vouch for.
"""

from __future__ import annotations

import re
import textwrap
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from . import gcode as _gcode
from . import machine as _machine
from . import openscad_server as _openscad
from . import packing as _packing
from . import slicer_server as _slicer
from .common import measure_stl, run as _sh, safe_path, scad_literal

# The variable a multi-part .scad switches on. Every model in this repo
# spells it "part", and the convention is worth more than a knob.
PART_VARIABLE = "part"
OUTPUT_DIR = "plates"
RENDER_TIMEOUT_S = 900
# Closer than this to the edge and a warped first layer starts to matter.
# Not a failure — the part is on the bed — but worth a human's eye.
BRIM_MARGIN_MM = 10.0
# What summary() promises: one screen, and it holds however long the lists
# get, because the lists are what get cut.
SUMMARY_LINES = 30
SUMMARY_WIDTH = 78

_NUMERIC = re.compile(r"^-?\d+(?:\.\d+)?$")
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")
_WS_LOCK = threading.RLock()


# --------------------------------------------------------------------
# plumbing
# --------------------------------------------------------------------

def _blank_report() -> dict[str, Any]:
    """The report's shape, with nothing in it. Kept in one place so a
    refusal and a success are the same object to anyone reading them."""
    return {"ok": None, "reason": "", "machine": None, "parts": [],
            "plates": [], "arranged": [], "attention": [], "output": None,
            "totals": {"plates": 0, "minutes": 0, "filament_m": 0.0}}


def _refuse(reason: str, **extra: Any) -> dict[str, Any]:
    return {**_blank_report(), "ok": None, "reason": reason, **extra}


def _names(items: list[str], limit: int = 3) -> str:
    if len(items) <= limit:
        return ", ".join(items)
    return ", ".join(items[:limit]) + f" and {len(items) - limit} more"


@contextmanager
def _slicer_workspace(ws: Path):
    """Point slicer_server at the workspace run() was handed.

    slice_model and extract_gcode resolve their relative paths against
    slicer_server.WORKSPACE, a module global read from SLICER_WORKSPACE at
    import time. run() is given a workspace explicitly and that one is the
    authority — passing absolute paths would not help, since _safe refuses
    anything outside the module's own root. Under the MCP tool the two are
    the same object and nothing is touched; the swap exists for callers
    (tests, an embedder) that name a different directory.
    """
    if _slicer.WORKSPACE == ws:
        yield
        return
    with _WS_LOCK:
        was = _slicer.WORKSPACE
        _slicer.WORKSPACE = ws
        try:
            yield
        finally:
            _slicer.WORKSPACE = was


def _scad_value(entry: Any) -> Any:
    """What to hand OpenSCAD for one entry of `parts`.

    A tooth count is a number to the model — spirograph dispatches on
    is_num(part) — but it arrives over MCP as the string "36", because the
    published signature is list[str]. A string that is entirely a number is
    passed as one; anything else goes through as a string. The report keeps
    the caller's spelling either way.
    """
    if isinstance(entry, (bool, int, float)):
        return entry
    text = str(entry)
    if _NUMERIC.match(text):
        return float(text) if "." in text else int(text)
    return text


def _stem(name: str) -> str:
    return _UNSAFE.sub("_", name).strip("_") or "part"


def _record_exists() -> bool:
    """Whether there is a machine record file at all, however bad."""
    try:
        return _machine.record_path().is_file()
    except Exception:
        return False


def _plate_facts(summary: dict[str, Any] | None) -> tuple[int | None,
                                                          float | None]:
    """Minutes and filament metres out of a sliced archive's slice_info.

    Every value in there is a string, and a slicer that omits one is not an
    error — it just means we cannot report that number, so it stays None
    rather than becoming a confident zero.
    """
    info = (summary or {}).get("slice_info") or {}
    plates = info.get("plates") if isinstance(info, dict) else None
    minutes: int | None = None
    metres: float | None = None
    for plate in plates or []:
        if not isinstance(plate, dict):
            continue
        try:
            minutes = (minutes or 0) + int(round(float(plate["prediction"])
                                                 / 60.0))
        except (KeyError, TypeError, ValueError):
            pass
        for fil in plate.get("filaments") or []:
            try:
                metres = (metres or 0.0) + float(fil["used_m"])
            except (KeyError, TypeError, ValueError):
                pass
    return minutes, (round(metres, 2) if metres is not None else None)


# --------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------

def _render(binary: str, source: Path, outdir: Path, workspace: Path,
            name: str, define: Any) -> dict[str, Any]:
    """Render one part to its own STL and measure it.

    Returns a part record whose "stl" is None when nothing usable came out.
    An empty mesh counts as nothing usable: OpenSCAD exits 0 having written
    a valid STL with no triangles when `part` names something the model does
    not know, and packing an object with no size would put a ghost on a
    plate.
    """
    stl = outdir / (_stem(name) + ".stl")
    args = [binary, "-o", str(stl), str(source)]
    if define is not None:
        args += ["-D", f"{PART_VARIABLE}={scad_literal(define)}"]
    rec: dict[str, Any] = {"name": name, "rendered": False, "stl": None,
                           "size_mm": None, "volume_mm3": None,
                           "fits": None, "plate": None, "reason": ""}
    r = _sh(args, RENDER_TIMEOUT_S)
    if r["timed_out"]:
        rec["reason"] = f"OpenSCAD did not finish within {RENDER_TIMEOUT_S}s"
        return rec
    parsed = _openscad._parse(r["log"])
    if r["returncode"] != 0 or parsed["errors"] or not stl.exists():
        rec["reason"] = (parsed["errors"][-1] if parsed["errors"]
                         else f"OpenSCAD exited {r['returncode']} without "
                              f"writing a mesh")
        return rec
    mesh = measure_stl(stl)
    if not mesh.get("triangles"):
        rec["reason"] = (f"rendered no geometry — does the model know a part "
                         f"called {name!r}?")
        return rec
    rec.update({"rendered": True, "stl": str(stl.relative_to(workspace)),
                "size_mm": mesh["size_mm"], "volume_mm3": mesh["volume_mm3"]})
    return rec


# --------------------------------------------------------------------
# the pipeline
# --------------------------------------------------------------------

def run(source: str, workspace: str | Path,
        parts: list[Any] | None = None) -> dict[str, Any]:
    """Take a model to files this printer can run, and prove they fit.

    source is a .scad file inside workspace. parts names the values of the
    model's `part` variable to render; None renders the whole file once.
    """
    workspace = Path(workspace).expanduser().resolve()

    # 1. The source, before anything else. A path that leaves the workspace
    #    is refused by name, not by traceback.
    try:
        src = safe_path(workspace, source)
    except ValueError as exc:
        return _refuse(str(exc))
    if not src.is_file():
        return _refuse(f"{source} is not a file in the workspace ({workspace})")

    # 2. The printer. Missing or stale, and nothing else happens: a plate
    #    sliced against a setup that no longer matches the disk is worse
    #    than no plate, because it looks finished.
    try:
        rec = _machine.load()
    except _machine.RecordError as exc:
        return _refuse(str(exc))
    if rec is None:
        # load() answers None for two different situations: nothing has been
        # set up, and something was set up but the file no longer parses. It
        # refuses either way, but "run setup_printer" is the right next
        # action only for the first; for the second the user wants to know
        # their record is damaged, not be told they never made one.
        return _refuse("no printer set up yet, run setup_printer"
                       if not _record_exists() else
                       f"the machine record at {_machine.record_path()} is "
                       f"there but cannot be read as a printer setup. Delete "
                       f"it and run setup_printer.")
    why = _machine.staleness(rec)
    if why:
        return _refuse("the stored printer no longer matches what is on "
                       f"disk: {why}. Run setup_printer.")

    derived = rec.get("derived") or {}
    bed = _machine.bed(rec)
    machine_block = {"name": rec.get("name"), "bed": bed,
                     "filament": derived.get("filament_type")}
    if not bed.get("x_mm") or not bed.get("y_mm"):
        return _refuse("this printer's profile declares no printable area, "
                       "so nothing can be checked against its bed. Run "
                       "setup_printer.", machine=machine_block)

    wanted: list[Any] = [None] if parts is None else list(parts)
    if not wanted:
        return _refuse("no parts named, so there is nothing to render",
                       machine=machine_block)

    # 3. Render every part. The binary is found the way the OpenSCAD server
    #    finds it; not having one is a refusal, not a failed render.
    try:
        binary = _openscad._binary()
    except RuntimeError as exc:
        return _refuse(f"no OpenSCAD to render with: {exc}",
                       machine=machine_block)

    outdir = workspace / OUTPUT_DIR / _stem(src.stem)
    outdir.mkdir(parents=True, exist_ok=True)

    attention: list[str] = []
    arranged: list[str] = []
    rendered: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in wanted:
        name = src.stem if entry is None else str(entry)
        if _stem(name) in seen:
            attention.append(f"{name} was named more than once; it is "
                             f"rendered once. Ask for two copies of a part "
                             f"by slicing its plate twice.")
            continue
        seen.add(_stem(name))
        rendered.append(_render(binary, src, outdir, workspace, name,
                                None if entry is None else _scad_value(entry)))

    by_name = {p["name"]: p for p in rendered}
    for part in rendered:
        if not part["rendered"]:
            attention.append(f"{part['name']} did not render: {part['reason']}")

    # 4. Group what rendered onto plates. The packer decides which parts
    #    share a plate and whether one fits at all; the slicer arranges
    #    within the plate, so the packer's x/y are advisory and only its
    #    rotation is a decision anyone needs told about.
    packed = _packing.pack(
        [{"name": p["name"], "w": p["size_mm"][0], "d": p["size_mm"][1]}
         for p in rendered if p["rendered"]], bed)
    for miss in packed["unplaceable"]:
        by_name[miss["name"]].update({"fits": False, "reason": miss["reason"]})
        attention.append(f"{miss['name']} does not fit: {miss['reason']}")
    for index, plate in enumerate(packed["plates"], start=1):
        for placed in plate["parts"]:
            by_name[placed["name"]].update({"fits": True,
                                            "plate": f"plate_{index}"})
            if placed["rotated"]:
                arranged.append(f"{placed['name']} turned 90 degrees, it does "
                                f"not fit square")

    # 5 and 6. Slice each plate, then read its G-code back and check it
    #    against the bed. Steps 5 and 6 are one loop because a plate that
    #    sliced but prints off the bed has not been delivered, and the only
    #    way to know is the file the slice just wrote.
    plates: list[dict[str, Any]] = []
    refusal: str | None = None
    with _slicer_workspace(workspace):
        for index, plate in enumerate(packed["plates"], start=1):
            entry, refusal = _slice_plate(index, plate, by_name, outdir,
                                          workspace, bed, attention)
            if refusal is not None:
                break
            plates.append(entry)

    # 7. The report.
    off = [p["name"] for p in plates if p["on_bed"] is False]
    unsliced = [p["name"] for p in plates if not p["sliced"]]
    unrendered = [p["name"] for p in rendered if not p["rendered"]]
    unfit = [m["name"] for m in packed["unplaceable"]]
    # A plate that sliced but yielded no G-code file has no deliverable, so
    # it is a failure. A plate whose G-code is on disk but says nothing a
    # reader can vouch for is unknown. The two look alike in the plate
    # record — both have on_bed None — and the file is what tells them
    # apart, so that is what is asked.
    no_gcode = [p["name"] for p in plates if p["sliced"] and not p["gcode"]]
    unproven = [p["name"] for p in plates
                if p["sliced"] and p["gcode"] and p["on_bed"] is None]

    report = _blank_report()
    report.update({
        "machine": machine_block,
        "parts": rendered,
        "plates": plates,
        "arranged": arranged,
        "attention": attention,
        "output": str(outdir),
        "totals": {
            # What came out, not what was attempted: a plate that failed to
            # slice is in plates[] with its reason, but it is not a plate
            # anyone can print.
            "plates": sum(1 for p in plates if p["sliced"]),
            "minutes": sum(p["minutes"] or 0 for p in plates),
            "filament_m": round(sum(p["filament_m"] or 0.0 for p in plates), 2),
        },
    })

    problems = []
    if off:
        problems.append(f"{_names(off)} prints off the bed")
    if unsliced:
        problems.append(f"{_names(unsliced)} did not slice")
    if no_gcode:
        problems.append(f"{_names(no_gcode)} sliced but produced no G-code")
    if unrendered:
        problems.append(f"{_names(unrendered)} did not render")
    if unfit:
        problems.append(f"{_names(unfit)} does not fit this bed")

    if refusal is not None:
        # The refusal is real and it stopped the run, so ok stays None. It
        # does not get to erase what was already established, though: parts
        # that did not render and parts too big for the bed were checked
        # and found wanting before the slicer was ever asked, and demoting
        # those facts to "unknown" loses work the caller has to redo.
        report["ok"] = None
        report["reason"] = "; ".join(problems + [refusal])
    elif problems:
        report["ok"], report["reason"] = False, "; ".join(problems)
    elif not plates:
        report["ok"] = None
        report["reason"] = "no part reached a plate, so nothing was sliced"
    elif unproven:
        # Deliberately does not say why. A plate lands here both when its
        # G-code says nothing a reader can vouch for and when the G-code is
        # perfectly legible but the bed has no height to check it against,
        # and the top line guessing between them is how "could not be read"
        # ended up describing a file of ninety thousand readable moves. The
        # plate's own reason says which.
        report["ok"] = None
        report["reason"] = (f"{_names(unproven)} sliced, but could not be "
                            f"proven on the bed")
    else:
        report["ok"], report["reason"] = True, ""
    return report


def _slice_plate(index: int, plate: dict[str, Any],
                 by_name: dict[str, dict[str, Any]], outdir: Path,
                 workspace: Path, bed: dict[str, Any], attention: list[str],
                 ) -> tuple[dict[str, Any], str | None]:
    """Slice one plate and prove where it prints.

    Returns the plate record and, in place of it, a refusal reason when the
    slicer declined the call outright. A refusal is a fact about the
    printer rather than about this plate, so it stops the run instead of
    being recorded against whichever plate happened to meet it first.
    """
    name = f"plate_{index}"
    members = [p["name"] for p in plate["parts"]]
    entry: dict[str, Any] = {"name": name, "parts": members, "sliced": False,
                             "on_bed": None, "minutes": None,
                             "filament_m": None, "archive": None,
                             "gcode": None, "reason": ""}
    archive = str((outdir / f"{name}.gcode.3mf").relative_to(workspace))
    plain = str((outdir / f"{name}.gcode").relative_to(workspace))

    try:
        # arrange=1 leaves final placement to the slicer, which is why the
        # packer's x/y are advisory. orient is deliberately not passed: a
        # part turned for the slicer's convenience is a change to the part
        # nobody asked for, and if arrange fails to turn one that only fits
        # turned, the fit check below catches it.
        sliced = _slicer.slice_model(models=[by_name[m]["stl"] for m in members],
                                     output=archive, plate=1, arrange=1)
    except Exception as exc:
        entry["reason"] = f"the slicer could not be run: {exc}"
        attention.append(f"{name} did not slice: {entry['reason']}")
        return entry, None
    if sliced.get("ok") is None:
        return entry, (sliced.get("reason") or "the slicer refused the call")
    if not sliced.get("ok") or not sliced.get("output"):
        entry["reason"] = ("; ".join(sliced.get("errors") or [])
                           or ("the slicer timed out" if sliced.get("timed_out")
                               else f"the slicer exited "
                                    f"{sliced.get('returncode')}"))
        attention.append(f"{name} did not slice: {entry['reason']}")
        return entry, None
    entry["sliced"] = True
    entry["archive"] = archive
    entry["minutes"], entry["filament_m"] = _plate_facts(sliced.get("summary"))

    try:
        _slicer.extract_gcode(archive, plain, plate=1)
    except Exception as exc:
        entry["reason"] = f"no G-code came out of the archive: {exc}"
        attention.append(f"{name} sliced, but {entry['reason']}")
        return entry, None
    entry["gcode"] = plain

    fit = _gcode.fits(outdir / f"{name}.gcode", bed)
    entry["on_bed"] = fit["ok"]
    if fit["ok"] is False:
        entry["reason"] = fit["reason"]
        attention.append(f"{name} prints off the bed: {fit['reason']}")
    elif fit["ok"] is None:
        # Unreadable G-code and a bed with no printable height both land
        # here. Which one it was is gcode.fits's business to say, not this
        # module's to work out — a caller that has to reconstruct why a
        # check could not conclude is a caller compensating for a fact kept
        # somewhere else.
        entry["reason"] = fit["reason"]
        attention.append(f"{name} could not be proven on the bed: "
                         f"{fit['reason']}")
    else:
        extent = fit["extent"]
        gap = min(extent["x"][0], bed["x_mm"] - extent["x"][1],
                  extent["y"][0], bed["y_mm"] - extent["y"][1])
        if gap < BRIM_MARGIN_MM:
            attention.append(f"{name} sits {gap:.1f} mm from the bed edge, "
                             f"consider a brim")
    return entry, None


# --------------------------------------------------------------------
# the report, as a human reads it
# --------------------------------------------------------------------
#
# Everything below renders the report and adds nothing to it. Two things
# decide the shape.
#
# The three answers must look like three answers. ok, FAILED and UNKNOWN are
# three different words in the same column, no one of them a substring of
# another, so a reader skimming the first line cannot turn "could not tell"
# into "checked and fine". That mistake is the expensive one: a plate nobody
# proved reads exactly like a plate that passed, right up until it prints.
#
# And the next action is stated, never implied. A reader — person or model —
# should not have to work out what to do from a diagnosis, or fetch anything
# to decide. So the last line is always a call to make: print these, run
# setup_printer, fix this in the .scad and come back. What is not shown is
# counted rather than dropped, because "and 12 more" is a fact and silence
# is not.

_STATUS = {True: "ok", False: "FAILED", None: "UNKNOWN"}

# The refusals run() can produce, each with the thing to do about it. Matched
# on a phrase from the reason rather than carried in the report, because a
# summary has to work on a report it did not build — a refusal is often just
# an ok and a reason, and that has to be enough.
_NEXT_BY_REASON: tuple[tuple[str, str], ...] = (
    ("cannot be read as a printer setup",
     "delete the machine record named above, then run setup_printer."),
    ("cannot read the machine record",
     "make the machine record above readable, or delete it, then run "
     "setup_printer."),
    ("no longer matches what is on disk",
     "run setup_printer again. The slicer or one of its profiles moved since "
     "this printer was set up, and nothing is sliced against a setup that "
     "no longer matches."),
    ("declares no printable area",
     "run setup_printer and choose a printer whose profile declares a bed. "
     "Nothing can be checked against this one."),
    ("CADLOOP_MACHINE",
     "point CADLOOP_MACHINE at a file this can write, or unset it to use the "
     "default location, then run setup_printer."),
    ("setup_printer", "run setup_printer, then call make_printable again."),
    ("outside the workspace",
     "call make_printable with a path inside the workspace. Nothing outside "
     "it is readable."),
    ("is not a file in the workspace",
     "check the name, then call make_printable again with a .scad that is in "
     "the workspace."),
    ("no OpenSCAD to render with",
     "install OpenSCAD, or point OPENSCAD_BIN at it, then call make_printable "
     "again."),
    ("no parts named",
     "name the parts you want, or call make_printable with no parts at all to "
     "render the whole file once."),
    ("no part reached a plate",
     "nothing reached a plate. Fix the parts named above in the .scad, then "
     "call make_printable again."),
)


def _n(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def _mm(value: Any) -> str:
    """220.0 as "220", 63.17 as "63.17"."""
    number = float(value)
    return str(int(number)) if number == int(number) else f"{number:g}"


def _hm(minutes: Any) -> str:
    total = int(minutes)
    return f"{total}m" if total < 60 else f"{total // 60}h{total % 60:02d}m"


def _wrapped(text: str, indent: str = "", hang: str | None = None) -> list[str]:
    """One field, as the lines it takes. Wrapped rather than truncated: the
    line budget is spent on the lists, which can be counted instead of
    listed, not on a sentence that would lose its ending."""
    lines = textwrap.wrap(" ".join(str(text).split()), width=SUMMARY_WIDTH,
                          initial_indent=indent,
                          subsequent_indent=indent if hang is None else hang,
                          break_long_words=False, break_on_hyphens=False)
    return lines or [indent + str(text)]


def _fact(label: str, value: str) -> list[str]:
    return _wrapped(value, f"  {(label + ' ').ljust(14, '.')} ", " " * 17)


def _plate_name(plate: dict[str, Any]) -> str:
    return str(plate.get("name") or "a plate")


def _was_sliced(plate: dict[str, Any]) -> bool:
    """Whether a plate record describes something the slicer produced.

    run() always writes "sliced", so this only matters for a report built by
    hand or trimmed down. Anything known about where a plate prints, or any
    G-code named, means it was sliced; nothing known means it was not."""
    if "sliced" in plate:
        return bool(plate["sliced"])
    return plate.get("on_bed") is not None or bool(plate.get("gcode"))


def _unprintable(plates: list[dict[str, Any]],
                 ) -> tuple[list[str], list[str]]:
    """Plates that were sliced and still cannot be printed, split into the
    two answers they are.

    A plate whose archive held no G-code has no deliverable: that is a
    failure, and there is no file for anyone to go and look at. A plate with
    G-code on disk that says nothing a reader can vouch for is unknown, and
    the file is right there. Both carry on_bed None, so only the G-code
    tells them apart — and a report that never mentioned one is not
    claiming there is none, so an absent key means neither."""
    sliced = [p for p in plates if _was_sliced(p)]
    void = [p for p in sliced if "gcode" in p and not p["gcode"]]
    empty = {id(p) for p in void}
    return ([_plate_name(p) for p in void],
            [_plate_name(p) for p in sliced
             if p.get("on_bed") is None and id(p) not in empty])


def _by_reason(reason: str) -> str | None:
    for phrase, action in _NEXT_BY_REASON:
        if phrase in reason:
            return action
    return None


def _next_action(report: dict[str, Any]) -> str:
    """The call to make next, in words a caller can act on without asking
    anything else. Never a diagnosis: the top line already carries that."""
    ok = report.get("ok")
    reason = " ".join((report.get("reason") or "").split())
    parts = report.get("parts") or []
    plates = report.get("plates") or []
    output = report.get("output")
    where = f"in {output}" if output else "this run wrote"

    if ok is True:
        if report.get("attention"):
            return (f"print the plates {where}. Nothing above stops the print "
                    f"— the notes are advisories — so read them and go.")
        return f"print the plates {where}. Nothing needs a decision."

    # A refusal names its own remedy, and it outranks anything found earlier:
    # no printer means no plate, however many parts rendered cleanly first.
    if ok is None:
        refusal = _by_reason(reason)
        if refusal:
            return refusal

    no_mesh = [str(p.get("name")) for p in parts if p.get("rendered") is False]
    too_big = [str(p.get("name")) for p in parts if p.get("fits") is False]
    off = [_plate_name(p) for p in plates if p.get("on_bed") is False]
    unsliced = [_plate_name(p) for p in plates if not _was_sliced(p)]
    void, unproven = _unprintable(plates)

    if no_mesh:
        return (f"fix {_names(no_mesh)} in the .scad — the note above is what "
                f"OpenSCAD said — then call make_printable again.")
    if too_big:
        return (f"{_names(too_big)} is bigger than this bed. Make it smaller "
                f"or split it in the .scad, then call make_printable again. "
                f"Nothing was resized for you.")
    if off:
        return (f"do not print {_names(off)}. Call make_printable again with "
                f"fewer parts, so that plate carries less, and check it again.")
    if unsliced:
        return (f"{_names(unsliced)} did not slice, and the note above says "
                f"why. Fix that, then call make_printable again.")
    if void:
        # Sliced, and nothing came out of the archive. There is no file to
        # read, so "go and check the G-code" would send a caller after
        # something that does not exist.
        return (f"{_names(void)} produced no G-code, so there is nothing to "
                f"print. Call make_printable again; if it repeats, the "
                f"slicer is writing an archive this cannot read.")
    if unproven:
        return (f"do not print {_names(unproven)} yet: nothing proved where it "
                f"lands. Read the G-code {where}, or slice that plate on its "
                f"own and look again.")

    known = _by_reason(reason)
    if known:
        return known
    if plates or parts:
        return ("nothing here is printable. The top line says why. Fix that, "
                "then call make_printable again.")
    return ("nothing was made and nothing was changed. The top line says why. "
            "Fix that, then call make_printable again.")


def _headline(report: dict[str, Any]) -> str:
    reason = " ".join((report.get("reason") or "").split())
    if reason:
        return reason
    if report.get("ok") is True:
        parts, plates = report.get("parts") or [], report.get("plates") or []
        made = [_n(len(parts), "part")] if parts else []
        made += [_n(len(plates), "plate")] if plates else []
        return (" on ".join(made) or "done") + \
               (", every plate proven on the bed" if plates else "")
    return ("nothing came out of this run" if report.get("ok") is False
            else "nothing was made, and nothing was changed")


def _facts(report: dict[str, Any]) -> list[str]:
    """What it did, one fact to a line, each omitted when the report does not
    carry it. Parts are counted rather than enumerated: sixteen names would
    cost half the screen and say less than "16 of 16 fit"."""
    lines: list[str] = []
    printer = report.get("machine") or {}
    parts = report.get("parts") or []
    plates = report.get("plates") or []
    totals = report.get("totals") or {}
    output = report.get("output")

    if printer:
        bed = printer.get("bed") or {}
        bits = [str(printer.get("name") or "an unnamed printer")]
        if bed.get("x_mm") and bed.get("y_mm"):
            bits.append(f"{_mm(bed['x_mm'])} x {_mm(bed['y_mm'])} mm bed")
        if printer.get("filament"):
            bits.append(str(printer["filament"]))
        lines += _fact("machine", ", ".join(bits))

    if parts:
        fits = sum(1 for p in parts if p.get("fits") is True)
        text = f"{fits} of {len(parts)} fit this bed"
        big = sum(1 for p in parts if p.get("fits") is False)
        gone = sum(1 for p in parts if p.get("fits") is None)
        if big:
            text += f", {big} too big"
        if gone:
            text += f", {gone} did not render"
        lines += _fact("parts", text)

    if plates:
        held = sum(len(p.get("parts") or []) for p in plates)
        lines += _fact("plates", f"{_n(held, 'part')} packed onto {len(plates)}"
                                 if held else _n(len(plates), "plate"))

        sliced = [p for p in plates if _was_sliced(p)]
        off = [_plate_name(p) for p in plates if p.get("on_bed") is False]
        void, unproven = _unprintable(plates)
        told = [f"{len(sliced)} of {len(plates)}"]
        if off:
            told.append(f"{_names(off)} prints off the bed")
        if void:
            told.append(f"{_names(void)} produced no G-code")
        if unproven:
            told.append(f"{_names(unproven)} not proven on the bed")
        if not off and not void and not unproven and sliced:
            told.append("every extruding move on the bed")
        lines += _fact("sliced", ", ".join(told))

    if report.get("ok") is True and totals.get("plates"):
        # "ready" is a promise, so it is made only when the run kept it. A
        # partial run still says where its files are, under a word that
        # claims nothing about them.
        bits = [str(output)] if output else []
        if totals.get("minutes"):
            bits.append(_hm(totals["minutes"]))
        if totals.get("filament_m"):
            bits.append(f"{_mm(totals['filament_m'])} m "
                        f"{printer.get('filament') or 'filament'}")
        lines += _fact("ready", ", ".join(bits))
    elif output:
        lines += _fact("files", str(output))
    return lines


def _section(title: str, items: list[str], budget: int) -> list[str]:
    """A titled list inside a line budget, with the remainder counted.

    Costs a blank line and the title before any item, so a budget under
    three lines buys nothing worth printing and the whole section becomes
    its own count."""
    if budget < 3:
        return ["", f"  {title} {len(items)} in the report"]
    lines = ["", f"  {title}"]
    left, shown = budget - 2, 0
    for item in items:
        wrapped = _wrapped(item, "    ", "      ")
        # Keep a line back for the count unless this is the last item.
        if len(wrapped) + (1 if shown + 1 < len(items) else 0) > left:
            break
        lines += wrapped
        left -= len(wrapped)
        shown += 1
    if shown < len(items):
        lines.append(f"    and {len(items) - shown} more")
    return lines


def summary(report: dict[str, Any]) -> str:
    """The report as one screen of text, ending in what to do next.

    Takes any report run() produces, and any subset of one: a refusal is
    often nothing but an ok and a reason, and that has to render too."""
    status = _STATUS.get(report.get("ok"), "UNKNOWN")
    head = _wrapped(_headline(report), f"{status:<7} ", " " * 8)
    facts = _facts(report)
    if facts:
        head += [""] + facts
    tail = [""] + _wrapped(f"next: {_next_action(report)}", "  ", " " * 8)

    blocks = [(title, list(items)) for title, items in
              (("arranged for you:", report.get("arranged") or []),
               ("worth a look:", report.get("attention") or [])) if items]
    body: list[str] = []
    room = SUMMARY_LINES - len(head) - len(tail)
    for done, (title, items) in enumerate(blocks):
        # An even split of what is left, and whatever the first section does
        # not spend goes to the second rather than to nobody.
        share = room // (len(blocks) - done)
        rendered = _section(title, items, share)
        body += rendered
        room -= len(rendered)
    return "\n".join(head + body + tail)
