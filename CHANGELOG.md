# Changelog

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
