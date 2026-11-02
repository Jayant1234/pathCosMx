"""Read and write a pack's cell table.

The app ships `cells.tsv.gz` (pandas only, no pyarrow in the bundle). Older packs with
`cells.parquet` still load when pyarrow is installed.
"""
from pathlib import Path

import pandas as pd

DTYPES = {"cell_id": str, "section": str, "split": str, "bill_eugene_code": str, "vestrum": str, "claude_code": str,
          "novae5": str, "novae10": str, "novae20": str}
STAIN_COLUMNS = ("panck", "cd45", "cd68")


def read_cells(pack, columns=None):
    pack = Path(pack)
    tsv = pack / "cells.tsv.gz"
    if tsv.exists():
        d = pd.read_csv(tsv, sep="\t", dtype=DTYPES, keep_default_na=False, usecols=columns)
    else:
        d = pd.read_parquet(pack / "cells.parquet", columns=columns)
    for k in ("bill_eugene_code", "vestrum", "claude_code"):
        if k in d:
            d[k] = d[k].fillna("")
    for k in STAIN_COLUMNS:
        if k in d:
            d[k] = pd.to_numeric(d[k], errors="coerce")
    return d


def write_cells(d, pack):
    d.to_csv(Path(pack) / "cells.tsv.gz", sep="\t", index=False, float_format="%.6f")
