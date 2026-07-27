"""Rolling interference check for spirograph wheels inside the ring.

Neither OpenSCAD nor the slicer can answer "does this actually roll".
This does: it lays each wheel's pitch curve onto the ring's pitch
circle, walks a full circuit, and measures any overlap between the
two solids. Zero overlap across every position is the pass condition.

Needs shapely:  pip install "cadloop[verify]"
"""

import math, os
from shapely.geometry import Polygon
from shapely.ops import unary_union
from shapely import affinity
M = float(os.environ.get("GEAR_MODULE", "1.5"))
PA = 20.0
CLEAR = 0.25
BACKLASH = 0.30
RES = 8
WHEELS = [24, 30, 32, 36, 40, 45, 48, 52, 56, 63, 72, 80]


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


WHEELS = [24, 30, 32, 36, 40, 45, 48, 52, 56, 63, 72, 80]


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

NR_IN, NR_OUT = 96, 105
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


def main() -> int:
    rows = check_all()
    width = max(len(r["part"]) for r in rows)
    print(f"{'part':<{width}}  teeth  overlap mm2  result")
    bad = 0
    for r in rows:
        bad += 0 if r["pass"] else 1
        print(f"{r['part']:<{width}}  {r['teeth']:5d}  {r['overlap_mm2']:11.6f}  "
              f"{'pass' if r['pass'] else 'FAIL'}")
    print(f"\n{len(rows) - bad}/{len(rows)} parts mesh cleanly")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
