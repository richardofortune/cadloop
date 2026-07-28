# Machine-aware pipeline

**Status:** design, approved in outline
**Scope:** project A of two. Project B (printability checks) follows and depends on this.

## The problem

Getting the spirograph from a `.scad` file to print-ready plates took about forty
tool calls. Almost none of them were judgement. They were resolving profiles
across two vendors by hand, writing throwaway code to parse G-code bounds,
packing plates and then verifying them, and reading vendor JSON to work out why a
slice had failed.

Every failure in that session was a configuration or environment idiosyncrasy.
Not one was a modelling problem.

| Failure | Cause |
| --- | --- |
| `check_bed_fit` returned `null` for a stock printer | `printable_area` has two spellings; 144 printers inherit it rather than defining it |
| The slicer segfaulted on every call | Auto-detection preferred Creality Print, whose CLI does not run headless on macOS |
| `list_profiles` returned nothing usable | Only Creality Print's profile root was searched |
| `overrides` silently never worked | Emitted `--layer_gcode`; the CLI accepts hyphens only |
| A stock printer failed slicer validation | One vendor's profile omits `use_relative_e_distances`; the other sets it |
| The output would not open in the user's slicer | `.gcode.3mf` is a sliced project, not a model |
| The default render produces an unprintable sheet | 432 x 444 mm against a 220 mm bed |
| **Sliced for a printer the user does not own** | Nothing in the system knew which printer this was for |

The last one is the shape of the whole problem. The pipeline verified gear
meshing to six decimal places and bed fit to a tenth of a millimetre, then
confidently produced G-code for the wrong machine, because "which printer is
this for" was not a thing the system had. It was re-supplied as three filesystem
paths on every call, and nothing checked those paths agreed with each other or
with reality.

## The end state

Someone has an idea, writes or modifies OpenSCAD with an LLM, and the pipeline
takes it the rest of the way to files their printer can run. Seamless, and not a
token-burning machine.

That splits the work in a specific way:

- **Deterministic code** resolves the machine, renders parts, checks bed fit,
  packs plates, slices, and verifies the result is on the bed. All mechanical.
  All of it currently done by an LLM reading data and deciding, which is both
  expensive and where the mistakes came from.
- **The LLM** writes and modifies the OpenSCAD, and reacts to what the pipeline
  reports. That is the actual value: *the 48 tooth wheel only draws ellipses,
  drop it*; *these two shapes overlap on the sheet*; *this wall is thinner than
  your nozzle*.

Closer to one call than forty:

```
make_printable("spirograph.scad")

  machine ....... Ender-3 V3 SE, 0.4 nozzle, generic PLA      (cached)
  parts ......... 16 rendered
  verify ........ 14/14 mesh cleanly, no overlaps on the sheet
  bed fit ....... 16/16 fit individually, the sheet does not (432 x 444)
  plates ........ packed into 6
  sliced ........ 6/6, every extruding move on the bed
  ready ......... ~/cad/plates/   13h06m, 66.5 m PLA

  arranged for you:
    w80 rotated 45 deg, it does not fit square
  worth a look:
    plate3 sits 9.7 mm from the bed edge, consider a brim
```

## Decisions

**Audience.** Any maker, one printer each. Not a fleet, not a personal
hardcode. The vendor mess is absorbed, never exposed.

**The machine is declared once and then invisible.** After setup, no tool takes
a profile path. Profiles remain in the call to the slicer binary because it needs
them, but they are an implementation detail, not something a caller supplies.

**Setup adopts what the human already configured, then proves it.** Creality
Print stores the selected printer, quality and filament in `Creality.conf`;
OrcaSlicer keeps the equivalent. Asking someone to name a printer their slicer
already knows is a worse product.

Reading it blindly is also wrong: in testing, the stored filament was stale by
two changes. So setup reads the configuration, resolves it, proves it by slicing
a 20 mm cube, and **reports the five facts it settled on**. A wrong one gets
corrected in a single line, immediately, instead of three slices later.

Where more than one slicer has a configuration, the one that proves out wins: a
configuration belonging to a binary that cannot slice is not a usable answer. If
several prove out, the most recently modified configuration is taken, and the
choice is named in the report.

Where nothing is configured, or several printers are, setup falls back to a name
and refuses ambiguity rather than guessing. `"K1"` matches five printers across
four nozzle sizes; picking the first is how the wrong machine gets chosen.

**Vendor quirks become probes, not constants.** Hardcoding *prefer OrcaSlicer,
Creality Print is broken* is true today and will rot the moment Creality ships a
fix. Discovering it, by trying what is installed and proving which one actually
slices, is the same outcome now and self-correcting later. Each quirk is also
written up in `docs/known-issues.md` with a link to the upstream issue where one
exists.

**Staleness is checked, not assumed.** The record carries the slicer version and
a hash of each profile. Any call re-checks cheaply and re-runs setup if the
environment moved. A slicer update must not silently change what gets printed.

## What the pipeline may change on its own

Two axes, not one slider. The kind of change, and whether it is necessary.

| | Necessary: it will not print otherwise | Improving: it prints either way |
| --- | --- | --- |
| **Arrangement** — plate, position, rotation | Apply, and say so | Apply, and say so |
| **Slicing settings** — brim, supports, thin-wall handling | Apply, and say so | Report only |
| **Geometry** — the model | Never | Never |

Refusing to rotate a part that only fits on the diagonal is not caution, it is
handing back a failure the pipeline already knew how to avoid. Adding a brim
that merely improves adhesion is advice, and costs cleanup, so it stays advice.

Geometry is never touched. In a parametric model the right fix is usually a
parameter, and the model knows which parameter means what. The pipeline can
measure that a wall is 0.38 mm; only the author knows whether that is a
decorative rib or a gear tooth.

**Material and machine quirks** — warping on large flat parts, first-layer
adhesion, heat creep — do not fit the table cleanly. They are probabilistic and
printer-specific. They are reported, never acted on, and are the natural place
for project B to grow into.

## Tool surface

New:

- `setup_printer(printer=None, filament=None)` — adopt, resolve, prove, store,
  report. Arguments are the fallback when configuration cannot be read or is
  ambiguous.
- `machine()` — the resolved facts: bed, nozzle, flavour, layer height, material.
  The contract everything downstream reads instead of parsing vendor JSON.
- `make_printable(source, parts=None)` — the whole pipeline, one call, returning
  the report above.

Changed, with profile arguments becoming optional overrides rather than required:

- `check_bed_fit(model)` — asks `machine()` for the bed
- `slice_model(models, output)` — profiles default from the machine record

Unchanged: everything in the OpenSCAD server, and `list_profiles`, which becomes
a discovery aid rather than a prerequisite.

This is 0.2.0. The published 0.1.0 signatures keep working, since supplying a
profile explicitly remains legal.

## Internal boundaries

`_bed()`, `_profile_chain()` and `_area_points()` move out of `slicer_server.py`
into a resolver module. They are the most-tested code in the project and are
currently buried in the MCP server, which makes them awkward to use from
`gearcheck` or from project B. The resolver is pure: profiles in, machine facts
out, no subprocess and no MCP.

Plate packing is its own module too. It has one job, it is testable against
known footprints, and it is the piece most likely to be replaced later with
something better than a greedy pass.

## Error handling

Setup fails loudly and stores nothing. A half-valid machine record that fails at
first real slice is worse than no record. Every failure names the step that
broke: which slicer, which profile, what the test cube did.

A step that cannot answer says so rather than guessing. `check_bed_fit` already
does this, returning `ok: null` with a reason when a profile defines no bed, and
that is the pattern: unknown is a third answer, distinct from pass and fail.

## Testing

The existing smoke test drives both servers over real MCP stdio and stays as is.
Added:

- Resolver unit tests over both `printable_area` spellings, an inherited bed, and
  a profile chain that defines no bed anywhere
- A fake slicer that fails validation the way the Ender-3 V3 SE did, so the
  setup failure path is covered without needing that printer
- Packing tests over known footprints, including the case that fooled a 1-D
  check: two circles that do not fit side by side but do fit diagonally
- A staleness test: change a profile hash, confirm the next call re-validates

## Implementation order

Large enough that the plan should sequence it, in two landings that each stand up
on their own:

1. **The machine record.** Resolver module, `setup_printer`, `machine()`,
   staleness, and the existing tools taking their defaults from it. At this point
   the wrong-printer class of failure is gone and every tool call loses its
   profile arguments.
2. **The pipeline.** Packing module and `make_printable` over the top. This is
   where the forty calls become one.

Shipping 1 alone is already worth having. Shipping 2 without 1 is not possible,
since packing and slicing both need resolved machine facts.

## Out of scope

Printability analysis is project B and gets its own spec: overhangs, first-layer
contact area, thin walls, bridges, warping risk. It depends on `machine()`
existing, which is why it comes second.

Also out of scope: multiple stored machines, per-project machine records, driving
Cura or PrusaSlicer, and anything that edits the user's model.
