"""The spirograph's own checks. Not part of cadloop.

This file is what cadloop cannot give you and does not pretend to: the
question "does the thing actually work", asked in the only terms this
particular model understands. It ships beside the model rather than inside
the package, because every project's version of this question is different
and none of them generalise. Yours will look nothing like this one.

Two checks, neither of which OpenSCAD nor the slicer can do:

Rolling interference: lays each wheel's pitch curve onto the ring's pitch
circle, walks a full circuit, and measures any overlap between the two
solids. Zero overlap across every position is the pass condition.

Layout collision: renders the sheet and each of its groups, and compares
the union against the sum. A sheet that fuses two parts together still
renders as a clean manifold, slices without complaint, and prints as one
object, so nothing downstream catches it.

Worth saying what it does NOT check, since that is the more useful lesson:
it reported 14/14 parts meshing cleanly through five rounds of a pen hole
nobody could draw with. Interference is not usability. The checks you need
are discovered by holding the printed part, and they get written here.

    python models/verify_spirograph.py

Needs shapely and cadloop:  pip install -e ".[verify]"
The layout check also needs OpenSCAD, and skips if there is none.
"""

import argparse
import math
import re
import os
import tempfile
from pathlib import Path

from shapely.geometry import Polygon
from shapely.ops import unary_union
from shapely import affinity

from cadloop.common import find_binary, measure_stl, run as _sh
M = float(os.environ.get("GEAR_MODULE", "1.5"))
PA = 20.0
CLEAR = 0.25
BACKLASH = 0.30
RES = 8
# Mirrors wheel_teeth in spirograph.scad, and must stay in step with it.
# No 48: it is half of 96, the ratio whose pen only ever traces an ellipse.
WHEELS = [24, 30, 32, 36, 40, 45, 52, 56, 63, 72, 80]


# --- involute gear geometry, mirroring spirograph.scad ---------------

def d2r(a):
    return math.radians(a)


def inv_deg(a_deg):
    a = d2r(a_deg)
    return math.degrees(math.tan(a) - a)

def pol(r, a_deg):
    return (r * math.cos(d2r(a_deg)), r * math.sin(d2r(a_deg)))

def gear_pts(n, add_c=1.0, ded_c=1.0 + CLEAR, bl=0.0):
    rp = M * n / 2
    rb = rp * math.cos(d2r(PA))
    ra = rp + M * add_c
    rr = rp - M * ded_c
    rs = max(rb, rr) + 0.02
    ht = 90.0 / n - (bl / 2) / rp * 180 / math.pi

    def flank(r):
        return max(ht + inv_deg(PA) - inv_deg(math.degrees(math.acos(min(1.0, rb / r)))), 0.1)

    pts = []
    undercut = rs > rr + 0.03
    for i in range(n):
        a = i * 360.0 / n
        pts.append(pol(rr, a - 180.0 / n))
        if undercut:
            pts.append(pol(rr, a - flank(rs)))
        for j in range(RES + 1):
            r = rs + (ra - rs) * j / RES
            pts.append(pol(r, a - flank(r)))
        for j in range(RES, -1, -1):
            r = rs + (ra - rs) * j / RES
            pts.append(pol(r, a + flank(r)))
        if undercut:
            pts.append(pol(rr, a + flank(rs)))
    return pts

def gear(n, **kw):
    p = Polygon(gear_pts(n, **kw))
    return p

NR_IN, NR_OUT = 96, 105
ring_body = gear(NR_OUT)
ring_cut = gear(NR_IN, add_c=1.0 + CLEAR, ded_c=1.0)
ring = ring_body.difference(ring_cut)




P = math.pi * M
TAU = 2 * math.pi

# ---------------------------------------------------------------
# smooth convex curves: point, outward normal, radius of curvature
# ---------------------------------------------------------------

def polar_curve(R, e, k):
    """r(t) = R (1 + e cos k t), t in radians."""
    def f(t):
        c, s = math.cos(k * t), math.sin(k * t)
        r = R * (1 + e * c)
        r1 = -R * e * k * s
        r2 = -R * e * k * k * c
        x = r * math.cos(t); y = r * math.sin(t)
        dx = r1 * math.cos(t) - r * math.sin(t)
        dy = r1 * math.sin(t) + r * math.cos(t)
        sp = math.hypot(dx, dy)
        nang = math.atan2(dy, dx) - math.pi / 2
        num = r * r + 2 * r1 * r1 - r * r2
        rho = (r * r + r1 * r1) ** 1.5 / num
        return (x, y), nang, rho, sp
    return f

def ellipse_curve(a, b):
    def f(t):
        x = a * math.cos(t); y = b * math.sin(t)
        dx = -a * math.sin(t); dy = b * math.cos(t)
        sp = math.hypot(dx, dy)
        nang = math.atan2(dy, dx) - math.pi / 2
        rho = (a * a * math.sin(t) ** 2 + b * b * math.cos(t) ** 2) ** 1.5 / (a * b)
        return (x, y), nang, rho, sp
    return f

NS = 4000

def arclen_table(f):
    s = [0.0]
    for i in range(1, NS + 1):
        t0 = TAU * (i - 1) / NS; t1 = TAU * i / NS
        _, _, _, s0 = f(t0)
        _, _, _, s1 = f(t1)
        s.append(s[-1] + 0.5 * (s0 + s1) * (t1 - t0))
    return s

def t_at(s_tab, s):
    L = s_tab[-1]
    s = s % L
    lo, hi = 0, NS
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if s_tab[mid] <= s: lo = mid
        else: hi = mid
    span = s_tab[hi] - s_tab[lo]
    fr = 0.0 if span <= 0 else (s - s_tab[lo]) / span
    return TAU * (lo + fr) / NS

# ---------------------------------------------------------------
# teeth
# ---------------------------------------------------------------

def involute_tooth(rho, bl):
    ne = 2 * rho / M
    rb = rho * math.cos(math.radians(PA))
    ra = rho + M
    rr = rho - M * (1 + CLEAR)
    rs = max(rb, rr) + 0.02
    ht = 90.0 / ne - (bl / 2) / rho * 180 / math.pi
    def flank(r):
        return max(ht + inv_deg(PA)
                   - inv_deg(math.degrees(math.acos(min(1.0, rb / r)))), 0.1)
    pts = [pol(rr, -flank(rs) - 1.5)]
    if rs > rr + 0.03: pts.append(pol(rr, -flank(rs)))
    for j in range(9):
        r = rs + (ra - rs) * j / 8
        pts.append(pol(r, -flank(r)))
    for j in range(8, -1, -1):
        r = rs + (ra - rs) * j / 8
        pts.append(pol(r, flank(r)))
    if rs > rr + 0.03: pts.append(pol(rr, flank(rs)))
    pts.append(pol(rr, flank(rs) + 1.5))
    return pts

def place(pts, ang, tx, ty):
    c, s = math.cos(ang), math.sin(ang)
    return [(x * c - y * s + tx, x * s + y * c + ty) for x, y in pts]

# ---------------------------------------------------------------
# build
# ---------------------------------------------------------------

def build(kind, params, bl=BACKLASH):
    def mk(scale):
        if kind == 'polar':
            R, e, k = params
            return polar_curve(R * scale, e, k)
        a, b = params
        return ellipse_curve(a * scale, b * scale)

    f = mk(1.0)
    st = arclen_table(f)
    n = int(round(st[-1] / P))
    scale = n * P / st[-1]
    f = mk(scale)
    st = arclen_table(f)
    L = st[-1]

    teeth, table, rhos = [], [], []
    for i in range(n):
        s = L * i / n
        (px, py), nang, rho, _ = f(t_at(st, s))
        rhos.append(rho)
        cx = px - rho * math.cos(nang)
        cy = py - rho * math.sin(nang)
        teeth.append(Polygon(place(involute_tooth(rho, bl), nang, cx, cy)))
        table.append((px, py, math.degrees(nang) % 360.0, rho))

    pitch_pts = [f(TAU * i / 720)[0] for i in range(720)]
    pitch = Polygon(pitch_pts)
    body = pitch.buffer(-M * (1 + CLEAR), join_style=1, quad_segs=32)
    wheel = unary_union([body] + teeth).buffer(0)
    return dict(n=n, L=L, f=f, st=st, table=table, pitch=pitch, wheel=wheel,
                rho_min=min(rhos), rho_max=max(rhos), scale=scale)

# ---------------------------------------------------------------
# rolling test
# ---------------------------------------------------------------

RING = gear(NR_OUT).difference(gear(NR_IN, add_c=1.0 + CLEAR, ded_c=1.0))
R_IN = M * NR_IN / 2
R_OUT = M * NR_OUT / 2

def roll(w, steps=600, inside=True, phase=0.0):
    R = R_IN if inside else R_OUT
    worst = 0.0
    for i in range(steps):
        s = w['L'] * i / steps
        (px, py), nang, _, _ = w['f'](t_at(w['st'], s))
        sgn = 1.0 if inside else -1.0
        phi = (s + phase) / R * sgn
        tgt = phi if inside else phi + math.pi
        psi = tgt - nang
        c, sn = math.cos(psi), math.sin(psi)
        qx, qy = R * math.cos(phi), R * math.sin(phi)
        tx = qx - (px * c - py * sn); ty = qy - (px * sn + py * c)
        mv = affinity.translate(affinity.rotate(w['wheel'], math.degrees(psi),
                                                origin=(0, 0)), tx, ty)
        worst = max(worst, mv.intersection(RING).area)
        if worst > 1.0: break
    return worst

SHAPES = {
    "ellipse": ("ellipse", (24.0, 16.0)),
    "egg":     ("polar",   (14.0, 0.4582, 1)),
    "trefoil": ("polar",   (17.0, 0.0715, 3)),
}



def check_all(steps: int = 400) -> list[dict]:
    """Roll every wheel through a full circuit of the ring. A wheel passes
    when some meshing phase gives zero overlap for the whole circuit."""
    rows = []
    for z in WHEELS:
        rp = M * z / 2
        w = {"L": 2 * math.pi * rp, "st": None,
             "f": lambda t, rp=rp: ((rp * math.cos(t), rp * math.sin(t)), t, rp, rp),
             "wheel": gear(z, bl=BACKLASH), "n": z}
        worst = min(_roll_circular(w, rp, steps, ph)
                    for ph in (0.0, P / 2))
        rows.append({"part": f"{z}T", "teeth": z, "overlap_mm2": round(worst, 6),
                     "pass": worst < 1e-6})
    for name, (kind, prm) in SHAPES.items():
        w = build(kind, prm)
        worst = min(roll(w, steps=steps, inside=True, phase=ph)
                    for ph in (0.0, P / 2))
        rows.append({"part": name, "teeth": w["n"],
                     "rho_min": round(w["rho_min"], 2),
                     "rho_max": round(w["rho_max"], 2),
                     "overlap_mm2": round(worst, 6),
                     "pass": worst < 1e-6})
    return rows


def _roll_circular(w, rp, steps, phase):
    worst = 0.0
    for i in range(steps):
        s = w["L"] * i / steps
        t = s / rp
        px, py = rp * math.cos(t), rp * math.sin(t)
        phi = (s + phase) / R_IN
        psi = phi - t
        c, sn = math.cos(psi), math.sin(psi)
        qx, qy = R_IN * math.cos(phi), R_IN * math.sin(phi)
        tx = qx - (px * c - py * sn)
        ty = qy - (px * sn + py * c)
        mv = affinity.translate(
            affinity.rotate(w["wheel"], math.degrees(psi), origin=(0, 0)), tx, ty)
        worst = max(worst, mv.intersection(RING).area)
        if worst > 1.0:
            break
    return worst


# ---------------------------------------------------------------
# what each wheel draws
# ---------------------------------------------------------------

# A pen in a wheel of r teeth rolling in a ring of R traces a figure with
# R / gcd(R, r) lobes, closing after r / gcd(R, r) circuits of the ring. So
# the tooth counts decide the pattern before any geometry exists, and a
# wheel sharing most of its factors with the ring draws something plain.
# A ratio of exactly 2 is the degenerate case: the pen traces an ellipse.


def _nc_teeth(kind, params) -> int:
    """Tooth count of a non-circular wheel, without building its teeth."""
    if kind == "polar":
        R, e, k = params
        f = polar_curve(R, e, k)
    else:
        a, b = params
        f = ellipse_curve(a, b)
    return int(round(arclen_table(f)[-1] / P))


def patterns() -> dict[str, list[dict]]:
    """What every wheel in the set draws, in each ring."""
    parts = [(f"{t}T", t) for t in WHEELS]
    parts += [(name, _nc_teeth(kind, prm))
              for name, (kind, prm) in SHAPES.items()]
    out = {}
    for label, R in (("main ring", NR_IN), ("outer ring", NR_OUT)):
        rows = []
        for name, r in parts:
            g = math.gcd(R, r)
            rows.append({"part": name, "teeth": r, "gcd": g,
                         "lobes": R // g, "circuits": r // g,
                         "degenerate": R // g <= 2})
        rows.sort(key=lambda x: (x["lobes"], x["teeth"]))
        out[label] = rows
    return out


def print_patterns() -> None:
    for label, R in (("main ring", NR_IN), ("outer ring", NR_OUT)):
        rows = patterns()[label]
        print(f"\n{label}, {R} teeth")
        print(f"  {'part':>8}  {'teeth':>5}  {'lobes':>5}  {'circuits':>8}")
        for r in rows:
            note = ("  draws ellipses only" if r["degenerate"]
                    else "  plain" if r["lobes"] <= 8 else "")
            print(f"  {r['part']:>8}  {r['teeth']:5d}  {r['lobes']:5d}  "
                  f"{r['circuits']:8d}{note}")


# ---------------------------------------------------------------
# layout collision
# ---------------------------------------------------------------

# The groups the "all" sheet is made of. Their volumes must add up to the
# volume of the sheet; if two of them overlap, the union swallows the
# shared material and the sum comes out higher.
LAYOUT_GROUPS = ["ring", "outer_ring", "wheels", "shapes"]

# A tenth of a cubic millimetre is far below any real collision and well
# above the noise between two tessellations of the same solid.
LAYOUT_TOL_MM3 = 0.1


def _openscad() -> str:
    return find_binary(
        "OPENSCAD_BIN",
        ["openscad", "openscad-nightly", "OpenSCAD"],
        ["/Applications/OpenSCAD.app/Contents/MacOS/OpenSCAD"])


def _part_volume(binary: str, scad: Path, part: str, out: Path,
                 timeout_s: float) -> tuple[float | None, str]:
    """Volume of one group, or (None, why) if it could not be rendered.

    The labels are switched off. They are debossed text, they cost more than
    the gears themselves on the older CGAL backend, and where a part sits on
    the sheet does not depend on them."""
    name = Path(binary).name
    stl = out / f"{part}.stl"
    r = _sh([binary, "-o", str(stl),
             "-D", f'part="{part}"',
             "-D", "label_holes=false",
             "-D", "label_wheels=false",
             str(scad)], timeout_s)
    if r["timed_out"]:
        return None, f"{name} did not render {part!r} within {timeout_s:g}s"
    if not stl.exists():
        return None, (f"{name} exited {r['returncode']} without writing "
                      f"{part!r}: {_tail(r['log'], 200).strip() or 'no output'}")
    mesh = measure_stl(stl)
    return (float(mesh["volume_mm3"]) if mesh.get("triangles") else 0.0), ""


def _tail(log: str, n: int = 400) -> str:
    log = log or ""
    return log[-n:]


def check_layout(scad: Path, timeout_s: int = 240) -> dict:
    """Render the sheet and its groups, and compare the union to the sum.

    Two parts laid on top of each other still render as a clean manifold
    and slice without a word, so this is the only place it shows up.

    A render that does not finish means the answer is unknown, not that the
    layout is bad: OpenSCAD 2021 and earlier evaluate this sheet with CGAL
    and take far longer than the Manifold backend that replaced it. That
    comes back as skipped rather than as a failure."""
    binary = _openscad()
    # Beside the model rather than in /tmp: a snap or flatpak OpenSCAD runs
    # with a private /tmp, so it writes the STL into its own sandbox and we
    # find nothing at the path we asked for.
    with tempfile.TemporaryDirectory(dir=scad.parent, prefix=".cadloop-") as tmp:
        out = Path(tmp)
        union, why = _part_volume(binary, scad, "all", out, timeout_s)
        if union is None:
            return {"skipped": True, "reason": why}
        groups = {}
        for g in LAYOUT_GROUPS:
            v, why = _part_volume(binary, scad, g, out, timeout_s)
            if v is None:
                return {"skipped": True, "reason": why}
            groups[g] = v
    total = sum(groups.values())
    fused = total - union
    return {"skipped": False,
            "union_mm3": round(union, 3), "sum_mm3": round(total, 3),
            "fused_mm3": round(fused, 3), "groups": groups,
            "pass": abs(fused) < LAYOUT_TOL_MM3}


def _find_model() -> Path | None:
    """Beside this file first, since the two live together, then relative to
    wherever it was run from."""
    for c in (Path(__file__).resolve().parent / "spirograph.scad",
              Path("models/spirograph.scad"), Path("spirograph.scad")):
        if c.is_file():
            return c.resolve()
    return None


def check_wheel_list(model: Path) -> dict[str, Any]:
    """WHEELS above is a second copy of the model's wheel_teeth, kept because
    the checks run without OpenSCAD. Two copies drift, and a drifted list
    quietly checks parts the sheet no longer has. This is what notices."""
    m = re.search(r"^wheel_teeth\s*=\s*\[([^\]]*)\]", model.read_text(), re.M)
    if not m:
        return {"pass": False, "reason": "no wheel_teeth in the model"}
    from_scad = [int(n) for n in re.findall(r"\d+", m.group(1))]
    ok = from_scad == list(WHEELS)
    return {"pass": ok, "model": from_scad, "here": list(WHEELS),
            "reason": "" if ok else f"{from_scad} in the model, {list(WHEELS)} here"}


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Check the spirograph parts mesh, and that the sheet "
                    "does not lay any of them on top of each other.")
    ap.add_argument("--model", type=Path, default=None,
                    help="path to spirograph.scad (default: ./models/spirograph.scad)")
    ap.add_argument("--skip-mesh", action="store_true",
                    help="skip the rolling interference check")
    ap.add_argument("--skip-layout", action="store_true",
                    help="skip the layout collision check")
    ap.add_argument("--patterns", action="store_true",
                    help="show what each wheel draws in each ring, and exit")
    args = ap.parse_args()

    if args.patterns:
        print_patterns()
        return 0

    bad = 0

    # Before anything else: the two copies of the wheel list must agree, or
    # every result below is about a set of parts the sheet no longer has.
    model_for_list = args.model or _find_model()
    if model_for_list is not None:
        wl = check_wheel_list(model_for_list)
        if not wl["pass"]:
            bad += 1
            print(f"wheels   FAIL  {wl['reason']}\n")

    if not args.skip_mesh:
        rows = check_all()
        width = max(len(r["part"]) for r in rows)
        print(f"{'part':<{width}}  teeth  overlap mm2  result")
        for r in rows:
            bad += 0 if r["pass"] else 1
            print(f"{r['part']:<{width}}  {r['teeth']:5d}  {r['overlap_mm2']:11.6f}  "
                  f"{'pass' if r['pass'] else 'FAIL'}")
        print(f"\n{len(rows) - bad}/{len(rows)} parts mesh cleanly")

    if not args.skip_layout:
        print()
        model = args.model or _find_model()
        if model is None:
            print("layout   skip  no spirograph.scad here, pass --model")
        else:
            try:
                binary = _openscad()
            except RuntimeError:
                binary = None
                print("layout   skip  no OpenSCAD found, set OPENSCAD_BIN")
            if binary is not None:
                res = check_layout(model)
                if res["skipped"]:
                    # Loudly, so a skipped check is never mistaken for a pass.
                    print(f"layout   skip  {res['reason']}")
                else:
                    print(f"{'group':<11}  volume mm3")
                    for g, v in res["groups"].items():
                        print(f"{g:<11}  {v:10.1f}")
                    print(f"{'sum':<11}  {res['sum_mm3']:10.1f}")
                    print(f"{'sheet':<11}  {res['union_mm3']:10.1f}")
                    if res["pass"]:
                        print("\nno parts overlap on the sheet")
                    else:
                        bad += 1
                        print(f"\n{res['fused_mm3']:.1f} mm3 FUSED: parts on "
                              f"the sheet overlap each other")

    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
