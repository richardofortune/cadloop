# Acceptance criteria

What "seamless idea to print-ready" has to mean in practice, so that decisions
during implementation can be judged against something instead of escalated.

A change that moves one of these is worth discussing. A change that moves none
of them is an implementation detail, and gets decided without asking.

## The end goal

Someone has an idea, writes or modifies OpenSCAD with an LLM, and gets files
their printer can run. They never learn what a profile is.

## Criteria

**A1 — Zero-argument setup.** On a machine with a slicer installed and
configured, `setup_printer()` succeeds with no arguments, and names the printer,
quality and filament it settled on. Measured: run it on a clean config, read the
five lines back.

**A2 — No profile paths in the happy path.** From setup to G-code, no tool call
requires a profile path, a slicer binary path, or a vendor spelling. Measured:
grep the happy-path transcript for `.json` and `/Applications`. Zero hits.

**A3 — Wrong beats silent.** A setup that does not match reality is reported,
never used. A stale slicer version, an edited profile, a missing binary, and a
printer the user does not own all surface before any G-code is written.
Measured: each of those four states produces a refusal naming the cause.

**A4 — Three calls, not forty.** A `.scad` file reaches print-ready plates in at
most three tool calls. Tonight's baseline was roughly forty. Measured: count the
calls for `spirograph.scad`.

**A5 — The output is provably on the bed.** Every G-code file the pipeline
produces has every extruding move inside the printable area, checked by the
pipeline and not by a human. Measured: the report states it; corrupting a plate
makes the report say so.

**A6 — The model is never edited.** No pipeline action changes the `.scad` or
the geometry it produces. Rotation happens only where a part would not otherwise
fit. Measured: hash the source before and after; rotations appear in the report
with a reason.

**A7 — A second printer works without code changes.** Setting up a different
printer from a different vendor requires no edit to cadloop. Measured: run
`setup_printer` against a second machine profile and slice successfully.

**A8 — The report is readable in one screen.** The pipeline returns what it did,
what it changed, and what needs a human, in under thirty lines. An LLM reading
it should not need to fetch anything else to decide the next step. Measured:
line count, and whether the next action is inferable from the report alone.

## What follows from these

- **A2 and A4 are why the machine record exists.** Landing 1 delivers A1, A2, A3
  and A7. Landing 2 delivers A4, A5 and A8. A6 is a constraint on both.
- **Nothing here mentions profile formats, flag spellings, or signatures.**
  Those are means. When one is in question, pick the option that best serves the
  criteria and record the choice; do not raise it.
- **Backwards compatibility is not a criterion.** It is a courtesy to the
  published 0.1.0. Where it conflicts with a criterion, the criterion wins.

## Out of scope for these criteria

Print quality. Whether the part warps, adheres, or is strong enough is project B
and beyond. These criteria are about getting a correct, targeted, verified job
to the machine, not about whether the design is any good.
