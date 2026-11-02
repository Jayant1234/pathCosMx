#!/usr/bin/env python3
"""Build a PathQC slide pack: one table of cells with the three label sources joined on cell_id.

Sources
  bill_eugene_code  the lab's annotation (sealed/gold_labels.tsv, column final_cell)
  vestrum           Vestrum_knowledge runs
  claude_code       bare claude -p runs
For vestrum and claude_code one run per split is chosen: the candidate with the highest ARI
against bill_eugene_code on that split's sections (user rule, 2026-11-02). Every candidate's
ARI is recorded in the manifest so the choice can be checked.

Output (pack dir)
  cells.tsv.gz    cell_id, section, split, x_mm, y_mm, <source> (label string or empty)
  manifest.json   sources, chosen runs, candidate ARIs, vocabulary, sections

Usage: python build_pack.py --sandbox <jayant_agent/sandbox> --coords <dir with coords_dev.tsv, coords_test.tsv> \
           --out packs/hnsc_slide1
The coords files come from the agents' input objects (cell_id, x_mm, y_mm, fov from x/y_slide_mm).
The pack holds patient-derived data: keep it out of public repositories.
"""
import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

from cells_io import write_cells

HERE = Path(__file__).resolve().parent
SANDBOX = None   # set from --sandbox

SPLITS = {"dev": ["S1", "S4"], "test": ["S2", "S3", "S5", "S6"]}

# candidate runs per (source, split); paths relative to SANDBOX/runs
CANDIDATES = {
    ("vestrum", "dev"): ["vestrum/dev/v/rep81_20260918_132953", "vestrum/dev/v/rep82_20260918_141054"],
    ("vestrum", "test"): ["vestrum/test/v/rep901_20260918_150110", "vestrum/test/v/rep902_20260918_155554"],
    ("claude_code", "dev"): ["cc/dev/v/rep1_20260904_144411", "cc/dev/v/rep2_20260904_153831",
                             "vestrum/dev/v/rep61_20260917_135155"],
    ("claude_code", "test"): ["cc/test/v/rep951_20260918_150154", "cc/test/v/rep952_20260918_165217"],
}
NOTES = {
    "vestrum/test/v/rep902_20260918_155554": "cut at 90 min by the engine timeout; labels were complete, no DECISIONS.md",
    "vestrum/dev/v/rep61_20260917_135155": "bare claude -p run under the vestrum engine with every component off",
}

SOURCES = [
    {"key": "bill_eugene_code", "name": "Bill_eugene_code", "kind": "reference"},
    {"key": "vestrum", "name": "Vestrum", "kind": "agent"},
    {"key": "claude_code", "name": "Claude Code", "kind": "agent"},
]

# display order and colours (same palette as the Vestrum Knowledge Console)
CANON = [
    ("Basal(core)", "#c8432f"), ("Basal(stem)", "#e3792b"), ("Basal(ifn)", "#f0b323"),
    ("Squam1", "#d95f8a"), ("Squam2", "#8e3b8e"), ("Squam3", "#b07cc6"), ("Keratin", "#5a2a6e"),
    ("Tcells", "#2b6fc9"), ("Bcells", "#5fa8e6"), ("TAMs", "#6c4bd1"), ("DCs", "#b48be8"),
    ("Mast", "#2fa39a"), ("CAF1", "#3f9d4a"), ("Perictye", "#86b33c"), ("Muscle", "#8a5a2b"),
    ("Endoth", "#16a2b8"), ("Unknown", "#9aa3ad"),
]
EXTRA_COLOURS = ["#e377c2", "#7f7f7f", "#bcbd22", "#17becf", "#ff9896", "#98df8a", "#c5b0d5"]


def ari(a, b):
    """Adjusted Rand index of two label vectors (same as mclust::adjustedRandIndex)."""
    _, ai = np.unique(a, return_inverse=True)
    _, bi = np.unique(b, return_inverse=True)
    ct = np.zeros((ai.max() + 1, bi.max() + 1), dtype=np.int64)
    np.add.at(ct, (ai, bi), 1)
    comb = lambda x: (x * (x - 1) / 2.0).sum()
    n = len(a)
    s_ij, s_a, s_b = comb(ct), comb(ct.sum(1)), comb(ct.sum(0))
    expected = s_a * s_b / (n * (n - 1) / 2.0)
    return float((s_ij - expected) / (0.5 * (s_a + s_b) - expected))


def load_labels(run):
    f = SANDBOX / "runs" / run / "sandbox" / "work" / "output" / "labels.tsv"
    d = pd.read_csv(f, sep="\t", dtype=str)
    return d.drop_duplicates("cell_id").set_index("cell_id")["label"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sandbox", required=True, help="jayant_agent/sandbox (sealed gold + runs)")
    ap.add_argument("--coords", default=str(HERE / "raw"), help="dir with coords_dev.tsv and coords_test.tsv")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    global SANDBOX
    SANDBOX = Path(args.sandbox)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    gold = pd.read_csv(SANDBOX / "sealed" / "gold_labels.tsv", sep="\t", dtype=str).set_index("cell_id")
    coords = pd.concat([pd.read_csv(Path(args.coords) / f"coords_{s}.tsv", sep="\t", dtype={"cell_id": str})
                        for s in ("dev", "test")])
    coords = coords.drop_duplicates("cell_id").set_index("cell_id")
    cells = gold[["sample_slide", "final_cell"]].join(coords[["x_mm", "y_mm"]], how="inner")
    cells = cells.rename(columns={"sample_slide": "section", "final_cell": "bill_eugene_code"})
    cells["split"] = np.where(cells.section.isin(SPLITS["dev"]), "dev", "test")

    chosen = {}
    for (src, split), runs in CANDIDATES.items():
        sub = cells[cells.split == split]
        scored = []
        for run in runs:
            lab = load_labels(run).reindex(sub.index)
            ok = lab.notna()
            scored.append({
                "run": run, "rep": run.split("/")[-1].split("_")[0],
                "ari": round(ari(sub.bill_eugene_code[ok].values, lab[ok].values), 4),
                "coverage": round(float(ok.mean()), 4), "note": NOTES.get(run, ""),
            })
        best = max(scored, key=lambda r: r["ari"])
        chosen[(src, split)] = {"chosen": best, "candidates": scored}
        print(f"{src:12s} {split:4s} " + "  ".join(f"{r['rep']}={r['ari']:.3f}" for r in scored) + f"  -> {best['rep']}")

    for src in ("vestrum", "claude_code"):
        col = pd.Series(index=cells.index, dtype=object)
        for split in SPLITS:
            idx = cells.index[cells.split == split]
            col.loc[idx] = load_labels(chosen[(src, split)]["chosen"]["run"]).reindex(idx).values
        cells[src] = col.fillna("")

    # vocabulary: canonical names first, then anything else a source used
    names = [n for n, _ in CANON]
    seen = set()
    for s in SOURCES:
        seen |= set(cells[s["key"]].unique())
    extras = sorted(n for n in seen - set(names) if n)
    vocab = [{"code": i, "name": n, "color": c} for i, (n, c) in enumerate(CANON)]
    vocab += [{"code": len(CANON) + j, "name": n, "color": EXTRA_COLOURS[j % len(EXTRA_COLOURS)]}
              for j, n in enumerate(extras)]

    cells = cells.reset_index().rename(columns={"index": "cell_id"})
    cells = cells[["cell_id", "section", "split", "x_mm", "y_mm", "bill_eugene_code", "vestrum", "claude_code"]]
    write_cells(cells, out)

    sources = []
    for s in SOURCES:
        entry = dict(s)
        if s["kind"] == "agent":
            entry["runs"] = {split: chosen[(s["key"], split)] for split in SPLITS}
        else:
            entry["runs"] = {"all": {"chosen": {"run": "sealed/gold_labels.tsv", "rep": "gold"}}}
        entry["n_labelled"] = int((cells[s["key"]] != "").sum())
        sources.append(entry)

    manifest = {
        "pack": out.name,
        "dataset": "HNSC CosMx 6K, one slide, six tissue sections",
        "n_cells": int(len(cells)),
        "sections": sorted(cells.section.unique()),
        "splits": SPLITS,
        "split_note": "dev sections S1+S4: Vestrum's knowledge file was written from these; read wins there with care",
        "sources": sources,
        "vocab": vocab,
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2))
    print(f"wrote {out}/cells.tsv.gz ({len(cells)} cells), vocab {len(vocab)} names, extras {extras}")


if __name__ == "__main__":
    main()
