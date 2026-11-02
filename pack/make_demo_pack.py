#!/usr/bin/env python3
"""Make a synthetic demo pack: two fake tissue sections, three label sources with made-up errors.

Contains no patient data, so it can live in a public repository and ship inside the app for testing.
A curved epithelial band (basal -> squamous -> keratin layers) lies over stroma with immune cells and
vessels. "Bill_eugene_code" is the layer truth; "Vestrum" adds 8% noise and widens Basal(ifn);
"Claude Code" adds 15% noise and swaps Squam1/Squam2 in the second section.

Usage: python make_demo_pack.py --out demo_pack
"""
import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

from cells_io import write_cells

from add_attributes import ATTRIBUTES
from build_pack import CANON, SOURCES, ari

STROMA = ["CAF1"] * 6 + ["Endoth"] * 2 + ["Tcells"] * 2 + ["TAMs"] * 2 + ["Bcells", "Mast", "Perictye", "DCs"]


def section(rng, name, x_off, n):
    """Cells in a 4 x 3 mm block; the epithelium follows a sine curve."""
    x = rng.uniform(0, 4, n)
    y = rng.uniform(0, 3, n)
    surface = 2.2 + 0.25 * np.sin(x * 1.7)          # top of the epithelium
    depth = surface - y                              # mm below the surface
    keep = depth > -0.02                             # no cells above the surface
    x, y, depth = x[keep], y[keep], depth[keep]
    lab = np.empty(len(x), dtype=object)
    lab[depth < 0.06] = "Keratin"
    lab[(depth >= 0.06) & (depth < 0.2)] = "Squam2"
    lab[(depth >= 0.2) & (depth < 0.33)] = "Squam1"
    lab[(depth >= 0.33) & (depth < 0.40)] = "Basal(core)"
    lab[(depth >= 0.40) & (depth < 0.45)] = "Basal(stem)"
    lab[(depth >= 0.45) & (depth < 0.48)] = "Basal(ifn)"
    st = depth >= 0.48
    lab[st] = rng.choice(STROMA, st.sum())
    lab[st & (x > 3.3) & (y < 0.8)] = "Muscle"
    return pd.DataFrame({"section": name, "x_mm": x + x_off, "y_mm": y, "truth": lab, "depth": depth})


def noisy(rng, truth, rate):
    out = truth.copy()
    flip = rng.random(len(out)) < rate
    out[flip] = rng.choice([n for n, _ in CANON if n != "Unknown"], flip.sum())
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--cells", type=int, default=24000)
    args = ap.parse_args()
    rng = np.random.default_rng(7)
    d = pd.concat([section(rng, "S1", 0.0, args.cells // 2), section(rng, "S2", 4.6, args.cells // 2)],
                  ignore_index=True)
    d["cell_id"] = [f"demo_{i}" for i in range(len(d))]
    d["split"] = np.where(d.section == "S1", "dev", "test")
    d["bill_eugene_code"] = d.truth
    v = noisy(rng, d.truth.values, 0.08)
    v[(d.depth.values >= 0.40) & (d.depth.values < 0.52)] = "Basal(ifn)"          # widened ifn band
    c = noisy(rng, d.truth.values, 0.15)
    s2 = d.section.values == "S2"
    c[s2 & (d.truth.values == "Squam1")] = "Squam2"
    c[s2 & (d.truth.values == "Squam2")] = "Squam1"
    c[rng.random(len(c)) < 0.02] = ""                                               # a few unlabelled
    d["vestrum"], d["claude_code"] = v, c

    # synthetic stains and spatial domains for the assisted selection tools
    t = d.truth.values
    epi = np.isin(t, ["Keratin", "Squam1", "Squam2", "Squam3", "Basal(core)", "Basal(stem)", "Basal(ifn)"])
    imm = np.isin(t, ["Tcells", "Bcells", "TAMs", "DCs", "Mast"])
    lognorm = lambda mu, n: np.round(np.exp(rng.normal(np.log(mu), 0.35, n)), 1)
    d["panck"] = np.where(epi, lognorm(9000, len(d)), lognorm(1300, len(d)))
    d["cd45"] = np.where(imm, lognorm(6000, len(d)), lognorm(1500, len(d)))
    d["cd68"] = np.where(t == "TAMs", lognorm(900, len(d)), lognorm(480, len(d)))
    dep = d.depth.values
    d["novae5"] = np.where(dep < 0.2, "D1", np.where(dep < 0.48, "D2", np.where(d.x_mm.values % 4.6 > 2.3, "D3", "D4")))
    d["novae10"] = d.novae5 + np.where(d.x_mm.values % 2.3 > 1.15, "a", "b")
    d["novae20"] = d.novae10 + np.where(d.y_mm.values > 1.5, "u", "l")

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    cols = ["cell_id", "section", "split", "x_mm", "y_mm", "bill_eugene_code", "vestrum", "claude_code",
            "panck", "cd45", "cd68", "novae5", "novae10", "novae20"]
    write_cells(d[cols], out)

    sources = []
    for s in SOURCES:
        e = dict(s, n_labelled=int((d[s["key"]] != "").sum()))
        if s["kind"] == "agent":
            e["runs"] = {}
            for split in ("dev", "test"):
                sub = d[(d.split == split) & (d[s["key"]] != "")]
                e["runs"][split] = {"chosen": {"run": "synthetic", "rep": "demo",
                                               "ari": round(ari(sub.bill_eugene_code.values, sub[s["key"]].values), 4)},
                                    "candidates": []}
        else:
            e["runs"] = {"all": {"chosen": {"run": "synthetic", "rep": "demo"}}}
        sources.append(e)
    manifest = {
        "pack": "demo", "demo": True,
        "dataset": "DEMO: synthetic tissue, not patient data",
        "n_cells": int(len(d)), "sections": ["S1", "S2"],
        "splits": {"dev": ["S1"], "test": ["S2"]},
        "split_note": "synthetic demo: S1 plays the dev section",
        "sources": sources,
        "vocab": [{"code": i, "name": n, "color": c} for i, (n, c) in enumerate(CANON)],
        "attributes": ATTRIBUTES,
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2))
    print(f"wrote {out}: {len(d)} synthetic cells")


if __name__ == "__main__":
    main()
