# Machine Record Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give cadloop a notion of which printer a job is for, resolved once from what the user's slicer is already configured with, proven by slicing a test cube, and read by every tool thereafter instead of profile paths.

**Architecture:** Three new pure-ish modules under `src/cadloop/` — `profiles.py` (vendor JSON in, machine facts out), `slicers.py` (what is installed, which one actually works, what it is configured with), `machine.py` (the stored record, its fingerprint, staleness). `slicer_server.py` keeps only MCP tools and slicer invocation, and its tools take profile arguments as optional overrides rather than requirements.

**Tech Stack:** Python 3.10+, `mcp` (FastMCP), `pytest` for unit tests (new dev dependency), existing standalone `tests/smoke.py` for end-to-end MCP coverage.

## Global Constraints

- Python floor is `>=3.10`. No `match`, no `tomllib`, no `Self`. `X | None` annotations are fine.
- Dependencies stay `mcp>=1.0` at runtime; `shapely>=2.0` under the `verify` extra; `pytest>=8` under a new `dev` extra. Add nothing else.
- Every tool return value must be JSON-serialisable — no `Path` objects, no dataclasses, no sets.
- Unknown is a third answer. A check that cannot reach a conclusion returns `ok: None` with a `reason`, never `False`.
- No hardcoded "prefer OrcaSlicer" or "Creality Print is broken". Those are discovered by probing and recorded, never asserted in code.
- The pipeline may rearrange parts but never edits geometry. Rotation only where a part would not otherwise fit.
- Existing published 0.1.0 signatures keep working: passing a profile explicitly stays legal.
- Spec: `docs/superpowers/specs/2026-07-28-machine-aware-pipeline-design.md`
- Acceptance criteria: `docs/superpowers/specs/2026-07-28-acceptance-criteria.md`.
  This landing delivers A1, A2, A3 and A7. A choice that moves none of A1 to A8
  is an implementation detail: pick the better option and record it.

---

### Task 1: Extract profile resolution into its own module

`slicer_server.py` is 539 lines and holds the most-tested logic in the project buried among MCP tools. `gearcheck` and the future packer both need it. This task is a move plus tests, with no behaviour change.

**Files:**
- Create: `src/cadloop/profiles.py`
- Create: `tests/test_profiles.py`
- Create: `tests/profiles/flat.json`, `tests/profiles/stringbed.json`, `tests/profiles/base.json`, `tests/profiles/child.json`, `tests/profiles/nobed.json`
- Modify: `src/cadloop/slicer_server.py` (delete the moved functions, import them instead)
- Modify: `pyproject.toml` (add the `dev` extra)
- Modify: `.github/workflows/ci.yml` (run pytest)

**Interfaces:**
- Consumes: nothing.
- Produces: `profile_roots() -> list[Path]`, `profile_index() -> dict[str, Path]`, `profile_chain(path: Path, limit: int = 12) -> list[dict]`, `inherited(chain: list[dict], key: str) -> Any`, `area_points(raw: Any) -> list[tuple[float, float]]`, `bed_of(machine_profile: str | Path) -> dict`, `classify(path: Path) -> dict | None`, and the module constant `PROFILE_CANDIDATES: list[str]`.

- [ ] **Step 1: Add the dev extra and a pytest config**

In `pyproject.toml`, under `[project.optional-dependencies]`, add the `dev` line so the block reads:

```toml
[project.optional-dependencies]
verify = ["shapely>=2.0"]
dev = ["pytest>=8"]
```

Then append to the end of the file:

```toml
[tool.pytest.ini_options]
testpaths = ["tests"]
# smoke.py is a standalone script driving real MCP sessions, not a pytest module
addopts = "--ignore=tests/smoke.py --ignore=tests/mock"
```

- [ ] **Step 2: Create the test fixture profiles**

These four files stand in for the shapes real vendor profiles come in. Create `tests/profiles/flat.json`:

```json
{
  "type": "machine",
  "name": "Flat Bed",
  "printable_area": ["0x0", "220x0", "220x220", "0x220"],
  "printable_height": "250"
}
```

`tests/profiles/stringbed.json`:

```json
{
  "type": "machine",
  "name": "String Bed",
  "printable_area": "0x0,180x0,180x180,0x180",
  "printable_height": "200"
}
```

`tests/profiles/base.json`:

```json
{
  "type": "machine",
  "name": "Bed Base",
  "printable_area": ["0x0", "200x0", "200x200", "0x200"],
  "printable_height": "180"
}
```

`tests/profiles/child.json`:

```json
{
  "type": "machine",
  "name": "Bed Child",
  "inherits": "Bed Base"
}
```

`tests/profiles/nobed.json`:

```json
{
  "type": "machine",
  "name": "No Bed Anywhere",
  "inherits": "Nothing That Exists"
}
```

- [ ] **Step 3: Write the failing tests**

Create `tests/test_profiles.py`:

```python
from pathlib import Path

import pytest

from cadloop import profiles

FIXTURES = Path(__file__).parent / "profiles"


@pytest.fixture(autouse=True)
def only_fixture_profiles(monkeypatch):
    """Point profile discovery at the fixtures, not at whatever is installed."""
    monkeypatch.setenv("SLICER_PROFILE_DIRS", str(FIXTURES))
    monkeypatch.setattr(profiles, "PROFILE_CANDIDATES", [])
    profiles.reset_cache()
    yield
    profiles.reset_cache()


def test_area_points_reads_a_list_of_pairs():
    pts = profiles.area_points(["0x0", "220x0", "220x220", "0x220"])
    assert pts == [(0.0, 0.0), (220.0, 0.0), (220.0, 220.0), (0.0, 220.0)]


def test_area_points_reads_one_comma_separated_string():
    pts = profiles.area_points("0x0,180x0,180x180,0x180")
    assert pts == [(0.0, 0.0), (180.0, 0.0), (180.0, 180.0), (0.0, 180.0)]


def test_area_points_rejects_nonsense():
    assert profiles.area_points(None) == []
    assert profiles.area_points(42) == []


def test_bed_of_list_form():
    bed = profiles.bed_of(FIXTURES / "flat.json")
    assert bed == {"x_mm": 220.0, "y_mm": 220.0, "z_mm": 250.0, "origin": [0.0, 0.0]}


def test_bed_of_string_form():
    bed = profiles.bed_of(FIXTURES / "stringbed.json")
    assert bed["x_mm"] == 180.0 and bed["z_mm"] == 200.0


def test_bed_of_follows_inherits():
    bed = profiles.bed_of(FIXTURES / "child.json")
    assert bed["x_mm"] == 200.0 and bed["z_mm"] == 180.0


def test_bed_of_returns_empty_when_no_bed_anywhere_in_the_chain():
    assert profiles.bed_of(FIXTURES / "nobed.json") == {}


def test_classify_recognises_a_machine():
    rec = profiles.classify(FIXTURES / "flat.json")
    assert rec["kind"] == "machine" and rec["name"] == "Flat Bed"


def test_classify_ignores_json_that_is_not_a_profile(tmp_path):
    other = tmp_path / "notaprofile.json"
    other.write_text('{"hello": "world"}')
    assert profiles.classify(other) is None
```

- [ ] **Step 4: Run the tests to verify they fail**

Run: `.venv/bin/pip install -e ".[verify,dev]" && .venv/bin/pytest tests/test_profiles.py -q`
Expected: FAIL, `ModuleNotFoundError: No module named 'cadloop.profiles'`

- [ ] **Step 5: Create the module by moving the functions**

This is a verbatim move: copy each body across unchanged, drop the leading
underscore from the name, and delete the original. Do not rewrite the logic —
`tests/smoke.py` is the guard that behaviour did not shift, and it only works
as a guard if the code is the same code.

Exact sources in `src/cadloop/slicer_server.py` as of this plan:

| Move | From lines | Becomes |
| --- | --- | --- |
| `_PROFILE_CANDIDATES` | 69–93 | `PROFILE_CANDIDATES` |
| `_profile_roots` | 127–145 | `profile_roots` |
| `_profile_index` | 147–165 | `profile_index` |
| `_profile_chain` | 167–189 | `profile_chain` |
| `_inherited` | 191–198 | `inherited` |
| `_area_points` | 200–215 | `area_points` |
| `_classify` | 217–244 | `classify` |
| `_bed` | 361–375 | `bed_of` |

Verify the line numbers before cutting — earlier tasks in another branch may
have shifted them. `grep -n "^def _profile_roots" src/cadloop/slicer_server.py`
is the check.

Two changes are not verbatim, and are the only two:

- `_profile_index` memoises into a module-level `_PROFILE_INDEX`. Rename that
  global to `_INDEX` and add `reset_cache()`, so a test can clear it between
  cases and `setup_printer` can clear it after a slicer is installed.
- `_profile_roots` and `_profile_index` referred to `_PROFILE_CANDIDATES`;
  they now refer to `PROFILE_CANDIDATES`.

```python
"""Vendor profile resolution: JSON on disk in, machine facts out.

Kept apart from the MCP server because the packer, the verifier and the
machine record all need it, and because it is the part most exposed to
vendor idiosyncrasy: printable_area is written two different ways, and most
machines inherit their bed rather than declaring it.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

PROFILE_CANDIDATES = [
    # (paste the existing _PROFILE_CANDIDATES list here unchanged)
]

_INDEX: dict[str, Path] | None = None


def reset_cache() -> None:
    """Drop the memoised name index. Tests and re-setup need this."""
    global _INDEX
    _INDEX = None


def profile_roots() -> list[Path]:
    # verbatim from slicer_server.py:127-145, with _PROFILE_CANDIDATES
    # renamed to PROFILE_CANDIDATES
    ...


def profile_index() -> dict[str, Path]:
    # verbatim from slicer_server.py:147-165, with the global renamed
    # from _PROFILE_INDEX to _INDEX
    ...


def profile_chain(path: Path, limit: int = 12) -> list[dict[str, Any]]:
    # verbatim from slicer_server.py:167-189
    ...


def inherited(chain: list[dict[str, Any]], key: str) -> Any:
    # verbatim from slicer_server.py:191-198
    ...


def area_points(raw: Any) -> list[tuple[float, float]]:
    # verbatim from slicer_server.py:200-215
    ...


def classify(path: Path) -> dict[str, Any] | None:
    # verbatim from slicer_server.py:217-244
    ...


def bed_of(machine_profile: str | Path) -> dict[str, Any]:
    # verbatim from slicer_server.py:361-375
    ...
```

Replace the deleted definitions in `slicer_server.py` with an import, and update every call site (`_bed(` becomes `bed_of(`, `_profile_roots(` becomes `profile_roots(`, and so on):

```python
from .profiles import (area_points, bed_of, classify, inherited,
                       profile_chain, profile_index, profile_roots)
```

- [ ] **Step 6: Run both test suites**

Run: `.venv/bin/pytest tests/test_profiles.py -q && .venv/bin/python tests/smoke.py`
Expected: pytest PASSES; smoke prints `all checks passed`. The smoke test is the guard that the move changed no behaviour.

- [ ] **Step 7: Run pytest in CI**

In `.github/workflows/ci.yml`, change the install step and add a unit-test step before the existing rolling-interference step:

```yaml
      - name: Install
        run: pip install -e ".[verify,dev]"

      - name: Unit tests
        run: pytest -q
```

- [ ] **Step 8: Commit**

```bash
git add pyproject.toml src/cadloop/profiles.py src/cadloop/slicer_server.py tests/test_profiles.py tests/profiles .github/workflows/ci.yml
git commit -m "Extract profile resolution into its own module

The bed lookup and inheritance walk were buried in the MCP server, where
the packer and the verifier cannot reach them. Moved verbatim with unit
tests over both printable_area spellings, an inherited bed, and a chain
that defines no bed anywhere."
```

---

### Task 2: Derive the machine facts

The five facts everything downstream needs, read once from the resolved triple so nothing else parses vendor JSON.

**Files:**
- Modify: `src/cadloop/profiles.py`
- Modify: `tests/test_profiles.py`
- Create: `tests/profiles/proc.json`, `tests/profiles/fila.json`

**Interfaces:**
- Consumes: `bed_of`, `profile_chain`, `inherited` from Task 1.
- Produces: `machine_facts(machine: str | Path, process: str | Path, filament: str | Path) -> dict` returning keys `printer_model: str | None`, `nozzle_mm: float | None`, `gcode_flavor: str | None`, `layer_height_mm: float | None`, `filament_type: str | None`, `bed: dict`.

- [ ] **Step 1: Add the process and filament fixtures**

`tests/profiles/proc.json`:

```json
{
  "type": "process",
  "name": "0.20mm Standard Fixture",
  "layer_height": "0.2"
}
```

`tests/profiles/fila.json`:

```json
{
  "type": "filament",
  "name": "Fixture PLA",
  "filament_type": ["PLA"]
}
```

Add `"printer_model": "Fixture Printer"`, `"gcode_flavor": "marlin2"` and `"nozzle_diameter": ["0.4"]` to `tests/profiles/flat.json`, keeping its existing keys.

- [ ] **Step 2: Write the failing tests**

Append to `tests/test_profiles.py`:

```python
def test_machine_facts_reads_all_five():
    facts = profiles.machine_facts(FIXTURES / "flat.json",
                                   FIXTURES / "proc.json",
                                   FIXTURES / "fila.json")
    assert facts["printer_model"] == "Fixture Printer"
    assert facts["nozzle_mm"] == 0.4
    assert facts["gcode_flavor"] == "marlin2"
    assert facts["layer_height_mm"] == 0.2
    assert facts["filament_type"] == "PLA"
    assert facts["bed"]["x_mm"] == 220.0


def test_machine_facts_leaves_unknowns_as_none():
    facts = profiles.machine_facts(FIXTURES / "child.json",
                                   FIXTURES / "proc.json",
                                   FIXTURES / "fila.json")
    assert facts["nozzle_mm"] is None
    assert facts["bed"]["x_mm"] == 200.0


def test_machine_facts_is_json_serialisable():
    import json
    facts = profiles.machine_facts(FIXTURES / "flat.json",
                                   FIXTURES / "proc.json",
                                   FIXTURES / "fila.json")
    json.loads(json.dumps(facts))
```

- [ ] **Step 3: Run to verify they fail**

Run: `.venv/bin/pytest tests/test_profiles.py -q -k machine_facts`
Expected: FAIL, `AttributeError: module 'cadloop.profiles' has no attribute 'machine_facts'`

- [ ] **Step 4: Implement**

Add to `src/cadloop/profiles.py`:

```python
def _first_scalar(value: Any) -> Any:
    """Vendor profiles write single values as one-element lists as often as
    not: nozzle_diameter is ["0.4"], filament_type is ["PLA"]."""
    if isinstance(value, (list, tuple)):
        return value[0] if value else None
    return value


def _as_float(value: Any) -> float | None:
    try:
        return float(_first_scalar(value))
    except (TypeError, ValueError):
        return None


def machine_facts(machine: str | Path, process: str | Path,
                  filament: str | Path) -> dict[str, Any]:
    """The handful of facts the rest of the system needs, read once.

    Everything downstream asks for these rather than parsing vendor JSON,
    so the two spellings of printable_area and the inheritance walk are
    handled in one place instead of being rediscovered per caller."""
    m = profile_chain(Path(machine).expanduser())
    p = profile_chain(Path(process).expanduser())
    f = profile_chain(Path(filament).expanduser())
    return {
        "printer_model": _first_scalar(inherited(m, "printer_model")),
        "nozzle_mm": _as_float(inherited(m, "nozzle_diameter")),
        "gcode_flavor": _first_scalar(inherited(m, "gcode_flavor")),
        "layer_height_mm": _as_float(inherited(p, "layer_height")),
        "filament_type": _first_scalar(inherited(f, "filament_type")),
        "bed": bed_of(machine),
    }
```

- [ ] **Step 5: Run to verify they pass**

Run: `.venv/bin/pytest tests/test_profiles.py -q`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/cadloop/profiles.py tests/test_profiles.py tests/profiles
git commit -m "Derive machine facts from a resolved profile triple

Bed, nozzle, flavour, layer height and material, read once so nothing
downstream parses vendor JSON. Unknown stays None rather than guessing."
```

---

### Task 3: Discover installed slicers and probe which one works

No hardcoded preference. Try what is present, prove which can actually run.

**Files:**
- Create: `src/cadloop/slicers.py`
- Create: `tests/test_slicers.py`

**Interfaces:**
- Consumes: `run` and `find_binary` from `cadloop.common`.
- Produces: `SLICER_CANDIDATES: list[str]`, `family_of(binary: str) -> str`, `installed() -> list[dict]` where each dict is `{"family": str, "binary": str}`, and `probe(binary: str, timeout_s: int = 60) -> dict` returning `{"ok": bool, "version": str | None, "flags": list[str], "reason": str}`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_slicers.py`:

```python
import os
import stat
from pathlib import Path

from cadloop import slicers


def _fake_binary(tmp_path: Path, name: str, body: str) -> str:
    p = tmp_path / name
    p.write_text("#!/bin/sh\n" + body)
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return str(p)


def test_family_of_recognises_the_orca_family():
    assert slicers.family_of("/x/OrcaSlicer") == "orca"
    assert slicers.family_of("/x/BambuStudio") == "bambu"
    assert slicers.family_of("/x/CrealityPrint") == "creality"
    assert slicers.family_of("/x/somethingelse") == "unknown"


def test_probe_accepts_a_binary_that_prints_flags(tmp_path):
    b = _fake_binary(tmp_path, "good", 'echo "OrcaSlicer 2.4.2"; echo " --slice"; echo " --export-3mf"')
    r = slicers.probe(b)
    assert r["ok"] is True
    assert "--slice" in r["flags"]
    assert "2.4.2" in (r["version"] or "")


def test_probe_rejects_a_binary_that_dies(tmp_path):
    b = _fake_binary(tmp_path, "bad", "kill -SEGV $$")
    r = slicers.probe(b)
    assert r["ok"] is False
    assert r["reason"]


def test_probe_rejects_a_binary_missing_the_flags_we_need(tmp_path):
    b = _fake_binary(tmp_path, "thin", 'echo "Some Slicer 1.0"; echo " --help"')
    r = slicers.probe(b)
    assert r["ok"] is False
    assert "--slice" in r["reason"]


def test_installed_finds_a_candidate_that_exists(tmp_path, monkeypatch):
    b = _fake_binary(tmp_path, "OrcaSlicer", 'echo " --slice"')
    monkeypatch.setattr(slicers, "SLICER_CANDIDATES", [b])
    found = slicers.installed()
    assert [f["family"] for f in found] == ["orca"]
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/pytest tests/test_slicers.py -q`
Expected: FAIL, `ModuleNotFoundError: No module named 'cadloop.slicers'`

- [ ] **Step 3: Implement**

Create `src/cadloop/slicers.py`:

```python
"""Which slicers are installed, and which of them actually runs.

Deliberately no preference order baked in. A binary earns its place by
answering --help with the flags we need. Creality Print's CLI does not run
headless on macOS today and OrcaSlicer does, but that is something this
probes and records rather than something the code asserts, so it corrects
itself when upstream ships a fix.
"""

from __future__ import annotations

import re
from pathlib import Path

from .common import run as _run

# Every install location we know of, in no significant order.
SLICER_CANDIDATES = [
    "/Applications/OrcaSlicer.app/Contents/MacOS/OrcaSlicer",
    "/Applications/BambuStudio.app/Contents/MacOS/BambuStudio",
    "/Applications/ElegooSlicer.app/Contents/MacOS/ElegooSlicer",
    "/Applications/Creality Print.app/Contents/MacOS/CrealityPrint",
    "/Applications/CrealityPrint.app/Contents/MacOS/CrealityPrint",
    "/usr/bin/orca-slicer",
    "/usr/bin/OrcaSlicer",
    "/usr/bin/CrealityPrint",
    r"C:\Program Files\OrcaSlicer\orca-slicer.exe",
    r"C:\Program Files\Bambu Studio\bambu-studio.exe",
    r"C:\Program Files\Creality\Creality Print 7.0\CrealityPrint.exe",
]

# Without these there is no point going further.
REQUIRED_FLAGS = ["--slice", "--load-settings", "--load-filaments", "--export-3mf"]

_FAMILIES = [("orcaslicer", "orca"), ("orca-slicer", "orca"),
             ("bambustudio", "bambu"), ("bambu-studio", "bambu"),
             ("elegooslicer", "elegoo"),
             ("crealityprint", "creality")]


def family_of(binary: str) -> str:
    name = Path(binary).name.lower()
    for needle, family in _FAMILIES:
        if needle in name:
            return family
    return "unknown"


def installed() -> list[dict]:
    """Every candidate that exists on disk. Says nothing about whether it works."""
    out, seen = [], set()
    for c in SLICER_CANDIDATES:
        p = Path(c).expanduser()
        if p.exists() and str(p) not in seen:
            seen.add(str(p))
            out.append({"family": family_of(str(p)), "binary": str(p)})
    return out


def probe(binary: str, timeout_s: int = 60) -> dict:
    """Ask a binary for its flags. This is what earns it the job."""
    r = _run([binary, "--help"], timeout_s)
    log = r["log"] or ""
    if r["timed_out"]:
        return {"ok": False, "version": None, "flags": [],
                "reason": f"did not answer --help within {timeout_s}s"}
    if r["returncode"] not in (0, 1):
        return {"ok": False, "version": None, "flags": [],
                "reason": f"exited {r['returncode']} on --help"}
    flags = sorted(set(re.findall(r"--[a-z][a-z0-9-]+", log)))
    missing = [f for f in REQUIRED_FLAGS if f not in flags]
    if missing:
        return {"ok": False, "version": None, "flags": flags,
                "reason": f"does not offer {', '.join(missing)}"}
    m = re.search(r"(\d+\.\d+\.\d+(?:\.\d+)?)", log)
    return {"ok": True, "version": m.group(1) if m else None,
            "flags": flags, "reason": ""}
```

- [ ] **Step 4: Run to verify they pass**

Run: `.venv/bin/pytest tests/test_slicers.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/cadloop/slicers.py tests/test_slicers.py
git commit -m "Discover installed slicers and probe which one runs

No preference order in code. A binary earns the job by answering --help
with the flags we need, so a vendor fixing their CLI is picked up rather
than being permanently demoted by a constant."
```

---

### Task 4: Read what the user's slicer is already configured with

Creality Print keeps the selected printer, quality and filament in `Creality.conf`. Asking a user to name a printer their slicer already knows is a worse product. Trusting it blindly is also wrong, since it goes stale, which is why Task 6 proves it and reports it.

**Files:**
- Modify: `src/cadloop/slicers.py`
- Modify: `tests/test_slicers.py`

**Interfaces:**
- Consumes: `family_of` from Task 3.
- Produces: `CONFIG_CANDIDATES: list[tuple[str, str]]` of `(family, glob)`, and `adopt() -> list[dict]` where each dict is `{"family": str, "printer": str, "process": str | None, "filament": str | None, "source": str, "mtime": float}`, newest first.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_slicers.py`:

```python
import json


def test_adopt_reads_a_creality_style_config(tmp_path, monkeypatch):
    conf = tmp_path / "Creality.conf"
    conf.write_text(json.dumps({
        "presets": {
            "machine": "Creality Ender-3 V3 SE 0.4 nozzle",
            "process": "0.20mm Standard @Creality Ender-3 V3 SE 0.4 nozzle",
            "filaments": ["Hyper PLA @Creality Ender-3 V3 SE 0.4 nozzle"],
        }
    }))
    monkeypatch.setattr(slicers, "CONFIG_CANDIDATES", [("creality", str(conf))])
    got = slicers.adopt()
    assert len(got) == 1
    assert got[0]["printer"] == "Creality Ender-3 V3 SE 0.4 nozzle"
    assert got[0]["process"].startswith("0.20mm Standard")
    assert got[0]["filament"].startswith("Hyper PLA")


def test_adopt_returns_newest_first(tmp_path, monkeypatch):
    import os, time
    a = tmp_path / "a.conf"
    b = tmp_path / "b.conf"
    for p, name in ((a, "Old Printer"), (b, "New Printer")):
        p.write_text(json.dumps({"presets": {"machine": name}}))
    os.utime(a, (1000, 1000))
    os.utime(b, (2000, 2000))
    monkeypatch.setattr(slicers, "CONFIG_CANDIDATES",
                        [("creality", str(a)), ("orca", str(b))])
    got = slicers.adopt()
    assert [g["printer"] for g in got] == ["New Printer", "Old Printer"]


def test_adopt_skips_unreadable_config(tmp_path, monkeypatch):
    bad = tmp_path / "bad.conf"
    bad.write_text("this is not json")
    monkeypatch.setattr(slicers, "CONFIG_CANDIDATES", [("creality", str(bad))])
    assert slicers.adopt() == []


def test_adopt_returns_empty_when_nothing_is_configured(monkeypatch):
    monkeypatch.setattr(slicers, "CONFIG_CANDIDATES", [])
    assert slicers.adopt() == []
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/pytest tests/test_slicers.py -q -k adopt`
Expected: FAIL, `AttributeError: module 'cadloop.slicers' has no attribute 'adopt'`

- [ ] **Step 3: Implement**

Add to `src/cadloop/slicers.py`:

```python
import glob as _glob
import json
import os

# Where each slicer records what the user last selected. Globbed, because
# Creality versions its config directory (6.0, 7.0, ...).
CONFIG_CANDIDATES = [
    ("creality", os.path.expanduser(
        "~/Library/Application Support/Creality/Creality Print/*/Creality.conf")),
    ("creality", os.path.expanduser(
        "~/AppData/Roaming/Creality/Creality Print/*/Creality.conf")),
    ("orca", os.path.expanduser(
        "~/Library/Application Support/OrcaSlicer/OrcaSlicer.conf")),
    ("orca", os.path.expanduser("~/.config/OrcaSlicer/OrcaSlicer.conf")),
    ("bambu", os.path.expanduser(
        "~/Library/Application Support/BambuStudio/BambuStudio.conf")),
]


def _selected(conf: dict) -> dict | None:
    """Pull the selected presets out of a slicer config.

    The Orca family stores them under "presets", with filaments as a list
    because of multi-material machines. We take the first."""
    p = conf.get("presets")
    if not isinstance(p, dict):
        return None
    printer = p.get("machine") or p.get("printer")
    if not isinstance(printer, str) or not printer:
        return None
    fil = p.get("filaments") or p.get("filament")
    if isinstance(fil, list):
        fil = fil[0] if fil else None
    if isinstance(fil, dict):
        fil = fil.get("filament")
    process = p.get("process") or p.get("print")
    return {"printer": printer,
            "process": process if isinstance(process, str) else None,
            "filament": fil if isinstance(fil, str) else None}


def adopt() -> list[dict]:
    """What the user already chose in their slicer's own interface.

    Newest configuration first. This is a starting point, not the truth:
    a stored filament can be two changes out of date, so the caller proves
    it and reports what it settled on."""
    out = []
    for family, pattern in CONFIG_CANDIDATES:
        for path in _glob.glob(pattern):
            try:
                sel = _selected(json.loads(Path(path).read_text(errors="replace")))
            except Exception:
                continue
            if sel:
                sel.update({"family": family, "source": path,
                            "mtime": Path(path).stat().st_mtime})
                out.append(sel)
    out.sort(key=lambda r: r["mtime"], reverse=True)
    return out
```

- [ ] **Step 4: Run to verify they pass**

Run: `.venv/bin/pytest tests/test_slicers.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/cadloop/slicers.py tests/test_slicers.py
git commit -m "Read what the user's slicer is already configured with

Creality Print records the selected printer, quality and filament. Asking
someone to name a printer their slicer already knows is a worse product.
Newest config first; treated as a starting point, since a stored filament
can be well out of date."
```

---

### Task 5: The stored machine record

**Files:**
- Create: `src/cadloop/machine.py`
- Create: `tests/test_machine.py`

**Interfaces:**
- Consumes: `machine_facts` from Task 2.
- Produces: `record_path() -> Path`, `save(rec: dict) -> Path`, `load() -> dict | None`, `fingerprint(binary: str, version: str | None, profiles: dict) -> dict`, `staleness(rec: dict) -> str | None` returning a human reason or `None` when current, and `SCHEMA = 1`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_machine.py`:

```python
import json
from pathlib import Path

import pytest

from cadloop import machine


@pytest.fixture(autouse=True)
def isolated_record(tmp_path, monkeypatch):
    monkeypatch.setenv("CADLOOP_MACHINE", str(tmp_path / "machine.json"))
    yield


def _profiles(tmp_path: Path) -> dict:
    out = {}
    for kind in ("machine", "process", "filament"):
        p = tmp_path / f"{kind}.json"
        p.write_text(json.dumps({"type": kind, "name": kind}))
        out[kind] = str(p)
    return out


def test_load_returns_none_when_nothing_saved():
    assert machine.load() is None


def test_save_then_load_round_trips(tmp_path):
    rec = {"schema": machine.SCHEMA, "name": "Fixture",
           "profiles": _profiles(tmp_path)}
    machine.save(rec)
    assert machine.load()["name"] == "Fixture"


def test_fingerprint_changes_when_a_profile_changes(tmp_path):
    profs = _profiles(tmp_path)
    before = machine.fingerprint("/bin/true", "1.0", profs)
    Path(profs["process"]).write_text('{"type": "process", "name": "edited"}')
    after = machine.fingerprint("/bin/true", "1.0", profs)
    assert before != after


def test_staleness_is_none_for_a_fresh_record(tmp_path):
    profs = _profiles(tmp_path)
    rec = {"schema": machine.SCHEMA, "slicer": {"binary": "/bin/true", "version": "1.0"},
           "profiles": profs,
           "fingerprint": machine.fingerprint("/bin/true", "1.0", profs)}
    assert machine.staleness(rec) is None


def test_staleness_reports_an_edited_profile(tmp_path):
    profs = _profiles(tmp_path)
    rec = {"schema": machine.SCHEMA, "slicer": {"binary": "/bin/true", "version": "1.0"},
           "profiles": profs,
           "fingerprint": machine.fingerprint("/bin/true", "1.0", profs)}
    Path(profs["machine"]).write_text('{"type": "machine", "name": "edited"}')
    assert "machine" in machine.staleness(rec)


def test_staleness_reports_a_missing_profile(tmp_path):
    profs = _profiles(tmp_path)
    rec = {"schema": machine.SCHEMA, "slicer": {"binary": "/bin/true", "version": "1.0"},
           "profiles": profs,
           "fingerprint": machine.fingerprint("/bin/true", "1.0", profs)}
    Path(profs["filament"]).unlink()
    assert "filament" in machine.staleness(rec)


def test_staleness_reports_an_old_schema(tmp_path):
    assert "schema" in machine.staleness({"schema": 0})
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/pytest tests/test_machine.py -q`
Expected: FAIL, `ModuleNotFoundError: No module named 'cadloop.machine'`

- [ ] **Step 3: Implement**

Create `src/cadloop/machine.py`:

```python
"""The stored answer to "which printer is this for".

Written once by setup and read by every tool afterwards, so no caller has
to supply profile paths and no caller can supply three that disagree. The
record is a cache, not a source of truth: if it disagrees with what is on
disk, disk wins and setup runs again.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

SCHEMA = 1


def record_path() -> Path:
    """Config, not user data, so it lives outside the workspace sandbox and
    survives changing directory."""
    env = os.environ.get("CADLOOP_MACHINE")
    if env:
        return Path(env).expanduser()
    base = os.environ.get("XDG_CONFIG_HOME") or "~/.config"
    return Path(base).expanduser() / "cadloop" / "machine.json"


def save(rec: dict[str, Any]) -> Path:
    p = record_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(rec, indent=1, sort_keys=True))
    return p


def load() -> dict[str, Any] | None:
    p = record_path()
    if not p.is_file():
        return None
    try:
        rec = json.loads(p.read_text())
    except Exception:
        return None
    return rec if isinstance(rec, dict) else None


def _sha(path: str | Path) -> str | None:
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return None


def fingerprint(binary: str, version: str | None,
                profiles: dict[str, Any]) -> dict[str, Any]:
    """Enough to notice the ground moving: the slicer version, and the
    content of each profile it was validated against."""
    out: dict[str, Any] = {"binary": binary, "version": version, "sha256": {}}
    for kind, path in profiles.items():
        p = path[0] if isinstance(path, list) and path else path
        out["sha256"][kind] = _sha(p) if isinstance(p, str) else None
    return out


def staleness(rec: dict[str, Any]) -> str | None:
    """Why this record can no longer be trusted, or None if it still can."""
    if rec.get("schema") != SCHEMA:
        return f"record schema {rec.get('schema')!r}, expected {SCHEMA}"
    fp = rec.get("fingerprint") or {}
    binary = (rec.get("slicer") or {}).get("binary")
    if binary and not Path(binary).exists():
        return f"slicer is gone from {binary}"
    if binary and fp.get("binary") and fp["binary"] != binary:
        return "slicer binary changed"
    for kind, want in (fp.get("sha256") or {}).items():
        path = (rec.get("profiles") or {}).get(kind)
        p = path[0] if isinstance(path, list) and path else path
        if not isinstance(p, str) or not Path(p).exists():
            return f"{kind} profile is gone"
        if _sha(p) != want:
            return f"{kind} profile changed on disk"
    return None
```

- [ ] **Step 4: Run to verify they pass**

Run: `.venv/bin/pytest tests/test_machine.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/cadloop/machine.py tests/test_machine.py
git commit -m "Store the resolved machine, and notice when it goes stale

The record carries the slicer version and a hash of each profile it was
validated against, so a slicer update or an edited profile is caught
rather than silently changing what gets printed."
```

---

### Task 6: `setup_printer` — adopt, resolve, prove, store, report

The step that ends the wrong-printer class of failure. It slices a real cube, because the Ender-3 V3 SE validation failure appeared only at slice time and no static check would have caught it.

**Files:**
- Modify: `src/cadloop/machine.py`
- Modify: `src/cadloop/slicer_server.py`
- Modify: `tests/test_machine.py`

**Interfaces:**
- Consumes: `slicers.installed`, `slicers.probe`, `slicers.adopt`, `profiles.profile_index`, `profiles.classify`, `profiles.machine_facts`, `machine.save`, `machine.fingerprint`.
- Produces: `machine.CUBE_STL: str`, `machine.resolve(printer: str | None, filament: str | None) -> dict`, and the MCP tools `setup_printer(printer: str | None = None, filament: str | None = None) -> dict` and `machine_info() -> dict`.

- [ ] **Step 1: Write the failing tests for candidate resolution**

Append to `tests/test_machine.py`:

```python
def test_resolve_refuses_an_ambiguous_printer_name(monkeypatch, tmp_path):
    from cadloop import profiles
    idx = {}
    for name in ("Creality K1", "Creality K1C", "Creality K1 SE"):
        p = tmp_path / f"{name}.json"
        p.write_text(json.dumps({"type": "machine", "name": name}))
        idx[name] = p
    monkeypatch.setattr(profiles, "profile_index", lambda: idx)
    out = machine.resolve(printer="K1", filament=None)
    assert out["ok"] is False
    assert len(out["candidates"]) == 3
    assert "K1C" in " ".join(out["candidates"])


def test_resolve_accepts_an_exact_printer_name(monkeypatch, tmp_path):
    from cadloop import profiles
    idx = {}
    # an exact match must not drag in the longer name that contains it
    for name, kind in (("Creality K1", "machine"), ("Creality K1C", "machine"),
                       ("0.20mm Standard @Creality K1", "process"),
                       ("Generic PLA @Creality K1", "filament")):
        f = tmp_path / f"{name}.json"
        f.write_text(json.dumps({"type": kind, "name": name}))
        idx[name] = f
    monkeypatch.setattr(profiles, "profile_index", lambda: idx)
    out = machine.resolve(printer="Creality K1", filament=None)
    assert out["ok"] is True, out["reason"]
    assert out["profiles"]["machine"] == str(idx["Creality K1"])
    assert "Standard" in out["profiles"]["process"]


def test_resolve_reports_a_printer_with_no_process_profile(monkeypatch, tmp_path):
    from cadloop import profiles
    f = tmp_path / "Lonely Printer.json"
    f.write_text(json.dumps({"type": "machine", "name": "Lonely Printer"}))
    monkeypatch.setattr(profiles, "profile_index", lambda: {"Lonely Printer": f})
    out = machine.resolve(printer="Lonely Printer", filament=None)
    assert out["ok"] is False
    assert "process" in out["reason"]


def test_resolve_reports_when_nothing_matches(monkeypatch):
    from cadloop import profiles
    monkeypatch.setattr(profiles, "profile_index", lambda: {})
    out = machine.resolve(printer="No Such Printer", filament=None)
    assert out["ok"] is False
    assert "no printer" in out["reason"].lower()
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/pytest tests/test_machine.py -q -k resolve`
Expected: FAIL, `AttributeError: module 'cadloop.machine' has no attribute 'resolve'`

- [ ] **Step 3: Implement resolution**

Add to `src/cadloop/machine.py`:

```python
from . import profiles as _profiles

# A 20mm cube, so setup can prove the whole chain works rather than
# assuming it. Written out at validation time and thrown away after.
CUBE_STL = """solid cube
facet normal 0 0 -1
  outer loop
    vertex 0 0 0
    vertex 20 20 0
    vertex 20 0 0
  endloop
endfacet
facet normal 0 0 -1
  outer loop
    vertex 0 0 0
    vertex 0 20 0
    vertex 20 20 0
  endloop
endfacet
facet normal 0 0 1
  outer loop
    vertex 0 0 20
    vertex 20 0 20
    vertex 20 20 20
  endloop
endfacet
facet normal 0 0 1
  outer loop
    vertex 0 0 20
    vertex 20 20 20
    vertex 0 20 20
  endloop
endfacet
facet normal 0 -1 0
  outer loop
    vertex 0 0 0
    vertex 20 0 0
    vertex 20 0 20
  endloop
endfacet
facet normal 0 -1 0
  outer loop
    vertex 0 0 0
    vertex 20 0 20
    vertex 0 0 20
  endloop
endfacet
facet normal 1 0 0
  outer loop
    vertex 20 0 0
    vertex 20 20 0
    vertex 20 20 20
  endloop
endfacet
facet normal 1 0 0
  outer loop
    vertex 20 0 0
    vertex 20 20 20
    vertex 20 0 20
  endloop
endfacet
facet normal 0 1 0
  outer loop
    vertex 20 20 0
    vertex 0 20 0
    vertex 0 20 20
  endloop
endfacet
facet normal 0 1 0
  outer loop
    vertex 20 20 0
    vertex 0 20 20
    vertex 20 20 20
  endloop
endfacet
facet normal -1 0 0
  outer loop
    vertex 0 20 0
    vertex 0 0 0
    vertex 0 0 20
  endloop
endfacet
facet normal -1 0 0
  outer loop
    vertex 0 20 0
    vertex 0 0 20
    vertex 0 20 20
  endloop
endfacet
endsolid cube
"""


def _match(kind: str, needle: str) -> list[tuple[str, Path]]:
    """Every profile of a kind whose name contains needle, case-insensitively.
    An exact name always wins outright, so "Creality K1" does not drag in
    "Creality K1C"."""
    hits = []
    for name, path in _profiles.profile_index().items():
        rec = _profiles.classify(path)
        if not rec or rec["kind"] != kind:
            continue
        if name.lower() == needle.lower():
            return [(name, path)]
        if needle.lower() in name.lower():
            hits.append((name, path))
    return sorted(hits)


def resolve(printer: str | None, filament: str | None) -> dict[str, Any]:
    """Turn a printer name into a machine, process and filament triple.

    Refuses ambiguity rather than guessing. "K1" matches five printers
    across four nozzle sizes, and picking the first is how the wrong
    machine gets chosen."""
    if not printer:
        return {"ok": False, "reason": "no printer given and none configured",
                "candidates": []}
    machines = _match("machine", printer)
    if not machines:
        return {"ok": False, "reason": f"no printer matches {printer!r}",
                "candidates": []}
    if len(machines) > 1:
        return {"ok": False,
                "reason": f"{printer!r} matches {len(machines)} printers",
                "candidates": [n for n, _ in machines]}
    name, mpath = machines[0]

    def pick(kind: str, want: str | None) -> str | None:
        if want:
            hits = _match(kind, want)
            if len(hits) == 1:
                return str(hits[0][1])
        # fall back to anything scoped to this printer
        hits = [h for h in _match(kind, name)]
        return str(hits[0][1]) if hits else None

    process = pick("process", None)
    fil = pick("filament", filament)
    missing = [k for k, v in (("process", process), ("filament", fil)) if not v]
    if missing:
        return {"ok": False,
                "reason": f"found {name} but no {' or '.join(missing)} profile for it",
                "candidates": []}
    return {"ok": True, "reason": "", "candidates": [], "printer": name,
            "profiles": {"machine": str(mpath), "process": process,
                         "filament": fil}}
```

- [ ] **Step 4: Run to verify they pass**

Run: `.venv/bin/pytest tests/test_machine.py -q`
Expected: PASS

- [ ] **Step 5: Add the `setup_printer` and `machine_info` tools**

In `src/cadloop/slicer_server.py`, add near the other tools:

```python
from . import machine as _machine
from . import slicers as _slicers
from .profiles import machine_facts


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
                  filament: str | None = None) -> dict[str, Any]:
    """Work out which printer this is for, once, and remember it.

    Reads what your slicer is already configured with, resolves it to a
    profile triple, proves the combination by slicing a 20mm cube, and
    stores the result. Every other tool then works without profile
    arguments. Pass printer and filament only to override what is
    configured, or when nothing is."""
    report: dict[str, Any] = {"adopted": None, "steps": []}

    want_printer, want_filament = printer, filament
    if not want_printer:
        for cand in _slicers.adopt():
            want_printer = cand["printer"]
            want_filament = want_filament or cand["filament"]
            report["adopted"] = {"from": cand["source"],
                                 "printer": cand["printer"],
                                 "filament": cand["filament"]}
            break

    res = _machine.resolve(want_printer, want_filament)
    if not res["ok"]:
        return {"ok": False, "reason": res["reason"],
                "candidates": res["candidates"], **report}

    working = []
    for cand in _slicers.installed():
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
            _machine.save(rec)
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
    rec = _machine.load()
    if rec is None:
        return {"ok": None, "reason": "no printer set up yet, run setup_printer"}
    why = _machine.staleness(rec)
    return {"ok": why is None, "reason": why or "",
            "name": rec.get("name"), "slicer": rec.get("slicer"),
            "derived": rec.get("derived")}
```

Add `import tempfile` to the imports at the top of `slicer_server.py` if not already present.

- [ ] **Step 6: Cover the test-slice failure path**

A slicer can answer `--help` correctly and still refuse to slice: that is
exactly what the Ender-3 V3 SE did, failing validation with *"Relative extruder
addressing requires resetting the extruder position at each layer"*. Append to
`tests/test_machine.py`:

```python
def test_prove_reports_a_slicer_that_answers_help_but_cannot_slice(tmp_path):
    import stat
    from cadloop import slicer_server

    fake = tmp_path / "fussy"
    fake.write_text("#!/bin/sh\n"
                    "echo 'Relative extruder addressing requires resetting' >&2\n"
                    "exit 205\n")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)

    profs = {}
    for kind in ("machine", "process", "filament"):
        f = tmp_path / f"{kind}.json"
        f.write_text(json.dumps({"type": kind, "name": kind}))
        profs[kind] = str(f)

    out = slicer_server._prove(str(fake), profs)
    assert out["ok"] is False
    assert "205" in out["reason"] or "Relative extruder" in out["reason"]
```

Run: `.venv/bin/pytest tests/test_machine.py -q -k prove`
Expected: PASS. If it fails because no `.gcode.3mf` was written, that is the
behaviour under test — `_prove` must report, not raise.

- [ ] **Step 7: Verify the tools are exposed and behave with nothing set up**

Run:

```bash
CADLOOP_MACHINE=/tmp/none.json .venv/bin/python -c "
import sys; sys.path.insert(0,'src')
from cadloop.slicer_server import machine_info
print(machine_info())"
```

Expected: `{'ok': None, 'reason': 'no printer set up yet, run setup_printer'}`

- [ ] **Step 8: Commit**

```bash
git add src/cadloop/machine.py src/cadloop/slicer_server.py tests/test_machine.py
git commit -m "Add setup_printer: adopt, resolve, prove, store, report

Reads what the slicer is already configured with, refuses an ambiguous
printer name rather than guessing, and proves the triple by slicing a 20mm
cube before storing it. The cube is the only check that catches a stock
printer failing slicer validation, which happens at slice time."
```

---

### Task 7: Existing tools default from the record, and say when it is stale

**Files:**
- Modify: `src/cadloop/slicer_server.py:377-411` (`check_bed_fit`), `:412-501` (`slice_model`)
- Modify: `tests/smoke.py`
- Create: `docs/known-issues.md`

**Interfaces:**
- Consumes: `machine.load`, `machine.staleness` from Task 5.
- Produces: `_active_profiles(machine_profile, process_profile, filament_profiles) -> tuple[dict, str | None]` returning the profiles to use and a staleness warning, raising `RuntimeError` when nothing is set up and nothing was passed.

- [ ] **Step 1: Add the resolution helper**

In `src/cadloop/slicer_server.py`:

```python
def _active_profiles(machine_profile: str | None = None,
                     process_profile: str | None = None,
                     filament_profiles: list[str] | None = None
                     ) -> tuple[dict[str, Any], str | None]:
    """Profiles for this call: whatever was passed, then the stored machine.

    Explicit arguments still win, so every 0.1.0 call site keeps working."""
    rec = _machine.load()
    warn = _machine.staleness(rec) if rec else None
    stored = (rec or {}).get("profiles") or {}
    out = {
        "machine": machine_profile or stored.get("machine"),
        "process": process_profile or stored.get("process"),
        "filament": list(filament_profiles) if filament_profiles
                    else ([stored["filament"]] if stored.get("filament") else []),
    }
    if not out["machine"]:
        raise RuntimeError(
            "no printer set up and no machine_profile given. Run setup_printer.")
    return out, warn
```

- [ ] **Step 2: Make `machine_profile` optional on `check_bed_fit`**

Change the signature and the first lines of the body:

```python
@mcp.tool()
def check_bed_fit(model: str, machine_profile: str | None = None,
                  margin_mm: float = 3.0) -> dict[str, Any]:
```

and replace `bed = bed_of(machine_profile)` with:

```python
    profs, warn = _active_profiles(machine_profile)
    rec = _machine.load() or {}
    bed = ((rec.get("derived") or {}).get("bed")
           if not machine_profile else {}) or bed_of(profs["machine"])
```

Add `"stale": warn` to the returned dict in every return path of `check_bed_fit`.

- [ ] **Step 3: Make the profile arguments optional on `slice_model`**

Change the signature so the three profile parameters default to `None`:

```python
def slice_model(
    models: list[str],
    machine_profile: str | None = None,
    process_profile: str | None = None,
    filament_profiles: list[str] | None = None,
    output: str | None = None,
    ...
```

`output` keeps a default only so the published positional order survives. Reject
a missing one as the first statement of the body:

```python
    if not output:
        raise ValueError("output is required (it defaults to None only to keep "
                         "the published positional order)")
```

and replace the two lines that build `settings` and `filaments` with:

```python
    profs, warn = _active_profiles(machine_profile, process_profile,
                                   filament_profiles)
    settings = f"{Path(profs['machine']).expanduser()};{Path(profs['process']).expanduser()}"
    filaments = ";".join(str(Path(f).expanduser()) for f in profs["filament"])
```

Add `"stale": warn` to the returned dict.

The parameter order is unchanged, so no existing call site needs touching.

- [ ] **Step 4: Add smoke coverage**

In `tests/smoke.py`, inside `test_slicer`, after the existing `slice_model` dry-run checks:

```python
            # With a machine set up, the profile arguments become optional.
            # This is the whole point: a caller can no longer supply three
            # profiles that disagree, because it supplies none.
            got = payload(await s.call_tool("machine_info", {}))
            check("machine_info answers before setup",
                  got["ok"] is None and "setup_printer" in got["reason"],
                  str(got)[:80])
```

The session already sets `SLICER_WORKSPACE`; add `"CADLOOP_MACHINE": str(ws / "machine.json")` to the env dict in `test_slicer` so the smoke run never reads a real record.

- [ ] **Step 5: Run everything**

Run: `.venv/bin/pytest -q && .venv/bin/python tests/smoke.py`
Expected: pytest PASSES; smoke prints `all checks passed`

- [ ] **Step 6: Write the known-issues doc**

Create `docs/known-issues.md`:

```markdown
# Known issues in the tools cadloop drives

Vendor quirks cadloop works around at runtime. Each is discovered by probing
rather than asserted in code, so a fix upstream is picked up automatically.

## Creality Print's CLI does not run headless on macOS

**Affects:** Creality Print 7.1.1.4472 on macOS 26, Apple Silicon
**Upstream:** https://github.com/CrealityOfficial/CrealityPrint/issues/574

Every CLI operation except `--help` segfaults, including `--info` on a plain
STL with no profiles loaded. It dies in `Slic3r::GUI::PartPlate::set_shape`
called from `Slic3r::CLI::run`, a null dereference in GUI bed setup that the
headless path never initialises.

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
```

- [ ] **Step 7: Commit**

```bash
git add src/cadloop/slicer_server.py tests/smoke.py docs/known-issues.md
git commit -m "Default profiles from the machine record

check_bed_fit and slice_model take their profiles from the stored machine,
so a caller cannot supply three that disagree. Explicit arguments still
win, keeping every 0.1.0 call site working, and both report when the
record has gone stale.

Vendor quirks are written up in docs/known-issues.md with upstream links,
since they are worked around at runtime rather than asserted in code."
```

---

## Deviations from the spec

- The spec calls the read tool `machine()`. It is `machine_info()` here, because
  `slicer_server.py` imports the module as `_machine` and a tool named `machine`
  would shadow it at the point of use. Same contract, clearer name.
- The spec shows `slice_model(models, output)`. The published parameter order is
  kept instead, with all four of `machine_profile`, `process_profile`,
  `filament_profiles` and `output` defaulting to `None` and a missing `output`
  raising at the top of the body. Reordering would have broken any positional
  caller of a package that is on PyPI. `output` reading as optional when it is
  not is the accepted cost, stated in the docstring.

## What lands after this

The wrong-printer failure is gone, every tool call has lost its profile
arguments, and the vendor quirks are probed rather than hardcoded.

Landing 2 gets its own plan: the packing module and `make_printable`, turning
the remaining forty calls into one. It cannot start before this lands, since
packing and slicing both need `machine()`.
