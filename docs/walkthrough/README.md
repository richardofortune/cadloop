# The walkthrough

The GIF at the top of the project README is filmed from `index.html` in this
directory, using [nolan](https://github.com/richardofortune/nolan).

```console
cd docs/walkthrough
nolan cadloop.screenplay.json --cut=hero --style=cadloop.style.json --out=out
```

| File | Holds |
| --- | --- |
| `index.html` | the page being filmed, with the artifacts it displays |
| `cadloop.screenplay.json` | what the walkthrough shows and says |
| `cadloop.style.json` | how it looks: captions, cursor, encoding |
| `assets/` | real renders and real gear geometry |
| `out/` | generated media, ignored except the README hero |

`--cut=hero` is the 26 second version used in the README. `--cut=full` runs
through all six steps and lasts about 48 seconds.

## Where the numbers come from

Everything on the page is real output, produced by this repo on a machine with
OpenSCAD and Creality Print installed. Nothing is illustrative.

- The renders in `assets/` are OpenSCAD PNG exports of `models/spirograph.scad`.
- `4 990` triangles, `38.993 mm` and `3 796.322 mm³` come from `measure`.
- The bed numbers come from `check_bed_fit` against Creality Print's stock
  `Creality K1 0.4 nozzle.json`, and `469 of 473` is a sweep over every machine
  profile that ships with it.
- The two gear drawings are Shapely geometry from `cadloop.gearcheck`, exported
  straight to SVG. The red is the actual intersection of the two solids at the
  worst point of the circuit.

The failing wheel is an ellipse of 30 by 10 mm. Its pitch curve reaches a radius
of curvature of 88.85 mm, and the ring it has to roll inside is 72.00 mm, so its
teeth cannot clear. `3.934 mm²` is the measured overlap. The passing wheel, an
ellipse of 28 by 12, peaks at 65.94 mm and clears everywhere.

Neither wheel ships in the model. They exist to show the check working, and both
of them render as clean manifolds, which is the whole point.

## Keeping it honest

`nolan verify cadloop.screenplay.json` resolves every target without filming and
exits non-zero if the page moved under the screenplay. Worth running after any
edit to `index.html`.
