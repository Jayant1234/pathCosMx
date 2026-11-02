#!/usr/bin/env python3
"""Render a stand-in slide image from cell positions until the real H&E arrives.

The image uses positions only, never labels: a pink tissue mask from local cell density and one
purple "nucleus" per cell. It lets the viewer, the overlays and the export run end to end.
It is NOT histology; the app shows a banner while it is in use.

Writes into the pack:
  image/standin.tif          full-resolution base raster (tiled TIFF, used by the slide export)
  image/slide.dzi + _files/  Deep Zoom tiles for the viewer
  transform.json             affine slide-mm -> image-px for this image

Usage: python make_standin.py --pack packs/hnsc_slide1 [--um-per-px 1.0]
"""
import argparse
import json
from pathlib import Path

import numpy as np
import pyvips
from PIL import Image, ImageDraw

try:
    from .cells_io import read_cells
except ImportError:            # run as a script
    from cells_io import read_cells

Image.MAX_IMAGE_PIXELS = None

MARGIN_MM = 0.3
GLASS = (246, 242, 244)
STROMA = (234, 182, 202)
NUCLEUS = (92, 62, 140)


def build(pack, upp=1.0, log=print):
    """Render the stand-in into <pack>/image and write <pack>/transform.json."""
    pack = Path(pack)
    cells = read_cells(pack, columns=["x_mm", "y_mm"])
    x0 = cells.x_mm.min() - MARGIN_MM
    y1 = cells.y_mm.max() + MARGIN_MM
    w = int(np.ceil((cells.x_mm.max() + MARGIN_MM - x0) * 1000 / upp))
    h = int(np.ceil((y1 - (cells.y_mm.min() - MARGIN_MM)) * 1000 / upp))
    # px = a*x_mm + c ; py = e*y_mm + f  (image y grows downward)
    a, c = 1000 / upp, -x0 * 1000 / upp
    e, f = -1000 / upp, y1 * 1000 / upp
    px = a * cells.x_mm.values + c
    py = e * cells.y_mm.values + f
    log(f"canvas {w} x {h} px at {upp} um/px")

    # tissue mask from density on a 10 um grid, smoothed, upsampled
    g = 10.0 / upp
    gw, gh = int(np.ceil(w / g)), int(np.ceil(h / g))
    dens = np.zeros((gh, gw), np.float32)
    np.add.at(dens, (np.clip((py / g).astype(int), 0, gh - 1), np.clip((px / g).astype(int), 0, gw - 1)), 1)
    dv = pyvips.Image.new_from_memory(dens.tobytes(), gw, gh, 1, "float").gaussblur(2.5) / 0.25
    dv = dv.resize(w / gw, vscale=h / gh, kernel="linear")
    if dv.width < w or dv.height < h:            # rounding can leave the upsampled grid a pixel short
        dv = dv.embed(0, 0, max(w, dv.width), max(h, dv.height), extend="copy")
    tissue = np.ndarray(buffer=dv.write_to_memory(), dtype=np.float32, shape=(dv.height, dv.width))
    tissue = np.clip(tissue[:h, :w], 0, 1)

    img = np.empty((h, w, 3), np.uint8)
    for ch in range(3):
        img[..., ch] = (GLASS[ch] + (STROMA[ch] - GLASS[ch]) * tissue).astype(np.uint8)
    del tissue
    rng = np.random.default_rng(0)
    rad = np.maximum(1, np.round(rng.normal(3.2, 0.6, len(px)) / upp)).astype(int)
    shade = rng.integers(-18, 18, len(px))
    canvas = Image.fromarray(img)
    del img
    draw = ImageDraw.Draw(canvas)
    for x, y, r, s in zip(px, py, rad, shade):
        draw.ellipse((x - r, y - r, x + r, y + r), fill=tuple(int(v + s) for v in NUCLEUS))
    img = np.asarray(canvas)

    out = pack / "image"
    out.mkdir(exist_ok=True)
    vim = pyvips.Image.new_from_memory(img.data, w, h, 3, "uchar")
    vim.tiffsave(str(out / "standin.tif"), tile=True, pyramid=True, compression="jpeg", Q=90, bigtiff=True)
    vim.dzsave(str(out / "slide"), tile_size=254, overlap=1, suffix=".jpg[Q=85]")

    transform = {
        "kind": "standin",
        "description": "stand-in image rendered from cell positions (not H&E)",
        "um_per_px": upp,
        "image": {"width": w, "height": h, "dzi": "image/slide.dzi", "base": "image/standin.tif"},
        "mm_to_px": [[a, 0.0, c], [0.0, e, f]],
        "sections": {},
    }
    (pack / "transform.json").write_text(json.dumps(transform, indent=2))
    log(f"wrote {out}/slide.dzi and {pack}/transform.json")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pack", required=True)
    ap.add_argument("--um-per-px", type=float, default=1.0)
    args = ap.parse_args()
    build(args.pack, args.um_per_px)


if __name__ == "__main__":
    main()
