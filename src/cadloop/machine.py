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

from . import profiles as _profiles

SCHEMA = 1

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


def _root_of(path: Path) -> Path | None:
    for root in _profiles.profile_roots():
        try:
            path.relative_to(root)
            return root
        except ValueError:
            continue
    return None


def _under(root: Path | None,
          hits: list[tuple[str, Path]]) -> list[tuple[str, Path]]:
    """Keep only candidates from the same install as the machine profile.

    Two vendors ship profiles under the same name, and their contents differ
    in ways that decide whether a slice validates at all: Creality's machine
    profile sets use_relative_e_distances, OrcaSlicer's leaves it unset. A
    triple assembled across installs is not a configuration anyone tested."""
    if root is None:
        return hits
    out = []
    for n, p in hits:
        try:
            p.relative_to(root)
            out.append((n, p))
        except ValueError:
            pass
    return out


def _named(kind: str, name: str, root: Path) -> Path | None:
    """A single profile of a kind with exactly this name, read straight off
    one root rather than through the process-wide profile_index().

    profile_index() keeps one path per name, whichever root it saw first, so
    it can only ever show one vendor's copy of a machine two vendors ship
    under the identical name. That is fine for picking a printer, but it
    means the other vendor's copy - the one that might actually have a
    matching process and filament - is otherwise invisible."""
    for p in root.rglob("*.json"):
        rec = _profiles.classify(p)
        if rec and rec["kind"] == kind and rec["name"].lower() == name.lower():
            return p
    return None


def resolve(printer: str | None, filament: str | None,
           process: str | None = None) -> dict[str, Any]:
    """Turn a printer name into a machine, process and filament triple.

    Refuses ambiguity rather than guessing. "K1" matches five printers
    across four nozzle sizes, and picking the first is how the wrong
    machine gets chosen. The process and filament are also constrained to
    the same profile root as the chosen machine: two vendors can ship a
    profile under the identical name, and mixing installs is how a Creality
    machine profile ends up paired with an OrcaSlicer process profile that
    was never validated against it.

    profile_index() keeps only one path per name across every root, so when
    two vendors ship the same machine name it silently hides whichever copy
    it did not see first - and that copy can be the one with a matching
    process and filament. If the root that won the name has no complete
    triple, every other root is checked directly for its own copy of the
    same machine name before giving up."""
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
    mpath = Path(mpath)
    root = _root_of(mpath)

    def pick_in(root: Path | None, kind: str, want: str | None) -> str | None:
        if want:
            hits = _under(root, _match(kind, want))
            if len(hits) == 1:
                return str(hits[0][1])
        # fall back to anything scoped to this printer, same install only
        hits = _under(root, _match(kind, name))
        return str(hits[0][1]) if hits else None

    proc = pick_in(root, "process", process)
    fil = pick_in(root, "filament", filament)

    if (not proc or not fil) and root is not None:
        for alt_root in _profiles.profile_roots():
            if alt_root == root:
                continue
            alt_machine = _named("machine", name, alt_root)
            if not alt_machine:
                continue
            alt_proc = pick_in(alt_root, "process", process)
            alt_fil = pick_in(alt_root, "filament", filament)
            if alt_proc and alt_fil:
                mpath, root, proc, fil = alt_machine, alt_root, alt_proc, alt_fil
                break

    missing = [k for k, v in (("process", proc), ("filament", fil)) if not v]
    if missing:
        return {"ok": False,
                "reason": f"found {name} but no {' or '.join(missing)} profile "
                          f"for it under {root or 'its install'}",
                "candidates": []}
    return {"ok": True, "reason": "", "candidates": [], "printer": name,
            "profiles": {"machine": str(mpath), "process": proc,
                         "filament": fil}}
