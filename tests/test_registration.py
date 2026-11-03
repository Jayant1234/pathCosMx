#!/usr/bin/env python3
"""Registration against a fake H&E with a known transform (tests/make_fake_he.py).

Checks: auto registration places every cell within TOL_UM of where the known transform puts it
(reported: median / p95 / max error in um and in H&E px); anchor mode with 4 exact points recovers it
to < 0.5 um. Then the hard cases (tests/hard_fake_he.py: serial-section rendering, holes, debris, crops, any
rotation, nonlinear warps), with the file's mpp and without: each must EITHER come within HARD_TOL_UM (p95) of
the best global affine OR say status "uncertain"; "confident" and wrong is a failure. Exit non-zero on failure.

Usage: python tests/test_registration.py [--pack demo_pack] [--work <dir>] [--rot 90 --scale 0.8]
                                         [--hard all|none|case,case]
"""
import argparse
import json
import sys
import tempfile
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

TOL_UM = 10.0
HARD_TOL_UM = 5.0       # p95 above the affine floor


def errors_um(A, truth, x, y):
    from server.register import apply
    ex, ey = apply(A, x, y)
    tx, ty = apply(truth["mm_to_he_px"], x, y)
    return np.hypot(ex - tx, ey - ty) * truth["um_per_px"]


def stats(e, um_px):
    return (f"median {np.median(e):.2f} um, p95 {np.percentile(e, 95):.2f} um, max {e.max():.2f} um "
            f"(= {np.median(e) / um_px:.2f} / {np.percentile(e, 95) / um_px:.2f} / {e.max() / um_px:.2f} H&E px)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pack", default=str(ROOT / "demo_pack"))
    ap.add_argument("--work", default=None)
    ap.add_argument("--rot", type=float, default=90)
    ap.add_argument("--scale", type=float, default=0.8)
    ap.add_argument("--no-flip-x", action="store_true")
    ap.add_argument("--hard", default="all", help="hard cases to run: all, none or a comma list")
    a = ap.parse_args()
    from make_fake_he import make
    from pack.cells_io import read_cells
    from server import he, register

    pack = Path(a.pack)
    if not (pack / "manifest.json").exists():
        raise SystemExit(f"no pack at {pack}: run python pack/make_demo_pack.py --out {pack}")
    work = Path(a.work or tempfile.mkdtemp(prefix="pathqc_reg_"))
    f, truth = make(pack, work / "fake_he.tif", a.rot, not a.no_flip_x, a.scale)
    cells = read_cells(pack, columns=["x_mm", "y_mm"])
    x, y = cells.x_mm.values.astype(float), cells.y_mm.values.astype(float)
    standin_A = json.loads((pack / "transform.json").read_text())["mm_to_px"]
    ok = True

    slide, reader = he.open_slide(f)
    info = he.slide_info(slide, reader, f)
    print(f"opened {info['name']} with {reader}: {info['width']}x{info['height']}, mpp {info['mpp']}")
    for mpp in (info["mpp"], None):              # with the file's mpp, and as if the file had none
        res = register.auto(slide, x, y, standin_A, mpp=mpp, log=lambda m: None)
        e = errors_um(res["mm_to_px"], truth, x, y)
        good = np.percentile(e, 95) < TOL_UM and e.max() < 2 * TOL_UM
        ok &= bool(good)
        print(f"{'PASS' if good else 'FAIL'} auto (mpp {'given' if mpp else 'unknown'}): {stats(e, truth['um_per_px'])}; "
              f"{res['summary']}; {res['status']} {res['reason']}; recovered {res['um_per_px']:.4f} um/px (true {truth['um_per_px']:.4f}); "
              f"{res['seconds']} s")

    # the pyvips fallback reader (plain TIFFs OpenSlide refuses): same registration, same Deep Zoom tiles
    vs = he.VipsSlide(f)
    res = register.auto(vs, x, y, standin_A, mpp=info["mpp"], log=lambda m: None)
    e = errors_um(res["mm_to_px"], truth, x, y)
    from openslide.deepzoom import DeepZoomGenerator
    dz_o = DeepZoomGenerator(slide, 254, 1, limit_bounds=False)
    dz_v = DeepZoomGenerator(vs, 254, 1, limit_bounds=False)
    L = dz_o.level_count - 3
    diff = np.abs(np.asarray(dz_o.get_tile(L, (1, 1)), float) - np.asarray(dz_v.get_tile(L, (1, 1)), float)).mean()
    good = np.percentile(e, 95) < TOL_UM and diff < 8
    ok &= bool(good)
    print(f"{'PASS' if good else 'FAIL'} pyvips fallback reader: auto {stats(e, truth['um_per_px'])}; "
          f"DZI tile level {L} mean |diff| vs OpenSlide {diff:.2f} grey levels")

    rng = np.random.default_rng(1)
    idx = [int(np.argmin(x + y)), int(np.argmax(x + y)), int(np.argmin(x - y)), int(np.argmax(x - y))]
    from server.register import apply
    hx, hy = apply(truth["mm_to_he_px"], x[idx], y[idx])
    pairs = [{"mm": [x[i], y[i]], "he": [hx[k], hy[k]]} for k, i in enumerate(idx)]
    res = register.anchors(pairs, standin_A)
    e = errors_um(res["mm_to_px"], truth, x, y)
    good = e.max() < 0.5
    ok &= bool(good)
    print(f"{'PASS' if good else 'FAIL'} anchors (4 exact points): {stats(e, truth['um_per_px'])}; {res['summary']}")
    noisy = [{"mm": p["mm"], "he": list(np.add(p["he"], rng.normal(0, 3, 2)))} for p in pairs]
    res = register.anchors(noisy, standin_A)
    e = errors_um(res["mm_to_px"], truth, x, y)
    print(f"info anchors (4 points, 3 px click noise): {stats(e, truth['um_per_px'])}; residuals {res['residual_um']} um")
    if a.hard != "none":
        ok &= hard_cases(pack, work, x, y, standin_A, a.hard)
    print("ALL PASS" if ok else "FAILED")
    return 0 if ok else 1


def hard_cases(pack, work, x, y, standin_A, which):
    from hard_fake_he import CASES, make, floor
    from server import he, register
    names = list(CASES) if which == "all" else which.split(",")
    ok, n_bad = True, 0
    print(f"hard cases: pass = p95 <= affine floor + {HARD_TOL_UM} um, or status uncertain")
    print(f"  {'case':14s} {'mpp':5s} {'floor p95':>9s} {'p95':>9s}  {'r':>5s} {'margin':>6s}  status     result")
    for name in names:
        f, truth = make(pack, work / "hard", name, x, y)
        slide, _ = he.open_slide(f)
        W, H = slide.dimensions
        tx, ty = truth(x, y)
        inside = (tx >= 0) & (tx < W) & (ty >= 0) & (ty < H)
        um = CASES[name]["um"]

        def p95(A):
            ex, ey = register.apply(A, x, y)
            return float(np.percentile(np.hypot(ex - tx, ey - ty)[inside] * um, 95))
        fl = p95(floor(truth, x, y, inside))
        for mpp in (um, None):
            res = register.auto(slide, x, y, standin_A, mpp=mpp, log=lambda m: None)
            e = p95(res["mm_to_px"])
            good = e <= fl + HARD_TOL_UM or res["status"] == "uncertain"
            bad = res["status"] == "confident" and e > fl + 50
            ok &= bool(good)
            n_bad += bad
            print(f"  {name:14s} {'given' if mpp else 'none':5s} {fl:9.1f} {e:9.1f}  {res['score']:5.2f} {res['margin']:6.2f}  "
                  f"{res['status']:10s} {'PASS' if good else 'FAIL'}{' CONFIDENT AND WRONG' if bad else ''}"
                  f"{'  (' + res['reason'] + ')' if res['reason'] else ''}", flush=True)
        slide.close()
    print(f"{'PASS' if ok else 'FAIL'} hard cases: {n_bad} confident and wrong (> 50 um above the floor)")
    return ok


if __name__ == "__main__":
    sys.exit(main())
