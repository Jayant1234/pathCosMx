"""A loaded slide pack: cells, the three label sources as codes, and the mm <-> image-px transform."""
import json
from pathlib import Path

import numpy as np
import pandas as pd
import shapely
from shapely.geometry import Point, Polygon

from pack.cells_io import read_cells

SOURCE_KEYS = ["bill_eugene_code", "vestrum", "claude_code"]


class Pack:
    def __init__(self, path: Path, image_override=None):
        self.path = Path(path)
        self.manifest = json.loads((self.path / "manifest.json").read_text())
        self.transform = json.loads((self.path / "transform.json").read_text())
        if image_override:
            self.transform = json.loads(Path(image_override).read_text())
        self.cells = read_cells(self.path)
        self.n = len(self.cells)
        self.vocab = self.manifest["vocab"]
        code = {v["name"]: v["code"] for v in self.vocab}
        self.src = {k: np.array([code.get(s, 255) if s else 255 for s in self.cells[k]], np.uint8)
                    for k in SOURCE_KEYS}
        self.sections = self.manifest["sections"]
        self.sec = self.cells.section.map({s: i for i, s in enumerate(self.sections)}).values.astype(np.uint8)
        self.x_mm = self.cells.x_mm.values.astype(np.float64)
        self.y_mm = self.cells.y_mm.values.astype(np.float64)
        self.px, self.py = self.mm_to_px(self.x_mm, self.y_mm, self.cells.section.values)
        self.px = self.px.astype(np.float32)
        self.py = self.py.astype(np.float32)
        self.cell_ids = self.cells.cell_id.values

    # ---------- coordinates
    def _matrix(self, section=None):
        m = self.transform.get("sections", {}).get(section) if section else None
        return np.array(m or self.transform["mm_to_px"], dtype=np.float64)

    def mm_to_px(self, x, y, sections=None):
        x, y = np.asarray(x, float), np.asarray(y, float)
        if sections is None or not self.transform.get("sections"):
            m = self._matrix()
            return m[0, 0] * x + m[0, 1] * y + m[0, 2], m[1, 0] * x + m[1, 1] * y + m[1, 2]
        px, py = np.empty_like(x), np.empty_like(y)
        for s in np.unique(sections):
            k = sections == s
            m = self._matrix(s)
            px[k] = m[0, 0] * x[k] + m[0, 1] * y[k] + m[0, 2]
            py[k] = m[1, 0] * x[k] + m[1, 1] * y[k] + m[1, 2]
        return px, py

    def px_to_mm(self, px, py):
        """Inverse of the global transform (drawn geometry is converted with it)."""
        m = np.vstack([self._matrix(), [0, 0, 1]])
        inv = np.linalg.inv(m)
        px, py = np.asarray(px, float), np.asarray(py, float)
        return inv[0, 0] * px + inv[0, 1] * py + inv[0, 2], inv[1, 0] * px + inv[1, 1] * py + inv[1, 2]

    def geom_px_to_mm(self, coords):
        a = np.asarray(coords, float).reshape(-1, 2)
        x, y = self.px_to_mm(a[:, 0], a[:, 1])
        return np.column_stack([x, y]).round(6).tolist()

    def geom_mm_to_px(self, coords):
        a = np.asarray(coords, float).reshape(-1, 2)
        x, y = self.mm_to_px(a[:, 0], a[:, 1])
        return np.column_stack([x, y]).round(2).tolist()

    # ---------- spatial queries (in mm, so they do not depend on the image)
    def cells_in_polygon(self, poly_mm):
        pts = np.asarray(poly_mm, float)
        if len(pts) < 3:
            return np.zeros(0, np.int64)
        poly = Polygon(pts)
        if not poly.is_valid:
            poly = shapely.make_valid(poly)
        x0, y0, x1, y1 = poly.bounds
        cand = np.nonzero((self.x_mm >= x0) & (self.x_mm <= x1) & (self.y_mm >= y0) & (self.y_mm <= y1))[0]
        inside = shapely.contains_xy(poly, self.x_mm[cand], self.y_mm[cand])
        return cand[inside]

    def cells_near_point(self, pt_mm, radius_mm=0.015):
        d2 = (self.x_mm - pt_mm[0]) ** 2 + (self.y_mm - pt_mm[1]) ** 2
        return np.nonzero(d2 <= radius_mm ** 2)[0]

    def cells_of_geometry(self, geom):
        if geom["type"] == "Point":
            return self.cells_near_point(geom["mm"][0])
        return self.cells_in_polygon(geom["mm"])

    # ---------- binary payload for the browser
    def cells_payload(self):
        n = np.array([self.n], np.uint32).tobytes()
        parts = [self.px.tobytes(), self.py.tobytes(), self.sec.tobytes()]
        parts += [self.src[k].tobytes() for k in SOURCE_KEYS]
        return n + b"".join(parts)
