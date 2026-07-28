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
