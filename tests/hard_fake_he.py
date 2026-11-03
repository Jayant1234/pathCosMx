#!/usr/bin/env python3
"""Hard synthetic H&E for registration tests: rendered from the pack's cell positions (not the stand-in), like a
serial section (30 % of cells dropped, 6 um jitter), H&E-like optical density with a saturating haematoxylin and a
stain-variation field, blur + noise, optionally a torn-out tissue piece, debris + a pen mark, a 10 % crop, any
rotation and a smooth nonlinear warp (sum of sinusoids, amplitude in um). Synthetic only (demo pack).

truth(x_mm, y_mm) -> H&E level-0 px is exact; floor() is the best global affine fit to it (what any affine can reach).

Usage: python tests/hard_fake_he.py --pack demo_pack --out <dir> --case hole_only
"""
import argparse
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

CASES = {
    # rot: deg (y down, clockwise); mirror; um: um/px; warp: amplitude um; crop: tissue fraction cut off the left
    "easy_rot90":    dict(rot=90, mirror=True, um=1.25, warp=0, crop=0.0, hole=False, debris=False),
    "warp30":        dict(rot=90, mirror=True, um=1.25, warp=30, crop=0.0, hole=False, debris=False),
    "full_hard":     dict(rot=93, mirror=True, um=1.25, warp=40, crop=0.10, hole=True, debris=True),
    "full_hard_r0":  dict(rot=-4, mirror=False, um=0.9, warp=40, crop=0.10, hole=True, debris=True),
    "nowarp_hard":   dict(rot=93, mirror=True, um=1.25, warp=0, crop=0.10, hole=True, debris=True),
    "crop_only":     dict(rot=90, mirror=True, um=1.25, warp=0, crop=0.10, hole=False, debris=False),
    "hole_only":     dict(rot=90, mirror=True, um=1.25, warp=0, crop=0.0, hole=True, debris=False),
    "debris_only":   dict(rot=90, mirror=True, um=1.25, warp=0, crop=0.0, hole=False, debris=True),
    "rot15":         dict(rot=15, mirror=False, um=1.25, warp=30, crop=0.10, hole=True, debris=True),
    "warp50_rot180": dict(rot=182, mirror=False, um=1.1, warp=50, crop=0.10, hole=True, debris=True),
}


def _map(c, x, y, cx, cy, W, H):
    """mm -> H&E px: centred, y down, (mirror), rotate, scale, + smooth warp."""
    th = np.radians(c["rot"])
    k = 1000.0 / c["um"]
    xm, ym = (x - cx) * k, -(y - cy) * k
    if c["mirror"]:
        xm = -xm
    u = np.cos(th) * xm - np.sin(th) * ym
    v = np.sin(th) * xm + np.cos(th) * ym
    a = c["warp"] / c["um"]
    if a:
        L = 2500.0 / c["um"]                                    # warp wavelength ~2.5 mm
        u = u + a * np.sin(2 * np.pi * v / L + 0.7) + 0.5 * a * np.cos(2 * np.pi * u / (1.7 * L))
        v = v + a * np.cos(2 * np.pi * u / L + 1.3) + 0.5 * a * np.sin(2 * np.pi * v / (1.3 * L))
    return u + W / 2, v + H / 2


def make(pack, out_dir, name, x, y):
    """-> (tif path, truth(x_mm, y_mm) -> (px, py) on the written image)."""
    import cv2
    import pyvips
    c = CASES[name]
    rng = np.random.default_rng(7)
    cx, cy = (x.min() + x.max()) / 2, (y.min() + y.max()) / 2
    k = 1000.0 / c["um"]
    th = np.radians(c["rot"])
    ext = np.array([(x.max() - x.min()) * k, (y.max() - y.min()) * k])
    R = np.abs(np.array([[np.cos(th), np.sin(th)], [np.sin(th), np.cos(th)]]))
    W, H = (R @ ext).astype(int) + 2 * int(0.4 * k)
    tx, ty = _map(c, x, y, cx, cy, W, H)
    keep = rng.random(x.size) > 0.3                                    # serial section
    jx = tx[keep] + rng.normal(0, 6 / c["um"], keep.sum())
    jy = ty[keep] + rng.normal(0, 6 / c["um"], keep.sum())
    dens = np.zeros((H, W), np.float32)
    np.add.at(dens, (np.clip(jy.astype(int), 0, H - 1), np.clip(jx.astype(int), 0, W - 1)), 1.0)
    nuc = cv2.GaussianBlur(dens, (0, 0), 3.0 / c["um"])
    nuc = nuc / (np.percentile(nuc[nuc > 0], 99) + 1e-6)
    tissue = cv2.GaussianBlur(dens, (0, 0), 40.0 / c["um"])
    tissue = np.clip(tissue / (0.3 * np.percentile(tissue[tissue > 0], 50)), 0, 1)
    tissue = np.clip(tissue + 0.15 * cv2.GaussianBlur(rng.random((H, W)).astype(np.float32), (0, 0), 30), 0, 1) * (tissue > 0.15)
    if c["hole"]:                                                       # tissue torn out in the busiest area
        yy, xx = np.mgrid[0:H, 0:W]
        py, px = np.unravel_index(np.argmax(cv2.GaussianBlur(dens, (0, 0), 200)), dens.shape)
        hole = ((xx - px - 300) / (0.12 * W)) ** 2 + ((yy - py) / (0.18 * H)) ** 2 < 1
        tissue[hole] = 0
        nuc[hole] = 0
    var = cv2.GaussianBlur(rng.normal(0, 1, (H // 50 + 1, W // 50 + 1)).astype(np.float32), (0, 0), 2)
    var = cv2.resize(var, (W, H)) * 0.6
    E = tissue * (0.35 + 0.15 * var)
    Hm = (1 - np.exp(-2.5 * nuc)) * (1.1 + 0.4 * var) * (tissue > 0)
    od_e, od_h = np.array([0.07, 0.99, 0.11]), np.array([0.65, 0.70, 0.29])    # Ruifrok vectors
    od = E[..., None] * od_e * 0.9 + Hm[..., None] * od_h * 0.9
    if c["debris"]:
        for _ in range(25):
            bx, by, r = rng.integers(0, W), rng.integers(0, H), rng.integers(5, 40)
            cv2.circle(od, (int(bx), int(by)), int(r), (0.6, 0.8, 0.4), -1)
        cv2.line(od, (int(0.05 * W), int(0.9 * H)), (int(0.4 * W), int(0.97 * H)), (0.2, 0.9, 1.2), 25)   # pen mark
    rgb = 245 * np.power(10, -od)
    rgb = cv2.GaussianBlur(rgb.astype(np.float32), (0, 0), 1.2) + rng.normal(0, 6, (H, W, 1))
    rgb = np.clip(rgb, 0, 255).astype(np.uint8)
    x0 = 0
    if c["crop"]:
        x0 = int(np.sort(tx)[int(c["crop"] * tx.size)])
        rgb = rgb[:, x0:]
    out = Path(out_dir) / f"{name}.tif"
    out.parent.mkdir(parents=True, exist_ok=True)
    vim = pyvips.Image.new_from_memory(np.ascontiguousarray(rgb).data, rgb.shape[1], H, 3, "uchar")
    vim.tiffsave(str(out), tile=True, tile_width=256, tile_height=256, pyramid=True, compression="jpeg", Q=85,
                 xres=1000.0 / c["um"], yres=1000.0 / c["um"])

    def truth(xx, yy):
        u, v = _map(c, xx, yy, cx, cy, W, H)
        return u - x0, v
    return out, truth


def floor(truth, x, y, inside):
    """Best global affine mm -> px (least squares over the cells inside the image)."""
    tx, ty = truth(x, y)
    M = np.column_stack([x, y, np.ones_like(x)])
    return np.linalg.lstsq(M[inside], np.column_stack([tx, ty])[inside], rcond=None)[0].T


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pack", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--case", choices=list(CASES), required=True)
    a = ap.parse_args()
    from pack.cells_io import read_cells
    cells = read_cells(Path(a.pack), columns=["x_mm", "y_mm"])
    f, _ = make(a.pack, a.out, a.case, cells.x_mm.values.astype(float), cells.y_mm.values.astype(float))
    print(f)


if __name__ == "__main__":
    main()
