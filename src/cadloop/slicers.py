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
REQUIRED_FLAGS = ["--slice", "--export-3mf"]

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
