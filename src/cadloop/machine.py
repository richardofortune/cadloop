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


class RecordError(RuntimeError):
    """The record's location or contents cannot be used, and guessing where
    it should have been is worse than saying so."""

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


def _unsafe(p: Path) -> str | None:
    """Why another user could substitute the record at this path, or None.

    The question is not "do I own this directory" - /tmp is owned by root and
    is a perfectly ordinary place to put a file - but "could somebody else
    replace what I put here". That is: a directory anyone may write to
    without the sticky bit that stops them removing other people's files, a
    directory belonging to some other unprivileged user, or a file already
    there that is not ours. Windows has no getuid, and a filesystem that
    cannot answer gets the benefit of the doubt rather than a refusal."""
    if not hasattr(os, "getuid"):
        return None
    me = os.getuid()
    try:
        d = p.parent.stat()
    except OSError:
        return None
    if d.st_uid not in (me, 0):
        return f"{p.parent} belongs to another user"
    if (d.st_mode & 0o022) and not (d.st_mode & 0o1000):
        return f"{p.parent} is writable by other users"
    try:
        if p.exists() and p.stat().st_uid != me:
            return f"{p} belongs to another user"
    except OSError:
        return None
    return None


def record_path() -> Path:
    """Config, not user data, so it lives outside the workspace sandbox and
    survives changing directory.

    CADLOOP_MACHINE names a file that later supplies an executable path to
    subprocess.run, so it is checked here rather than trusted: resolved so
    that a `..` chain cannot be read one way and written another, required
    to be a regular file, and required to sit somewhere no other user could
    substitute it. Trees are not created for it either; a variable that can
    mkdir -p anywhere is a wider hole than the record is worth."""
    env = os.environ.get("CADLOOP_MACHINE")
    if not env:
        base = os.environ.get("XDG_CONFIG_HOME") or "~/.config"
        return Path(base).expanduser() / "cadloop" / "machine.json"
    try:
        p = Path(env).expanduser().resolve()
    except OSError as exc:
        raise RecordError(f"CADLOOP_MACHINE={env!r} cannot be resolved: {exc}")
    if p.is_dir():
        raise RecordError(f"CADLOOP_MACHINE={env!r} is a directory, not a file")
    if p.exists() and not p.is_file():
        raise RecordError(
            f"CADLOOP_MACHINE={env!r} is not a regular file")
    unsafe = _unsafe(p) if p.parent.exists() else None
    if unsafe:
        raise RecordError(f"CADLOOP_MACHINE={env!r} is not safe to trust: "
                          f"{unsafe}, so another user could replace the record "
                          f"and choose the program this server runs")
    return p


def save(rec: dict[str, Any]) -> Path:
    """Write the record, atomically. A kill mid-write used to leave a
    truncated file where the previous good one was."""
    p = record_path()
    if not p.parent.exists():
        if os.environ.get("CADLOOP_MACHINE"):
            raise RecordError(
                f"CADLOOP_MACHINE points into {p.parent}, which does not exist. "
                "Create it, or unset the variable to use the default location.")
        p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.parent / f".{p.name}.{os.getpid()}.tmp"
    try:
        tmp.write_text(json.dumps(rec, indent=1, sort_keys=True))
        os.replace(tmp, p)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
    return p


def load() -> dict[str, Any] | None:
    p = record_path()
    if not p.is_file():
        return None
    try:
        rec = json.loads(p.read_text())
    except OSError as exc:
        raise RecordError(f"cannot read the machine record at {p}: {exc}")
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
    if binary and not Path(binary).is_file():
        return f"the recorded slicer at {binary} is not a program"
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


def bed(rec: dict[str, Any] | None,
        machine_profile: str | Path | None = None) -> dict[str, Any]:
    """The printable area to measure a part against.

    Every tool that compares something to the bed needs the same answer,
    and it is not simply "what the record cached" nor simply "what the
    profile says". The fingerprint covers the machine profile but not the
    parents it inherits printable_area from, so the cache can be right
    about the file and wrong about the bed — the live profile wins. But
    bed_of() reports z_mm None when no ancestor declares printable_height,
    and taking that answer whole discards a height the record read when one
    did. Losing the height is not a harmless gap: every consumer skips the
    z bound when it has no number, so an absent height turns a check off
    rather than failing it.

    So the two are merged field by field, live over cached. Both are
    optional: pass rec None to read a profile on its own terms, which is
    what a caller naming some other printer's profile wants, since the
    stored record describes a different machine. A field neither source
    knows is absent from the result rather than present and None, so
    bed.get("z_mm") is the one question a caller has to ask.

    Returns {} when nothing anywhere declares a printable area.
    """
    profile = machine_profile or ((rec or {}).get("profiles") or {}).get("machine")
    live: dict[str, Any] = {}
    if profile:
        try:
            live = _profiles.bed_of(profile)
        except Exception:
            live = {}
    out = dict(((rec or {}).get("derived") or {}).get("bed") or {})
    out.update({k: v for k, v in live.items() if v is not None})
    return {k: v for k, v in out.items() if v is not None}


def _rank(hits: list[tuple[str, Path]],
          needle: str) -> list[tuple[str, Path]]:
    """An exact name always wins outright, so "Creality K1" does not drag in
    "Creality K1C". Otherwise sorted, so nothing depends on the order the
    filesystem hands directory entries back."""
    exact = [h for h in hits if h[0].lower() == needle.lower()]
    if exact:
        return exact[:1]
    return sorted(hits, key=lambda t: (t[0], str(t[1])))


def _match(kind: str, needle: str) -> list[tuple[str, Path]]:
    """Every profile of a kind whose name contains needle, across every
    install, one path per name. Which install won a shared name is decided
    the same way profile_index() decides it, and undone by the retry in
    resolve() when the winner turns out to have no matching triple."""
    seen: set[str] = set()
    hits: list[tuple[str, Path]] = []
    for root in _profiles.profile_roots():
        for rec in _profiles.root_profiles(root):
            if rec["kind"] != kind:
                continue
            name = str(rec["name"])
            if name in seen:
                continue
            if needle.lower() in name.lower():
                seen.add(name)
                hits.append((name, Path(rec["path"])))
    return _rank(hits, needle)


def _as_install(root: Path) -> dict[str, Any]:
    """The install dict profiles_in() expects, for a root already known to
    be one (picked from profile_roots() or handed back by install_of())."""
    return {"root": str(root), "name": Path(root).name}


def _match_in(root: Path, kind: str, needle: str) -> list[tuple[str, Path]]:
    """The same question asked of one install's own files.

    _match() keeps one path per name across every root, so the copies it drops
    are invisible to it - and when two Orca forks ship the same process and
    filament names, those dropped copies are exactly the ones that complete
    the other install's triple.

    profiles_in() lists every file, including two that share a name within
    this same install - a vendor install can ship one profile twice, under
    two filenames. That is one hit, not two: deduping belongs here, where
    resolution happens, not in profiles_in(), which is a listing API and
    would otherwise be hiding a file that genuinely exists on disk."""
    seen: set[str] = set()
    hits: list[tuple[str, Path]] = []
    for rec in _profiles.profiles_in(_as_install(root), kind, needle):
        name = str(rec["name"])
        if name in seen:
            continue
        seen.add(name)
        hits.append((name, Path(rec["path"])))
    return _rank(hits, needle)


def _root_of(path: str | Path) -> Path | None:
    """The install a profile belongs to, as the Path resolve() compares
    against - install_of()'s dict form, unwrapped."""
    install = _profiles.install_of(path)
    return Path(install["root"]) if install else None


def _under(root: Path | None,
          hits: list[tuple[str, Path]]) -> list[tuple[str, Path]]:
    """Keep only candidates from the same install as the machine profile.

    Two vendors ship profiles under the same name, and their contents differ
    in ways that decide whether a slice validates at all: Creality's machine
    profile sets use_relative_e_distances, OrcaSlicer's leaves it unset. A
    triple assembled across installs is not a configuration anyone tested.

    Same install means "the most specific configured root that holds it is
    the same one", not "sits somewhere underneath it". Pointing
    SLICER_PROFILE_DIRS at /Applications makes the second test true for every
    vendor at once, which is how this guard was reachable around."""
    if root is None:
        return hits
    return [(n, p) for n, p in hits if _root_of(p) == root]


def _named(kind: str, name: str, root: Path) -> Path | None:
    """A single profile of a kind with exactly this name, read straight off
    one root rather than through the process-wide profile_index().

    profile_index() keeps one path per name, whichever root it saw first, so
    it can only ever show one vendor's copy of a machine two vendors ship
    under the identical name. That is fine for picking a printer, but it
    means the other vendor's copy - the one that might actually have a
    matching process and filament - is otherwise invisible."""
    for rec in _profiles.profiles_in(_as_install(root), kind):
        if rec["name"].lower() == name.lower():
            return Path(rec["path"])
    return None


class _Conflict(Exception):
    """A profile the caller asked for by name cannot be honoured here, and
    quietly using a different one instead is the failure mode this module
    exists to remove."""


def resolve(printer: str | None, filament: str | None,
           process: str | None = None,
           explicit: list[str] | tuple[str, ...] | None = None) -> dict[str, Any]:
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
    same machine name before giving up.

    explicit names which of "process" and "filament" the caller demanded,
    rather than read out of the slicer's own configuration. A demanded
    profile is honoured by name or refused by name; it is never swapped for
    another one. An adopted one is a starting point - a stored preset can be
    two changes out of date - so when it cannot be used here the printer's
    own default is taken instead and the substitution is reported in notes.
    The default, no explicit list, treats everything given as demanded."""
    demanded = ("process", "filament") if explicit is None else tuple(explicit)
    if not printer:
        return {"ok": False, "reason": "no printer given and none configured",
                "candidates": [], "notes": []}
    machines = _match("machine", printer)
    if not machines:
        return {"ok": False, "reason": f"no printer matches {printer!r}",
                "candidates": [], "notes": []}
    if len(machines) > 1:
        return {"ok": False,
                "reason": f"{printer!r} matches {len(machines)} printers",
                "candidates": [n for n, _ in machines], "notes": []}
    name, mpath = machines[0]
    mpath = Path(mpath)
    root = _root_of(mpath)

    def anywhere(kind: str, needle: str) -> list[tuple[str, Path]]:
        out: list[tuple[str, Path]] = []
        for r in _profiles.profile_roots():
            out += _match_in(r, kind, needle)
        return out

    def pick_in(root: Path | None, kind: str, want: str | None,
                notes: list[str]) -> str | None:
        if want:
            hits = _under(root, _match_in(root, kind, want) if root
                          else _match(kind, want))
            if len(hits) == 1:
                return str(hits[0][1])
            if kind in demanded:
                if len(hits) > 1:
                    raise _Conflict(
                        f"{kind} {want!r} matches {len(hits)} profiles for "
                        f"{name}: " + ", ".join(n for n, _ in hits))
                other = anywhere(kind, want)
                if other:
                    where = sorted({str(_root_of(p) or Path(p).parent)
                                    for _, p in other})
                    raise _Conflict(
                        f"{kind} {want!r} is not in the same install as {name} "
                        f"({root}); it lives under " + ", ".join(where))
                raise _Conflict(f"no {kind} profile matches {want!r}")
        # fall back to anything scoped to this printer, same install only
        hits = _under(root, _match_in(root, kind, name) if root
                      else _match(kind, name))
        if not hits:
            return None
        if want:
            notes.append(f"the configured {kind} {want!r} is not available for "
                         f"{name}; used {hits[0][0]!r} instead")
        return str(hits[0][1])

    def attempt(root: Path | None,
                mpath: Path) -> tuple[dict[str, str] | None, str, list[str]]:
        notes: list[str] = []
        try:
            proc = pick_in(root, "process", process, notes)
            fil = pick_in(root, "filament", filament, notes)
        except _Conflict as exc:
            return None, str(exc), notes
        missing = [k for k, v in (("process", proc), ("filament", fil)) if not v]
        if missing:
            return None, (f"found {name} but no {' or '.join(missing)} profile "
                          f"for it under {root or 'its install'}"), notes
        return ({"machine": str(mpath), "process": str(proc),
                 "filament": str(fil)}, "", notes)

    got, why, notes = attempt(root, mpath)
    if got is None and root is not None:
        for alt_root in _profiles.profile_roots():
            if alt_root == root:
                continue
            alt_machine = _named("machine", name, alt_root)
            # A root that only holds this copy because it is an ancestor of
            # the root that really owns it is not a second install.
            if not alt_machine or _root_of(alt_machine) != alt_root:
                continue
            alt, alt_why, alt_notes = attempt(alt_root, alt_machine)
            if alt is not None:
                got, why, notes, root = alt, "", alt_notes, alt_root
                break

    if got is None:
        return {"ok": False, "reason": why, "candidates": [], "notes": notes}
    names = {kind: ((_profiles.classify(Path(path)) or {}).get("name")
                    or Path(path).stem)
             for kind, path in got.items()}
    return {"ok": True, "reason": "", "candidates": [], "printer": name,
            "profiles": got, "names": names, "notes": notes}
