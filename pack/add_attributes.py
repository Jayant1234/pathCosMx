#!/usr/bin/env python3
"""Add per-cell attributes used by the assisted selection tools to an existing pack.

Attributes (all optional; the app hides a tool whose attribute is missing)
  panck, cd45, cd68      mean immunofluorescence intensity per cell (CosMx morphology stains)
  novae5, novae10, novae20   Novae spatial-domain ids at three resolutions

The meta table is a TSV with cell_id plus the AtoMx columns Mean.PanCK, Mean.CD45, Mean.CD68 and
novae_domains_5/10/20 (exported from the Seurat objects' meta.data). The label columns of the
pack are not touched.

Usage: python add_attributes.py --pack packs/hnsc_slide1 --meta meta.tsv.gz
"""
import argparse
import json
from pathlib import Path

import pandas as pd

try:
    from .cells_io import read_cells, write_cells
except ImportError:            # run as a script
    from cells_io import read_cells, write_cells

STAINS = {"panck": "Mean.PanCK", "cd45": "Mean.CD45", "cd68": "Mean.CD68"}
DOMAINS = {"novae5": "novae_domains_5", "novae10": "novae_domains_10", "novae20": "novae_domains_20"}
ATTRIBUTES = {
    "stains": {"panck": "PanCK (epithelium)", "cd45": "CD45 (immune)", "cd68": "CD68 (macrophage)"},
    "domains": {"novae5": "Novae, 5 domains", "novae10": "Novae, 10 domains", "novae20": "Novae, 20 domains"},
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pack", required=True)
    ap.add_argument("--meta", required=True)
    args = ap.parse_args()
    pack = Path(args.pack)
    cells = read_cells(pack)
    meta = pd.read_csv(args.meta, sep="\t", usecols=["cell_id", *STAINS.values(), *DOMAINS.values()],
                       dtype={"cell_id": str, **{c: str for c in DOMAINS.values()}})
    meta = meta.drop_duplicates("cell_id").set_index("cell_id")
    m = meta.reindex(cells.cell_id)
    for k, c in STAINS.items():
        cells[k] = m[c].values.round(1)
    for k, c in DOMAINS.items():
        cells[k] = m[c].fillna("").values
    print("joined", int(m[STAINS["panck"]].notna().sum()), "of", len(cells), "cells")
    write_cells(cells, pack)
    man = json.loads((pack / "manifest.json").read_text())
    man["attributes"] = ATTRIBUTES
    (pack / "manifest.json").write_text(json.dumps(man, indent=2))


if __name__ == "__main__":
    main()
