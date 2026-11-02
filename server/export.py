"""Save outputs: the corrected labels file, the base-pick log, GeoJSON, and the whole-slide image."""
import json
import shutil
import tempfile
import time
from pathlib import Path

import numpy as np
import pandas as pd
import pyvips
from PIL import Image, ImageDraw, ImageFont

from .session import TAG_NAMES

BAND = 2048
Image.MAX_IMAGE_PIXELS = None
try:
    FONT = ImageFont.load_default(size=24)
except TypeError:          # Pillow < 10.1
    FONT = ImageFont.load_default()


def _hex(c):
    c = c.lstrip("#")
    return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))


def corrected_table(pack, sess):
    final, tag, opi = sess.replay(pack.src)
    root = sess.resolve_table()
    names = {v["code"]: v["name"] for v in sess.vocab}
    ops = sess.active_ops()
    op_id = np.array([ops[i]["id"] if i >= 0 else "" for i in opi], dtype=object)
    op_ts = np.array([ops[i]["ts"] if i >= 0 else "" for i in opi], dtype=object)

    comment_ids = [[] for _ in range(pack.n)]
    for c in sess.comments:
        for i in pack.cells_of_geometry(c["geometry"]):
            comment_ids[i].append(c["id"])

    df = pd.DataFrame({
        "cell_id": pack.cell_ids,
        "section": pack.cells.section.values,
        "split": pack.cells.split.values,
        "x_mm": pack.x_mm.round(6), "y_mm": pack.y_mm.round(6),
        "image_px": pack.px.round(1), "image_py": pack.py.round(1),
        "final_label": [names.get(int(root[c]), "") for c in final],
        "final_label_unmerged": [names.get(int(c), "") for c in final],
        "source": [TAG_NAMES[int(t)] for t in tag],
        "op_id": op_id, "op_ts": op_ts,
        "reviewer": np.where(opi >= 0, sess.reviewer, ""),
        "comment_ids": [";".join(c) for c in comment_ids],
        "bill_eugene_code": pack.cells.bill_eugene_code.values,
        "vestrum": pack.cells.vestrum.values,
        "claude_code": pack.cells.claude_code.values,
    })
    return df, final, root


def base_picks(pack, sess):
    rows = []
    for o in sess.active_ops():
        idx = np.asarray(o["cells"], dtype=np.int64)
        secs = pack.cells.section.values[idx] if idx.size else []
        rows.append({
            "op_id": o["id"], "ts": o["ts"], "op": o["op"],
            "chosen_as_base": o.get("source", ""), "relabel_to": o.get("to_name", ""),
            "filter": o.get("filter_name", ""), "n_cells": int(idx.size),
            "sections": ";".join(sorted(set(secs))),
            "dev_section": bool(set(secs) & set(pack.manifest["splits"]["dev"])),
        })
    return pd.DataFrame(rows, columns=["op_id", "ts", "op", "chosen_as_base", "relabel_to", "filter",
                                       "n_cells", "sections", "dev_section"])


def _feature(item, pack, kind):
    g = item["geometry"]
    px = pack.geom_mm_to_px(g["mm"])
    if g["type"] == "Point":
        geometry = {"type": "Point", "coordinates": px[0]}
    else:
        geometry = {"type": "Polygon", "coordinates": [px + [px[0]]]}
    props = {k: v for k, v in item.items() if k != "geometry"}
    props["objectType"] = "annotation"
    props["coordinates_mm"] = g["mm"]
    props["classification"] = {"name": item.get("name") or item.get("category") or kind}
    return {"type": "Feature", "geometry": geometry, "properties": props}


def write_results(pack, sess, out_root: Path):
    out = out_root / f"{sess.dir.name}_{time.strftime('%Y%m%d_%H%M%S')}"
    out.mkdir(parents=True)
    df, final, root = corrected_table(pack, sess)
    df.to_csv(out / f"{sess.dir.name}_labels.tsv", sep="\t", index=False)
    picks = base_picks(pack, sess)
    picks.to_csv(out / "base_picks.tsv", sep="\t", index=False)
    for kind, items in (("comments", sess.comments), ("annotations", sess.annotations)):
        fc = {"type": "FeatureCollection", "features": [_feature(i, pack, kind) for i in items]}
        (out / f"{kind}.geojson").write_text(json.dumps(fc, indent=1))
    for name in ("ops.jsonl", "vocab.json", "vocab_log.json"):
        if (sess.dir / name).exists():
            shutil.copy(sess.dir / name, out / name)
    summary = {
        "reviewer": sess.reviewer, "label_layer": f"{sess.dir.name}_labels",
        "saved": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "pack": pack.manifest["pack"], "image": pack.transform.get("kind"),
        "n_cells": int(pack.n),
        "cells_by_source": df.source.value_counts().to_dict(),
        "base_picks_by_method": picks[picks.op == "base"].chosen_as_base.value_counts().to_dict(),
        "cells_by_base_method": picks[picks.op == "base"].groupby("chosen_as_base").n_cells.sum().to_dict(),
        "n_comments": len(sess.comments), "n_annotations": len(sess.annotations),
        "changed_from_bill_eugene_code": int((root[final] != root[pack.src["bill_eugene_code"]]).sum()),
    }
    (out / "summary.json").write_text(json.dumps(summary, indent=2))
    return out, final, root


def slide_image(pack, sess, final, root, out_file: Path, status: dict, max_dim=None):
    """Paint corrected labels, annotations and comment pins onto the base image, band by band.

    The base is streamed from disk and each painted band is written as its own compressed file,
    so memory stays at about one band whatever the slide size.
    """
    base = pyvips.Image.new_from_file(str(pack.path / pack.transform["image"]["base"]))
    if base.bands == 4:
        base = base.flatten()
    scale = 1.0
    if max_dim and max(base.width, base.height) > max_dim:
        scale = max_dim / max(base.width, base.height)
        base = base.resize(scale)
    W, H = base.width, base.height
    colour = {v["code"]: _hex(v["color"]) for v in sess.vocab}
    rgb = np.array([colour.get(int(root[c]), (128, 128, 128)) for c in range(256)], np.uint8)
    px, py = pack.px * scale, pack.py * scale
    um = pack.transform.get("um_per_px", 1.0) / scale
    r = max(2, int(round(3.5 / um)))
    order = np.argsort(py)
    pys = py[order]

    shapes = []
    for a in sess.annotations:
        shapes.append(("ann", np.array(pack.geom_mm_to_px(a["geometry"]["mm"])) * scale, a.get("name", "")))
    for c in sess.comments:
        shapes.append(("com", np.array(pack.geom_mm_to_px(c["geometry"]["mm"])) * scale, c.get("category", "")))

    tmp = Path(tempfile.mkdtemp(prefix="pathqc_band_", dir=out_file.parent))
    bands = []
    try:
        for k, y0 in enumerate(range(0, H, BAND)):
            h = min(BAND, H - y0)
            arr = np.ndarray(buffer=base.crop(0, y0, W, h).write_to_memory(), dtype=np.uint8,
                             shape=(h, W, base.bands))[..., :3].copy()
            over = Image.fromarray(arr)
            draw = ImageDraw.Draw(over)
            lo, hi = np.searchsorted(pys, y0 - r), np.searchsorted(pys, y0 + h + r)
            for i in order[lo:hi]:
                x, y = float(px[i]), float(py[i] - y0)
                draw.ellipse((x - r, y - r, x + r, y + r), fill=tuple(int(v) for v in rgb[final[i]]))
            # labels at 80% over the slide
            arr = (0.8 * np.asarray(over, np.float32) + 0.2 * arr).astype(np.uint8)
            img = Image.fromarray(arr)
            draw = ImageDraw.Draw(img)
            for kind, pts, label in shapes:
                p = [(float(x), float(y - y0)) for x, y in pts]
                if len(p) == 1:
                    x, y = p[0]
                    draw.polygon([(x - 14, y - 28), (x + 14, y - 28), (x, y)], fill=(255, 200, 0), outline=(90, 67, 0))
                else:
                    draw.line(p + [p[0]], fill=(20, 20, 20) if kind == "ann" else (255, 200, 0), width=3)
                    if kind == "ann" and label:
                        draw.text((min(x for x, _ in p), min(y for _, y in p) - 26), label, fill=(20, 20, 20), font=FONT)
            arr = np.asarray(img)
            f = tmp / f"band_{k:05d}.tif"
            pyvips.Image.new_from_memory(np.ascontiguousarray(arr).data, W, h, 3, "uchar").tiffsave(
                str(f), compression="jpeg", Q=92, tile=True)
            bands.append(f)
            status["progress"] = round((y0 + h) / H, 3)
        # arrayjoin pads every band to the tallest one; crop the padding off the last band
        img = pyvips.Image.arrayjoin([pyvips.Image.new_from_file(str(b), access="sequential") for b in bands],
                                     across=1).crop(0, 0, W, H)
        img.tiffsave(str(out_file), tile=True, pyramid=True, compression="jpeg", Q=90, bigtiff=True,
                     xres=1000 / um, yres=1000 / um)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return out_file
