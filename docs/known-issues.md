# Known issues in the tools cadloop drives

Vendor quirks cadloop works around at runtime. Each is discovered by probing
rather than asserted in code, so a fix upstream is picked up automatically.

## Creality Print's CLI does not run headless

**Affects:** Creality Print 7.1.1.4472 on macOS 26 Apple Silicon, and 7.2.0 on
Windows 11. Not a macOS problem, and not fixed by 7.2.0.
**Upstream:** https://github.com/CrealityOfficial/CrealityPrint/issues/574

Every CLI operation except `--help` segfaults, including `--info` on a plain
STL with no profiles loaded. It dies in `Slic3r::GUI::PartPlate::set_shape`
called from `Slic3r::CLI::run`, a null dereference in GUI bed setup that the
headless path never initialises.

The upstream issue is titled as a 3MF input problem and was first reported
against macOS. It is neither: the crash does not depend on the input format,
and a second reporter reproduced it on Windows against a later build. Read the
title narrowly and you would conclude a different platform or a newer version
avoids it, which is what makes this worth writing down.

cadloop probes each installed slicer with `--help` and proves it with a test
slice, so Creality Print simply fails to earn the job while another slicer is
present. Its *profiles* are still used, and are preferred: they set
`use_relative_e_distances` where OrcaSlicer's equivalent does not, which is
what makes the Ender-3 V3 SE slice without an override.

## `printable_area` has two spellings

**Affects:** stock profiles from Creality Print and OrcaSlicer

Written both as a list of `"XxY"` strings and as one comma-separated string.
Iterating the string form yields characters, so a naive reader finds no points
and reports no bed. Handled in `profiles.area_points`.

## Most machines inherit their bed rather than declaring it

**Affects:** 144 of the concrete machine profiles shipped with Creality Print
7.1.1, including every Bambu Lab model

`printable_area` is absent and reached through `inherits`. Handled in
`profiles.profile_chain`.

## Setting overrides must be hyphenated

**Affects:** the Orca-family CLI

Settings are spelled with underscores in the profile JSON and with hyphens on
the command line. `--layer_height` is rejected outright; `--layer-height` is
accepted. Not every setting is exposed as a flag, and one that is not fails the
whole run with `Invalid option`. Note the CLI uses the current name rather than
the PrusaSlicer one: `layer_change_gcode`, not `layer_gcode`.
