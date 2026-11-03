#!/usr/bin/env python3
"""Make a fake "H&E" from a pack's stand-in raster with a KNOWN transform, for registration tests.

Stand-in px -> fake px:  rotate 90 deg (clockwise on screen), mirror x, scale 0.8, translate (defaults).
The result is tinted H&E-like (eosin pink / haematoxylin purple, a little noise) and written as a pyramidal
tiled JPEG TIFF (pyvips) that OpenSlide reads as generic-tiff. Ground truth goes next to it:
<out>.truth.json  {"standin_px_to_he_px": 3x3, "mm_to_he_px": 2x3, "um_per_px": ...}

Usage: python tests/make_fake_he.py --pack demo_pack --out /tmp/fake_he.tif [--scale 0.8 --rot 90 --flip-x]
"""
import argparse
import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def known_transform(w, h, rot=90, flip_x=True, scale=0.8, pad=(500, 300)):
    """3x3 continuous-px matrix stand-in -> fake, and the fake canvas size."""
    th = np.radians(rot)
    R = np.array([[np.cos(th), -np.sin(th), 0], [np.sin(th), np.cos(th), 0], [0, 0, 1]])   # y down: +rot = clockwise
    F = np.diag([-1.0 if flip_x else 1.0, 1.0, 1.0])
    K = np.diag([scale, scale, 1.0]) @ F @ R
    corners = np.array([[0, 0, 1], [w, 0, 1], [0, h, 1], [w, h, 1]], float).T
    c = K @ corners
    K = np.array([[1, 0, pad[0] - c[0].min()], [0, 1, pad[1] - c[1].min()], [0, 0, 1]]) @ K
    c = K @ corners
    return K, int(np.ceil(c[0].max() + pad[0] * 1.7)), int(np.ceil(c[1].max() + pad[1] * 1.3))


def tint(rgb, seed=0):
    """Stand-in colours -> H&E-like: bright glass stays white, pink stroma -> eosin, purple nuclei -> haematoxylin."""
    g = rgb.astype(np.float32).mean(axis=2) / 255.0
    glass, eosin, haem = (np.array(c, np.float32) for c in ([244, 241, 246], [228, 150, 190], [75, 45, 125]))
    t1 = np.clip((0.955 - g) / (0.955 - 0.80), 0, 1)[..., None]           # glass -> stroma
    t2 = np.clip((0.80 - g) / (0.80 - 0.33), 0, 1)[..., None]            # stroma -> nucleus
    out = glass * (1 - t1) + eosin * t1
    out = out * (1 - t2) + haem * t2
    out += np.random.default_rng(seed).normal(0, 4, out.shape[:2]).astype(np.float32)[..., None]
    return np.clip(out, 0, 255).astype(np.uint8)


def make(pack, out, rot=90, flip_x=True, scale=0.8, log=print):
    import cv2
    import pyvips
    pack, out = Path(pack), Path(out)
    if not (pack / "image" / "standin.tif").exists():
        from pack.make_standin import build
        build(pack, log=log)
    tr = json.loads((pack / "transform.json").read_text())
    src = pyvips.Image.new_from_file(str(pack / tr["image"]["base"]))
    a = np.ndarray(buffer=src.write_to_memory(), dtype=np.uint8, shape=(src.height, src.width, src.bands))[..., :3]
    K, W, H = known_transform(src.width, src.height, rot, flip_x, scale)
    Sh = np.array([[1, 0, -0.5], [0, 1, -0.5], [0, 0, 1]])              # cv2 uses pixel-centre indices
    M = (Sh @ K @ np.linalg.inv(Sh))[:2]
    warped = cv2.warpAffine(np.ascontiguousarray(a), M, (W, H), flags=cv2.INTER_LINEAR,
                            borderMode=cv2.BORDER_CONSTANT, borderValue=(246, 242, 244))
    he = tint(warped)
    um = tr["um_per_px"] / scale
    vim = pyvips.Image.new_from_memory(np.ascontiguousarray(he).data, W, H, 3, "uchar")
    out.parent.mkdir(parents=True, exist_ok=True)
    vim.tiffsave(str(out), tile=True, tile_width=256, tile_height=256, pyramid=True, compression="jpeg", Q=88,
                 xres=1000.0 / um, yres=1000.0 / um, bigtiff=False)
    truth = {"standin_px_to_he_px": K.tolist(), "mm_to_he_px": (K @ np.vstack([tr["mm_to_px"], [0, 0, 1]]))[:2].tolist(),
             "um_per_px": um, "width": W, "height": H, "rot": rot, "flip_x": flip_x, "scale": scale}
    out.with_suffix(".truth.json").write_text(json.dumps(truth, indent=2))
    log(f"fake H&E {W}x{H} at {um:.3f} um/px -> {out}")
    return out, truth


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pack", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--rot", type=float, default=90)
    ap.add_argument("--no-flip-x", action="store_true")
    ap.add_argument("--scale", type=float, default=0.8)
    a = ap.parse_args()
    make(a.pack, a.out, a.rot, not a.no_flip_x, a.scale)


if __name__ == "__main__":
    main()
