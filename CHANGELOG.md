# Changelog

## 0.3.0 — 2026-08-04

The package no longer carries one project's model checks. `render` can keep a
picture instead of only handing it back.

### Changed — existing tools

This affects callers written against 0.2.0.

- **`cadloop-verify` has been removed.** It ran the spirograph example's own
  checks, and the gear half ran off constants compiled into the package, so on
  an install with no model on disk it printed `14/14 parts mesh cleanly` about
  parts the caller did not have. Those checks now live in the repository
  alongside the model they describe: clone it and run
  `python models/verify_spirograph.py`. The command still exists in this
  release as a stub that explains the move and **exits 2**, so a Makefile or CI
  step calling it stops rather than appearing to pass. The stub goes away in
  0.4.0.
- **`cadloop` no longer pulls in shapely.** It was only ever needed by the
  removed checks. `pip install cadloop` now installs `mcp` and nothing else.
  The `verify` extra remains, for the worked example.

### Added

- **`render` writes images.** A `.png` output is kept in the workspace rather
  than returned, and takes the same `camera`, `imgsize`, `projection`,
  `colorscheme` and `full_render` options `preview` has. Use `preview` to look
  at a model and `render` when the image is the artefact — a before-and-after
  pair, a figure for a page. Since `render` already accepted `source` as text,
  the older half of a comparison is `git show <rev>:model.scad` piped in, with
  no checkout and no temporary file.

### Fixed

- `docs/known-issues.md` described the Creality Print CLI crash as specific to
  macOS and to 7.1.1. It is neither; it reproduces on 7.2.0 under Windows 11,
  and does not depend on 3MF input the way the upstream title suggests.

## 0.2.0 — 2026-07-30

One call now takes a model to plates a printer can run, and every plate is
proved to land on the bed before you are told it is ready.

### Added

- **`make_printable(source, parts=None)`** — renders each part, measures it
  against the bed, packs what fits onto as few plates as it can, slices them,
  and checks that every extruding move in the resulting G-code is inside the
  printable area. Returns the full report plus a rendered `summary` string.
  Two calls take `spirograph.scad` to five print-ready plates; the same work
  previously took roughly forty.
- **`setup_printer()` and `machine_info()`** — the printer is settled once and
  reused, so no ordinary call needs a profile path.
- `check_bed_fit` gained `height_checked`, and a `reason` on its main return.

### Changed — existing tools

These affect callers written against 0.1.0.

- **`check_bed_fit` may answer `null` where it used to answer `true`.** A part
  whose footprint fits, on a printer whose profile declares no
  `printable_height`, is now unproven rather than passed. Two axes out of three
  is not proof.
- **`check_bed_fit().bed` no longer carries a `z_mm` key when the height is
  unknown.** 0.1.0 emitted `"z_mm": null`. Read it with `bed.get("z_mm")`. An
  absent key reads as "not known"; `null` read as "known to be nothing".
- **`check_bed_fit().bed` is now the merged bed** — the record's cached bed
  under the live profile, field by field. 0.1.0 used the live profile alone and
  fell back to the record only when the profile yielded nothing at all. A
  profile that has lost its `printable_height` while its record still has one
  now reports `too_tall: true` where 0.1.0 said `false`.
- **`check_bed_fit` no longer raises on a partial bed.** A machine profile
  declaring a height but no printable area returned `KeyError` out of the tool;
  it now refuses with a reason.
- `reason` on `check_bed_fit` is populated when the answer is `false`, not only
  when it is `null`. The answer a user most needs explained used to be the one
  with no text.

Unchanged despite appearances: **`machine_info().derived.bed` still emits
`"z_mm": null`.** The stored record keeps the profile's own shape; only
`check_bed_fit` and the pipeline report use the stripped one.

### Notes

- `mcp` is pinned `<2`. Version 2.0.0 removed `mcp.server.fastmcp` entirely and
  there is no drop-in replacement yet.
- The pipeline never edits your model. It chooses which plate a part goes on and
  where it sits, and it turns a part only where it would not otherwise fit — and
  says so when it does. A part that cannot print as designed is reported, not
  altered.

## 0.1.0

First release. Two MCP servers — one driving OpenSCAD, one driving the
Orca-family slicers — plus `cadloop-verify` for rolling-interference and layout
checks on the spirograph model.
