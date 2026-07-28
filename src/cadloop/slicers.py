"""Which slicers are installed, and which of them actually runs.

Deliberately no preference order baked in. A binary earns its place by
answering --help with the flags we need. Creality Print's CLI does not run
headless on macOS today and OrcaSlicer does, but that is something this
probes and records rather than something the code asserts, so it corrects
itself when upstream ships a fix.
"""

from __future__ import annotations

import glob as _glob
import json
import os
import re
import shutil
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
    r"C:\Program Files\Creality\Creality Print 6.0\CrealityPrint.exe",
]

# The same search space by name, for installs that put the binary on PATH
# rather than in one of the locations above. Also in no significant order.
SLICER_NAMES = ["OrcaSlicer", "orca-slicer", "BambuStudio", "bambu-studio",
                "ElegooSlicer", "elegoo-slicer", "CrealityPrint",
                "creality-print"]

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
    """Every candidate that exists on disk or on PATH. Says nothing about
    whether it works: probe() decides that."""
    out, seen = [], set()
    found = [Path(c).expanduser() for c in SLICER_CANDIDATES]
    for n in SLICER_NAMES:
        hit = shutil.which(n)
        if hit:
            found.append(Path(hit))
    for p in found:
        if p.is_file() and str(p) not in seen:
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
