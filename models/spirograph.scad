// ============================================================
// spirograph.scad  -  a printable spirograph set
//
// Circular wheels with pen holes, three non-circular wheels
// (ellipse, egg, trefoil), a main ring with internal teeth for
// drawing inside, and an outer ring for drawing around the outside.
// All dimensions in millimetres. Involute teeth, so everything with
// the same "gear_module" meshes with everything else. Pen holes are
// numbered 1..n outward from the centre, on both the circular and the
// non-circular wheels.
//
// Render one part at a time by setting "part" below.
// ============================================================

/* [What to render] */
// "all", "ring", "outer_ring", "wheels", "shapes", "fit_test",
// a shape name such as "ellipse", "egg" or "trefoil", or a tooth count e.g. 36
part = "all";

/* [Gearing] */
gear_module    = 1.5;    // tooth size. 1.5 prints cleanly on a 0.4 mm nozzle
pressure_angle = 20;
backlash       = 0.30;   // circumferential play, applied to the wheels only
tip_clearance  = 0.25;   // radial gap coefficient (x module)

/* [Rings] */
// "flanged" gives a wide, stiff, hold-down-able ring (recommended).
// "classic" gives the old one-piece 96 inner / 105 outer ring.
ring_style       = "flanged";
ring_inner_teeth = 96;   // internal teeth, main ring
ring_outer_teeth = 105;  // external teeth, outer ring
ring_thickness   = 4;

/* [Ring usability] */
flange_width  = 10;      // solid band around the teeth, for holding down
lip_height    = 3;       // raised rim to push against, also stiffens the ring
lip_width     = 3;
lead_in       = 1.2;     // funnel chamfer so wheels drop into mesh easily
lead_in_open  = 0.9;     // how far the funnel opens out
pin_holes     = 8;       // holes for drawing pins. 0 to disable
pin_hole_d    = 2.6;
grip_channel  = true;    // underside groove for a non-slip bead
channel_w     = 4.5;
channel_d     = 1.4;

/* [Wheels] */
// No 48. It is exactly half the 96 tooth ring, and a wheel at half the ring's
// count traces an ellipse and nothing else. Run `python models/verify_spirograph.py --patterns`
// before adding a count: lobes are ring / gcd(ring, wheel), so a wheel sharing
// most of its factors with the ring draws something plain.
wheel_teeth     = [24, 30, 32, 36, 40, 45, 52, 56, 63, 72, 80];
// Thickness is what decides whether the nib reaches the paper. A pen is a
// cone: the bore has to clear not the tip but the pen's width a plate's
// thickness back from it, and at 4 mm that is the barrel, so the pen bottoms
// out and hovers. 2.5 is near a shop-bought wheel. Thinner still if your pen
// needs it: measure the pen 2 mm back from the nib and keep that under
// pen_hole_d. Nothing about hole spacing depends on this, so it costs no holes.
wheel_thickness = 2.5;
pen_hole_d      = 3.0;   // bore. 2.4 measured too tight once printed. Measure yours
pen_hole_edge   = 3.0;   // material left outside the outermost hole
pen_r0          = 5.5;   // floor for the first hole, clears the tooth count
// ...but never nearer the middle than this much of the pitch radius. A fixed
// 5.5 is a third of the way out on the 24, and a useless twelfth on the 80,
// where the first holes drew cramped little circles worth none of the plate.
pen_r0_frac     = 0.25;
pen_spiral_dr   = 1.4;   // radial step between successive holes
pen_max_holes   = 24;    // cap, so the big wheels stay strong
pen_cs_rim      = 0.5;   // countersink flare per side, on the top face
// Depth as a fraction of the plate, not a fixed number: a fixed one read as
// too shallow at 4 mm and again at 2.5. Only the flare above costs hole
// spacing, so depth is free and worth spending. 0.6 leaves the rest of the
// plate as parallel bore to steady the pen.
pen_cs_frac     = 0.6;

/* [Hole labelling] */
// Every hole is numbered, counting outward from the centre.
label_holes    = true;
hole_label_h   = 2.6;    // character height
hole_label_off = 3.6;    // how far the number sits beside its hole
label_wheels    = true;  // deboss the tooth count on each wheel
label_depth     = 0.6;

/* [Layout preview] */
layout_cols = 4;
layout_gap  = 3;

/* [Quality] */
res = 8;                 // points per involute flank
$fn = 48;

// ------------------------------------------------------------
// gear geometry
// ------------------------------------------------------------

function inv(a)    = (tan(a) - a * PI / 180) * 180 / PI;
function pol(r, a) = [r * cos(a), r * sin(a)];

// half tooth width in degrees at radius r, tooth centred on angle 0
function flank(r, rb, ht) =
    max(ht + inv(pressure_angle) - inv(acos(min(1, rb / r))), 0.1);

function gear_pts(n, add_c, ded_c, bl) =
    let (m  = gear_module,
         rp = m * n / 2,
         rb = rp * cos(pressure_angle),
         ra = rp + m * add_c,
         rr = rp - m * ded_c,
         rs = max(rb, rr) + 0.02,
         ht = 90 / n - (bl / 2) / rp * 180 / PI,
         uc = rs > rr + 0.03)   // base circle above root circle: relieve the flank
    [ for (i = [0 : n - 1])
        let (a = i * 360 / n)
        each concat(
            [ pol(rr, a - 180 / n) ],
            uc ? [ pol(rr, a - flank(rs, rb, ht)) ] : [],
            [ for (j = [0 : res])
                let (r = rs + (ra - rs) * j / res) pol(r, a - flank(r, rb, ht)) ],
            [ for (j = [res : -1 : 0])
                let (r = rs + (ra - rs) * j / res) pol(r, a + flank(r, rb, ht)) ],
            uc ? [ pol(rr, a + flank(rs, rb, ht)) ] : []
        )
    ];

module gear2d(n, add_c = 1.0, ded_c = 1.0 + tip_clearance, bl = 0) {
    polygon(gear_pts(n, add_c, ded_c, bl));
}

function tip_r(n)  = gear_module * (n / 2 + 1);
function root_r(n) = gear_module * (n / 2 - 1 - tip_clearance);

// root radius of a set of internal teeth, i.e. the back of the tooth
function int_root_r(n) = gear_module * (n / 2 + 1 + tip_clearance);

// ------------------------------------------------------------
// shared ring features
// ------------------------------------------------------------

// raised rim at radius r, facing outward if out = true
module lip(r, out = true) {
    translate([0, 0, ring_thickness])
        linear_extrude(lip_height)
            difference() {
                circle(r = out ? r : r + lip_width);
                circle(r = out ? r - lip_width : r);
            }
}

module pin_ring(r) {
    if (pin_holes > 0)
        for (i = [0 : pin_holes - 1])
            rotate(i * 360 / pin_holes)
                translate([r, 0, -1])
                    cylinder(d = pin_hole_d, h = ring_thickness + 2, $fn = 24);
}

module grip_groove(r) {
    if (grip_channel)
        translate([0, 0, -0.01])
            linear_extrude(channel_d)
                difference() {
                    circle(r = r + channel_w / 2);
                    circle(r = r - channel_w / 2);
                }
}

// ------------------------------------------------------------
// main ring: internal teeth in a wide flange
// ------------------------------------------------------------
// The internal teeth are cut by a gear whose addendum and dedendum
// are swapped, which gives the mating wheel clearance at tip and root.
// The same cutter, extruded with a slight upward scale, forms the
// lead-in funnel so wheels drop into mesh instead of being fought in.

module ring_flanged() {
    ri = int_root_r(ring_inner_teeth);
    ro = ri + flange_width;

    difference() {
        union() {
            linear_extrude(ring_thickness) circle(r = ro);
            lip(ro, out = true);
        }
        // internal teeth, full depth
        translate([0, 0, -1])
            linear_extrude(ring_thickness + lip_height + 2)
                gear2d(ring_inner_teeth, 1.0 + tip_clearance, 1.0);
        // lead-in funnel on the top face
        translate([0, 0, ring_thickness - lead_in])
            linear_extrude(lead_in + 0.01, scale = 1 + lead_in_open / ri)
                gear2d(ring_inner_teeth, 1.0 + tip_clearance, 1.0);
        pin_ring(ri + 2.6);
        grip_groove(ro - channel_w / 2 - 0.8);
    }
}

// ------------------------------------------------------------
// outer ring: external teeth, for drawing around the outside
// ------------------------------------------------------------

module ring_outer() {
    rr = root_r(ring_outer_teeth);
    rh = rr - flange_width - 2;       // central hole

    difference() {
        union() {
            linear_extrude(ring_thickness) gear2d(ring_outer_teeth);
            lip(rh, out = false);
        }
        translate([0, 0, -1])
            linear_extrude(ring_thickness + lip_height + 2) circle(r = rh);
        pin_ring(rh + lip_width + 2.1);
        grip_groove(rr - channel_w / 2 - 0.3);
    }
}

// ------------------------------------------------------------
// classic one-piece ring, kept for compatibility
// ------------------------------------------------------------

module ring2d_classic() {
    difference() {
        gear2d(ring_outer_teeth);
        gear2d(ring_inner_teeth, 1.0 + tip_clearance, 1.0);
    }
}

module ring() {
    if (ring_style == "classic") linear_extrude(ring_thickness) ring2d_classic();
    else ring_flanged();
}

// ------------------------------------------------------------
// wheels
// ------------------------------------------------------------

// Holes follow a spiral: each one steps out by pen_spiral_dr and turns
// by the golden angle, so no two share a radius and none of them line
// up on a spoke. That also keeps them far apart, which leaves room to
// print a number beside each.

GOLDEN = 137.50776;

// The figure a hole draws is set by how far out it sits as a fraction of the
// pitch radius, not by its distance in millimetres: near the middle it traces
// something close to a circle whatever the wheel. So the innermost hole is
// placed by whichever constraint binds, the label in the middle or that
// fraction, which keeps every hole on every wheel inside the range that
// actually draws something.
function pen_r_min(n) = max(pen_r0, pen_r0_frac * gear_module * n / 2);
function pen_r_max(n) = root_r(n) - pen_hole_edge - pen_hole_d / 2;
function pen_count(n) =
    max(1, min(pen_max_holes,
               floor((pen_r_max(n) - pen_r_min(n)) / pen_spiral_dr) + 1));
function pen_radius(n, i) =
    let (k = pen_count(n), a = pen_r_min(n))
    k < 2 ? a : a + (pen_r_max(n) - a) * i / (k - 1);

// A straight bore with a funnel around the top, the way a shop-bought
// spirograph is countersunk: the pen tip drops in instead of being aimed,
// and it can lean without the printed rim catching on its shoulder.
// Cut in 3D, so every wheel subtracts this rather than a flat circle.
function pen_cs_depth() = pen_cs_frac * wheel_thickness;

module pen_bore() {
    cs = pen_cs_depth();
    translate([0, 0, -0.5])
        cylinder(d = pen_hole_d, h = wheel_thickness + 1, $fn = 24);
    translate([0, 0, wheel_thickness - cs])
        cylinder(d1 = pen_hole_d, d2 = pen_hole_d + 2 * pen_cs_rim,
                 h = cs + 0.01, $fn = 24);
}

// Closest two hole centres may sit before their funnels meet. 0.8 mm of
// material between them is two perimeters at a 0.4 mm nozzle.
function pen_pitch_min() = pen_hole_d + 2 * pen_cs_rim + 0.8;

module hole_labels(n) {
    for (i = [0 : pen_count(n) - 1])
        rotate(i * GOLDEN)
            translate([pen_radius(n, i), hole_label_off])
                rotate(90)
                    text(str(i + 1), size = hole_label_h,
                         halign = "center", valign = "center");
}

module wheel(n) {
    difference() {
        linear_extrude(wheel_thickness)
            gear2d(n, 1.0, 1.0 + tip_clearance, backlash);
        for (i = [0 : pen_count(n) - 1])
            rotate(i * GOLDEN)
                translate([pen_radius(n, i), 0])
                    pen_bore();
        if (label_wheels)
            translate([0, 0, wheel_thickness - label_depth])
                linear_extrude(label_depth + 0.1) {
                    text(str(n), size = min(4.6, root_r(n) * 0.42),
                         halign = "center", valign = "center");
                    if (label_holes) hole_labels(n);
                }
    }
}

// ------------------------------------------------------------
// non-circular wheels
// ------------------------------------------------------------
// A wheel only rolls inside the ring if its radius of curvature stays
// below the ring's everywhere, so no flats and no sharp corners. Each
// tooth is the involute of the equivalent gear at the local radius of
// curvature, placed at equal arc length around the pitch curve. The
// tables below were generated and then checked by rolling each shape
// through a full circuit of the ring looking for interference.

// generated: [name, kind, params, teeth, label_pt, tooth_table, holes]
// all lengths in units of gear_module
nc_shapes = [
  ["ellipse", "e", [16.0392,10.6928], 27, [0,-0.0385],
   [[16.0392,0,0,7.1285],[15.3743,3.0467,24.0312,8.2408],[13.6266,5.6401,42.9621,11.1538],[11.2165,7.6433,56.8868,14.9536],[8.4354,9.0946,67.5971,18.7322],[5.4481,10.057,76.4628,21.783],[2.3524,10.5772,84.3549,23.6288],[-0.7853,10.68,91.8717,24.0108],[-3.9092,10.3703,99.5109,22.8777],[-6.9604,9.6335,107.8028,20.3837],[-9.8599,8.4338,117.4562,16.8951],[-12.4824,6.7147,129.5639,13.0034],[-14.6079,4.4154,145.7805,9.5251],[-15.868,1.5583,167.5401,7.4143],[-15.868,-1.5583,192.4599,7.4143],[-14.6079,-4.4154,214.2195,9.5251],[-12.4824,-6.7147,230.4361,13.0034],[-9.8599,-8.4338,242.5438,16.8951],[-6.9604,-9.6335,252.1972,20.3837],[-3.9092,-10.3703,260.4891,22.8777],[-0.7853,-10.68,268.1283,24.0108],[2.3524,-10.5772,275.6451,23.6288],[5.4481,-10.057,283.5372,21.783],[8.4354,-9.0946,292.4029,18.7322],[11.2165,-7.6433,303.1132,14.9536],[13.6266,-5.6401,317.0379,11.1538],[15.3743,-3.0467,335.9688,8.2408]],
   [[4.3333,-0.0385,5.823,1.4512],[6.8667,-0.0385,6.8667,2.0682],[9.4,-0.0385,9.4,2.0682],[11.9333,-0.0385,11.9333,2.0682],[3.0641,3.0257,3.0641,5.1323],[4.8555,4.817,6.3451,6.3066],[0,4.2949,-1.4896,5.7845],[0,6.8282,-2.1067,6.8282],[-3.0641,3.0257,-5.1708,3.0257],[-4.8555,4.817,-6.3451,6.3066],[-4.3333,-0.0385,-5.823,-1.5281],[-6.8667,-0.0385,-6.8667,-2.1451],[-9.4,-0.0385,-9.4,-2.1451],[-11.9333,-0.0385,-11.9333,-2.1451],[-3.0641,-3.1026,-3.0641,-5.2093],[-4.8555,-4.8939,-6.3451,-6.3836],[-0,-4.3718,1.4896,-5.8614],[-0,-6.9051,2.1067,-6.9051],[3.0641,-3.1026,5.1708,-3.1026],[4.8555,-4.8939,6.3451,-6.3836]]],
  ["egg", "p", [9.4947,0.4582,1], 20, [4.3504,-0.0216],
   [[13.8452,0,0,10.5349],[13.38,3.0952,17.1088,10.493],[12.022,5.915,34.3553,10.367],[9.8842,8.2003,51.8848,10.1565],[7.1523,9.7254,69.8569,9.8621],[4.081,10.3171,88.4516,9.4882],[0.9855,9.8776,107.8647,9.0521],[-1.7744,8.4117,128.2547,8.6188],[-3.8312,6.0607,149.4506,8.4683],[-4.9135,3.1286,169.3716,10.5317],[-5.1442,0,180,33.3392],[-4.9135,-3.1286,190.6284,10.5317],[-3.8312,-6.0607,210.5494,8.4683],[-1.7744,-8.4117,231.7453,8.6188],[0.9855,-9.8776,252.1353,9.0521],[4.081,-10.3171,271.5484,9.4882],[7.1523,-9.7254,290.1431,9.8621],[9.8842,-8.2003,308.1152,10.1565],[12.022,-5.915,325.6447,10.367],[13.38,-3.0952,342.8912,10.493]],
   [[8.6838,-0.0216,10.7904,-0.0216],[7.4146,3.0425,8.9042,4.5322],[4.3504,4.3117,4.3504,6.4184],[1.2863,3.0425,-0.2033,4.5322],[0.0171,-0.0216,-2.0896,-0.0216],[1.2863,-3.0857,-0.2033,-4.5754],[4.3504,-4.3549,4.3504,-6.4616],[7.4146,-3.0857,8.9042,-4.5754]]],
  ["trefoil", "p", [11.3702,0.0715,3], 23, [0.8128,-0.0444],
   [[12.1831,0,0,7.6118],[11.5531,3.0562,22.9514,8.3352],[9.8516,5.6798,42.0697,11.1283],[7.487,7.7385,54.5911,20.5392],[4.8232,9.4011,60.8815,33.3358],[1.9944,10.7622,68.7435,15.9669],[-1.0401,11.54,83.6816,9.8432],[-4.158,11.3388,104.4444,7.9249],[-6.9597,9.9668,127.8561,7.689],[-9.0014,7.6047,149.9111,8.953],[-10.122,4.6824,167.0733,13.0349],[-10.5197,1.5702,177.1419,27.0884],[-10.5197,-1.5702,182.8581,27.0884],[-10.122,-4.6824,192.9267,13.0349],[-9.0014,-7.6047,210.0889,8.953],[-6.9597,-9.9668,232.1439,7.689],[-4.158,-11.3388,255.5556,7.9249],[-1.0401,-11.54,276.3184,9.8432],[1.9944,-10.7622,291.2565,15.9669],[4.8232,-9.4011,299.1185,33.3358],[7.487,-7.7385,305.4089,20.5392],[9.8516,-5.6798,317.9303,11.1283],[11.5531,-3.0562,337.0486,8.3352]],
   [[5.1462,-0.0444,6.6358,1.4452],[7.6795,-0.0444,7.6795,2.0623],[3.877,3.0197,5.3666,4.5094],[0.8128,4.2889,-0.6768,5.7786],[0.8128,6.8223,-1.2938,6.8223],[-2.2513,3.0197,-4.358,3.0197],[-4.0426,4.8111,-5.5323,6.3007],[-3.5205,-0.0444,-5.0101,-1.534],[-6.0538,-0.0444,-6.0538,-2.1511],[-2.2513,-3.1085,-2.2513,-5.2152],[-4.0426,-4.8999,-5.5323,-6.3895],[0.8128,-4.3777,2.3025,-5.8674],[0.8128,-6.9111,2.9195,-6.9111],[3.877,-3.1085,5.3666,-4.5982]]]
];

module nc_pitch(kind, prm) {
    m = gear_module;
    if (kind == "e")
        polygon([ for (i = [0 : 239]) let (t = i * 360 / 240)
                  [ prm[0] * m * cos(t), prm[1] * m * sin(t) ] ]);
    else
        polygon([ for (i = [0 : 239])
                  let (t = i * 360 / 240,
                       r = prm[0] * m * (1 + prm[1] * cos(prm[2] * t)))
                  [ r * cos(t), r * sin(t) ] ]);
}

function nc_tooth_pts(rho, bl) =
    let (ne = 2 * rho / gear_module,
         rb = rho * cos(pressure_angle),
         ra = rho + gear_module,
         rr = rho - gear_module * (1 + tip_clearance),
         rs = max(rb, rr) + 0.02,
         ht = 90 / ne - (bl / 2) / rho * 180 / PI,
         uc = rs > rr + 0.03)
    concat(
        [ pol(rr, -flank(rs, rb, ht) - 1.5) ],
        uc ? [ pol(rr, -flank(rs, rb, ht)) ] : [],
        [ for (j = [0 : 8]) let (r = rs + (ra - rs) * j / 8)
            pol(r, -flank(r, rb, ht)) ],
        [ for (j = [8 : -1 : 0]) let (r = rs + (ra - rs) * j / 8)
            pol(r, flank(r, rb, ht)) ],
        uc ? [ pol(rr, flank(rs, rb, ht)) ] : [],
        [ pol(rr, flank(rs, rb, ht) + 1.5) ]
    );

// The hole tables were laid out on a 2.5333 module grid, which at
// gear_module 1.5 is 3.8 mm apart: closer than two countersinks fit. Walk
// the table from its last entry back, keeping a hole only when it clears
// everything kept so far, then restore the table's order so the numbering
// still counts outward. Each ray is listed inner to outer, so working
// backwards is what saves the outermost hole of a crowded ray. Widen
// gear_module or narrow the bore and nothing is dropped.
function rev(v) = [ for (i = [len(v) - 1 : -1 : 0]) v[i] ];

function crowded(h, kept) =
    len([ for (k = kept)
            if (gear_module * norm([k[0] - h[0], k[1] - h[1]]) < pen_pitch_min())
                1 ]) > 0;

function thin(hs, i = 0, kept = []) =
    i >= len(hs) ? kept
                 : thin(hs, i + 1,
                        crowded(hs[i], kept) ? kept : concat(kept, [hs[i]]));

function nc_holes(sh) = rev(thin(rev(sh[6])));

module nc_wheel(sh) {
    m     = gear_module;
    holes = nc_holes(sh);
    difference() {
        linear_extrude(wheel_thickness)
            union() {
                offset(delta = -m * (1 + tip_clearance)) nc_pitch(sh[1], sh[2]);
                for (t = sh[5])
                    let (rho = t[3] * m, na = t[2])
                        translate([ t[0] * m - rho * cos(na),
                                    t[1] * m - rho * sin(na) ])
                            rotate(na) polygon(nc_tooth_pts(rho, backlash));
            }
        for (h = holes)
            translate([ h[0] * m, h[1] * m ]) pen_bore();
        if (label_wheels)
            translate([ 0, 0, wheel_thickness - label_depth ])
                linear_extrude(label_depth + 0.1) {
                    translate([ sh[4][0] * m, sh[4][1] * m ])
                        text(str(sh[3]), size = 4.5,
                             halign = "center", valign = "center");
                    if (label_holes)
                        for (i = [0 : len(holes) - 1])
                            translate([ holes[i][2] * m, holes[i][3] * m ])
                                text(str(i + 1), size = hole_label_h,
                                     halign = "center", valign = "center");
                }
    }
}

function nc_by_name(nm, i = 0) =
    i >= len(nc_shapes) ? undef
    : nc_shapes[i][0] == nm ? nc_shapes[i] : nc_by_name(nm, i + 1);

// Extents of a non-circular wheel, derived the same way tip_r() is derived
// for a circular one. The pitch coordinates in nc_shapes are in module
// units, and each tooth stands one module proud of the pitch curve.
function nc_rx(sh) = gear_module * (max([ for (t = sh[5]) abs(t[0]) ]) + 1);
function nc_ry(sh) = gear_module * (max([ for (t = sh[5]) abs(t[1]) ]) + 1);

// Same cumulative spacing as row_x() uses for the circular wheels, so the
// shapes sit a layout_gap apart whatever their size.
function nc_x(i) =
    i == 0 ? nc_rx(nc_shapes[0])
           : nc_x(i - 1) + nc_rx(nc_shapes[i - 1]) + layout_gap + nc_rx(nc_shapes[i]);
function nc_w() = nc_x(len(nc_shapes) - 1) + nc_rx(nc_shapes[len(nc_shapes) - 1]);
function nc_h() = max([ for (s = nc_shapes) nc_ry(s) ]);

module nc_wheels() {
    for (i = [0 : len(nc_shapes) - 1])
        translate([ nc_x(i), 0 ]) nc_wheel(nc_shapes[i]);
}

// ------------------------------------------------------------
// layout
// ------------------------------------------------------------

function row_of(k) =
    [ for (j = [0 : layout_cols - 1])
        if (k * layout_cols + j < len(wheel_teeth)) wheel_teeth[k * layout_cols + j] ];
function row_r(k)  = max([ for (t = row_of(k)) tip_r(t) ]);
function row_x(row, i) =
    i == 0 ? tip_r(row[0])
           : row_x(row, i - 1) + tip_r(row[i - 1]) + layout_gap + tip_r(row[i]);
function row_y(k) =
    k == 0 ? -row_r(0) : row_y(k - 1) - row_r(k - 1) - layout_gap - row_r(k);
// right-hand edge of a row, so anything placed beside it can clear it
function row_end_x(k) =
    let (row = row_of(k))
        row_x(row, len(row) - 1) + tip_r(row[len(row) - 1]);

module wheels() {
    rows = ceil(len(wheel_teeth) / layout_cols);
    for (k = [0 : rows - 1]) {
        row = row_of(k);
        for (i = [0 : len(row) - 1])
            translate([row_x(row, i), row_y(k)]) wheel(row[i]);
    }
}

function ring_outer_r() =
    ring_style == "classic" ? tip_r(ring_outer_teeth)
                            : int_root_r(ring_inner_teeth) + flange_width;

// ------------------------------------------------------------
// fit test: a wedge of the main ring plus the smallest wheel
// ------------------------------------------------------------

module pie(a1, a2, r) {
    polygon(concat([[0, 0]], [ for (t = [a1 : 2 : a2]) pol(r, t) ], [pol(r, a2)]));
}

module fit_test() {
    t = wheel_teeth[0];
    intersection() {
        ring();
        linear_extrude(ring_thickness + lip_height + 1)
            pie(-35, 35, ring_outer_r() + 1);
    }
    translate([-tip_r(t) - layout_gap, 0]) wheel(t);
}

// ------------------------------------------------------------
// render
// ------------------------------------------------------------

echo(str("main ring outer diameter : ", 2 * ring_outer_r(), " mm"));
echo(str("main ring bore diameter  : ",
         2 * gear_module * (ring_inner_teeth / 2 - 1), " mm"));
echo(str("outer ring diameter      : ", 2 * tip_r(ring_outer_teeth), " mm"));
echo(str("tooth height             : ", gear_module * (2 + tip_clearance), " mm"));
echo(str("pen bore / countersink   : ", pen_hole_d, " / ",
         pen_hole_d + 2 * pen_cs_rim, " mm"));

// Two funnels that run into each other leave a groove the pen can wander
// along, which spoils the drawing rather than just looking wrong. The
// spiral spaces itself and the shape tables are thinned, but both depend
// on numbers above, so prove it on every render instead of trusting it.
function min_pitch(pts) =
    len(pts) < 2 ? 1e9
    : min([ for (i = [0 : len(pts) - 1], j = [0 : len(pts) - 1])
                if (i < j) norm(pts[i] - pts[j]) ]);

function wheel_hole_pts(n) =
    [ for (i = [0 : pen_count(n) - 1]) pol(pen_radius(n, i), i * GOLDEN) ];
function nc_hole_pts(sh) = [ for (h = nc_holes(sh)) gear_module * [h[0], h[1]] ];

for (t = wheel_teeth)
    assert(min_pitch(wheel_hole_pts(t)) >= pen_pitch_min(),
           str(t, "T: pen holes are closer than their countersinks are wide"));
for (s = nc_shapes)
    assert(min_pitch(nc_hole_pts(s)) >= pen_pitch_min(),
           str(s[0], ": pen holes still crowd after thinning"));

if (is_num(part))              wheel(part);
else if (part == "ring")       ring();
else if (part == "outer_ring") ring_outer();
else if (part == "wheels")     wheels();
else if (part == "shapes")     nc_wheels();
else if (is_string(part) && !is_undef(nc_by_name(part))) nc_wheel(nc_by_name(part));
else if (part == "fit_test")   fit_test();
else {
    // The shapes continue the first wheel row, to the right of it and at its
    // centreline. Everything here is derived, so changing gear_module,
    // wheel_teeth or layout_cols re-packs the sheet without anything colliding.
    wheels_dy = -ring_outer_r() - layout_gap;
    ring();
    translate([2 * ring_outer_r() + 10, 0]) ring_outer();
    translate([0, wheels_dy]) wheels();
    translate([row_end_x(0) + layout_gap, wheels_dy + row_y(0)]) nc_wheels();
}
