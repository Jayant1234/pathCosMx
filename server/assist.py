"""Assisted selection: a per-section cell graph and the tools that flood, refine or rank regions on it.

Every tool returns cell indices only; the browser shows them as a selection and nothing is applied
until the reviewer confirms in the popover.

Graph: Delaunay triangulation of each section's cell centres (slide mm -> um); edges longer than
GRAPH_MAX_UM are dropped and the rest keep their length, so a "max gap" slider just masks edges.
Built on first use, kept in memory and cached next to the sessions as .npz (keyed by pack + coords + edge cap).

Connected regions with a "jump" of k cells (k = 0..2 cells of another class may be crossed):
  k=0  cc over edges between same-class cells
  k=1  same + edges same->foreign, no foreign->foreign edges (every foreign run has length 1)
  k=2  also foreign->foreign edges, but only between foreign cells that touch a same-class cell
These graphs have exactly the components of "paths whose foreign runs are at most k long".

Compartments: per section Otsu threshold on log1p(PanCK) and log1p(CD45); epithelial if PanCK is
above its threshold, else immune if CD45 is above its threshold, else stromal. The browser can shift
both thresholds (in log units).

Refine border: Grady's random walker on the graph of the selection dilated by 3 hops. Edge weight
w = exp(-beta * (|dz|^2 + lam * [labels differ] + mu * [compartments differ])), z = per-section
standardised log [PanCK, CD45], labels = the layer of the panel the seeds were placed on, compartment =
the Otsu compartment above (with the slider shifts); beta = 2, lam = 0.5, mu = 4. New selection =
P(inside) > 0.5. Real slide, 60 epithelium/stroma border discs x 3 draws, accuracy vs Bill's epithelial
labels (median): plain PanCK-Otsu 0.87-0.88; walker without the compartment term 0.82-0.84, with
mu = 4 0.876-0.881 (mean 0.833-0.863, the threshold 0.831-0.861). So on stains it ties the threshold,
not beats it; CD68 or edge length in the weights and beta 4 or seed-tuned beta all did worse or no
better. Its use is mixed or label-defined borders where the stains do not separate.
"""
import hashlib
import threading
import time
from collections import Counter
from pathlib import Path

import numpy as np
from scipy import sparse
from scipy.sparse.csgraph import connected_components
from scipy.cluster.vq import kmeans2
from scipy.sparse.linalg import spsolve
from scipy.spatial import Delaunay

from .pack import SOURCE_KEYS

GRAPH_MAX_UM = 60.0         # longest edge kept (the slider's top end)
DEFAULT_GAP_UM = 30.0       # real slide: Delaunay edge median 12 um, p95 35 um
COMP_NAMES = ["stromal", "epithelial", "immune"]
QUEUE_MIN = 20
QUEUE_MAX = 1500


def otsu(v, bins=256):
    v = v[np.isfinite(v)]
    if v.size < 2 or v.min() == v.max():
        return float(v.mean()) if v.size else 0.0
    h, e = np.histogram(v, bins)
    c = (e[:-1] + e[1:]) / 2
    w0 = np.cumsum(h); w1 = w0[-1] - w0
    m0 = np.cumsum(h * c) / np.maximum(w0, 1)
    m1 = ((h * c).sum() - np.cumsum(h * c)) / np.maximum(w1, 1)
    return float(c[np.argmax(w0 * w1 * (m0 - m1) ** 2)])


class Assist:
    def __init__(self, pack, cache_dir: Path):
        self.pack, self.cache_dir = pack, Path(cache_dir)
        self.lock = threading.Lock()
        self._graph = None
        self._queue = {}
        cells = pack.cells
        self.sec_cells = [np.nonzero(pack.sec == k)[0] for k in range(len(pack.sections))]
        self.local = np.zeros(pack.n, np.int64)                 # cell -> index inside its section
        for idx in self.sec_cells:
            self.local[idx] = np.arange(idx.size)
        self.has_stains = all(k in cells and cells[k].notna().any() for k in ("panck", "cd45"))
        self.domains = {k: v for k, v in (pack.manifest.get("attributes", {}).get("domains") or {}).items() if k in cells}
        self.dom_codes, self.dom_names = {}, {}
        for k in self.domains:
            names, codes = np.unique(cells[k].fillna("").values.astype(str), return_inverse=True)
            self.dom_codes[k], self.dom_names[k] = codes.astype(np.uint16), names.tolist()
        if self.has_stains:
            self.logs = np.column_stack([np.log1p(np.nan_to_num(cells[k].values.astype(float), nan=0.0).clip(0))
                                         for k in ("panck", "cd45")])
            self.thr = np.array([[otsu(self.logs[idx, j]) for j in range(2)] for idx in self.sec_cells])
            self.z = np.zeros_like(self.logs)
            for idx in self.sec_cells:
                x = self.logs[idx]
                self.z[idx] = (x - x.mean(0)) / np.maximum(x.std(0), 1e-6)
        else:
            self.logs = self.thr = None
            self.z = np.zeros((pack.n, 2))
        self.default_gap = DEFAULT_GAP_UM

    # ---------- graph
    def _key(self):
        h = hashlib.sha1(self.pack.x_mm.tobytes() + self.pack.y_mm.tobytes() + self.pack.sec.tobytes()).hexdigest()[:12]
        return f"graph_v1_{GRAPH_MAX_UM:g}_{self.pack.manifest.get('pack', 'pack')}_{self.pack.n}_{h}.npz"

    def graph(self):
        """Per section: (u, v, length_um) with u < v in local indices."""
        with self.lock:
            if self._graph is not None:
                return self._graph
            f = self.cache_dir / self._key()
            g = None
            if f.exists():
                try:
                    z = np.load(f)
                    g = [(z[f"u{k}"], z[f"v{k}"], z[f"d{k}"]) for k in range(len(self.sec_cells))]
                except Exception:
                    g = None
            if g is None:
                g = [self._triangulate(idx) for idx in self.sec_cells]
                try:
                    self.cache_dir.mkdir(parents=True, exist_ok=True)
                    tmp = f.with_suffix(".tmp.npz")
                    np.savez(tmp, **{f"{c}{k}": a for k, e in enumerate(g) for c, a in zip("uvd", e)})
                    tmp.replace(f)
                except OSError:
                    pass
            self._graph = g
            # a 1-hop-gap median per pack: sparse packs (the synthetic demo) need a wider default gap
            med = np.median(np.concatenate([e[2] for e in g])) if any(e[2].size for e in g) else 12.0
            self.default_gap = float(np.clip(round(2.5 * med / 5) * 5, DEFAULT_GAP_UM, GRAPH_MAX_UM))
            return g

    def _triangulate(self, idx):
        if idx.size < 3:
            return np.zeros(0, np.int32), np.zeros(0, np.int32), np.zeros(0, np.float32)
        xy = np.column_stack([self.pack.x_mm[idx], self.pack.y_mm[idx]]) * 1000.0
        t = Delaunay(xy).simplices
        e = np.sort(np.vstack([t[:, [0, 1]], t[:, [1, 2]], t[:, [0, 2]]]), 1)
        e = np.unique(e, axis=0)
        d = np.hypot(*(xy[e[:, 0]] - xy[e[:, 1]]).T)
        keep = d <= GRAPH_MAX_UM
        return e[keep, 0].astype(np.int32), e[keep, 1].astype(np.int32), d[keep].astype(np.float32)

    def adjacency(self, k, gap):
        u, v, d = self.graph()[k]
        m = d <= gap
        n = self.sec_cells[k].size
        a = sparse.coo_matrix((np.ones(m.sum(), np.int8), (u[m], v[m])), shape=(n, n)).tocsr()
        return a + a.T

    # ---------- connected regions
    def components(self, k, same, gap, jump=0, allowed=None):
        """Component id per local cell of section k for the class mask `same` (-1 where not same)."""
        u, v, d = self.graph()[k]
        n = same.size
        ok = d <= gap
        if allowed is not None:
            ok &= allowed[u] & allowed[v]
        su, sv = same[u], same[v]
        keep = ok & su & sv
        if jump >= 1:
            keep |= ok & (su ^ sv)
        if jump >= 2:
            touch = np.zeros(n, bool)
            mixed = ok & (su ^ sv)
            touch[np.where(su[mixed], v[mixed], u[mixed])] = True
            keep |= ok & ~su & ~sv & touch[u] & touch[v]
        a = sparse.coo_matrix((np.ones(keep.sum(), np.int8), (u[keep], v[keep])), shape=(n, n))
        _, lab = connected_components(a, directed=False)
        lab = lab.astype(np.int64)
        lab[~same] = -1
        return lab

    def flood(self, classes, seed, gap, jump=0, within=None):
        """Cells of the seed's class connected to it (global indices)."""
        k = int(self.pack.sec[seed])
        idx = self.sec_cells[k]
        cls = classes[idx]
        same = cls == classes[seed]
        allowed = None
        if within is not None:
            allowed = np.zeros(idx.size, bool)
            w = within[self.pack.sec[within] == k]
            allowed[self.local[w]] = True
            allowed[self.local[seed]] = True
        lab = self.components(k, same, gap, jump, allowed)
        return idx[lab == lab[self.local[seed]]]

    # ---------- per-cell attributes
    def compartments(self, shift=(0.0, 0.0)):
        if not self.has_stains:
            return None
        thr = self.thr[self.pack.sec] + np.asarray(shift, float)
        epi = self.logs[:, 0] > thr[:, 0]
        imm = ~epi & (self.logs[:, 1] > thr[:, 1])
        return np.where(epi, 1, np.where(imm, 2, 0)).astype(np.uint8)

    def info(self):
        th = None
        if self.has_stains:
            th = {s: {"panck_log": round(float(t[0]), 4), "cd45_log": round(float(t[1]), 4),
                      "panck": round(float(np.expm1(t[0])), 1), "cd45": round(float(np.expm1(t[1])), 1)}
                  for s, t in zip(self.pack.sections, self.thr)}
        return {"stains": self.has_stains, "thresholds": th, "compartments": COMP_NAMES,
                "domains": {k: {"title": v, "names": self.dom_names[k]} for k, v in self.domains.items()},
                "graph_ready": self._graph is not None, "default_gap": self.default_gap,
                "gap_range": [15, GRAPH_MAX_UM]}

    # ---------- random walker
    def walker(self, selection, inside, outside, labels, gap, beta=2.0, lam=0.5, mu=4.0, shift=(0.0, 0.0), hops=3):
        sel = np.zeros(self.pack.n, bool); sel[selection] = True
        comp = self.compartments(shift)
        out = []
        for k, idx in enumerate(self.sec_cells):
            ins = [i for i in inside if self.pack.sec[i] == k]
            outs = [i for i in outside if self.pack.sec[i] == k]
            s_loc = sel[idx]
            if not s_loc.any() and not ins:
                continue
            if not ins or not outs:                       # no seeds of both kinds here: keep as selected
                out.append(idx[s_loc]); continue
            a = self.adjacency(k, gap)
            r = s_loc.copy(); r[self.local[ins + outs]] = True
            for _ in range(hops):
                r |= (a @ r.astype(np.int8)) > 0
            ri = np.nonzero(r)[0]
            sub = a[ri][:, ri].tocoo()
            m = sub.row < sub.col
            p, q = sub.row[m], sub.col[m]
            gi, gj = idx[ri[p]], idx[ri[q]]
            d2 = ((self.z[gi] - self.z[gj]) ** 2).sum(1) + lam * (labels[gi] != labels[gj])
            if comp is not None:
                d2 += mu * (comp[gi] != comp[gj])
            w = np.exp(-beta * d2) + 1e-9
            nr = ri.size
            W = sparse.coo_matrix((np.r_[w, w], (np.r_[p, q], np.r_[q, p])), shape=(nr, nr)).tocsr()
            L = sparse.diags(np.asarray(W.sum(1)).ravel()) - W
            pos = np.full(idx.size, -1); pos[ri] = np.arange(nr)
            x = np.full(nr, np.nan)
            x[pos[self.local[outs]]] = 0.0
            x[pos[self.local[ins]]] = 1.0
            seeded = ~np.isnan(x)
            ncomp, comp = connected_components(W, directed=False)
            has_seed = np.zeros(ncomp, bool); has_seed[comp[seeded]] = True
            unk = ~seeded & has_seed[comp]
            if unk.any():
                Luu = L[unk][:, unk].tocsc()
                rhs = -(L[unk][:, seeded] @ x[seeded])
                x[unk] = spsolve(Luu, rhs) if unk.sum() > 1 else rhs / Luu.toarray().ravel()
            free = np.isnan(x)                            # components without seeds keep their state
            keep = np.where(free, s_loc[ri], x > 0.5)
            out.append(idx[ri[keep]])
        return np.concatenate(out) if out else np.zeros(0, np.int64)

    # ---------- review queue
    def queue(self, kind, root, gap=None):
        """Disagreement hotspots, ranked by expected fixes.

        "both": cells where the two agents agree with each other and differ from Bill; components with
        1-cell gaps (every cell already has one agent label, so one pick fixes an item).
        "any": cells where an agent differs from Bill, split by pattern = (Bill label, agent label: the
        shared one, the one dissenter's, or "split") and by Novae domain (novae10 when present), with no
        gap-jumping, so an item is one confusion in one niche (with 1-cell gaps the real slide's top items
        were 22k / 10k / 8k cells). Components >= QUEUE_MIN cells; anything above QUEUE_MAX is cut
        spatially (k-means on the cell centres, recursively). Ranked by score = sum over the cells of
        1 (both agents vs Bill), 0.33 (one agent), 0.25 (agents split each other): expected fixes."""
        self.graph()                                   # default_gap is known only once the graph exists
        gap = gap or self.default_gap
        key = (kind, root.tobytes(), gap)
        with self.lock:
            if key in self._queue:
                return self._queue[key]
        src = self.pack.src
        r = lambda a: np.where(a == 255, -1, root[np.minimum(a, 255)])
        b, v, c = r(src["bill_eugene_code"]), r(src["vestrum"]), r(src["claude_code"])
        vo, co = (v >= 0) & (v != b), (c >= 0) & (c != b)
        both = vo & co & (v == c)
        disp = both if kind == "both" else vo | co
        agent = np.where(both | (vo & ~co), v, np.where(co & ~vo, c, -2))
        w = np.where(both, 1.0, np.where(vo & co, 0.25, 0.33))
        pat = (b + 1) * 1000 + (agent + 2)
        dom = None
        if kind != "both" and self.dom_codes:
            dom = self.dom_codes["novae10" if "novae10" in self.dom_codes else sorted(self.dom_codes)[0]].astype(np.int64)
            pat = pat * 1000 + dom
        items, item_of = [], np.full(self.pack.n, -1, np.int64)
        for k, idx in enumerate(self.sec_cells):
            dk, pk = disp[idx], pat[idx]
            if kind == "both":
                lab = self.components(k, dk, gap, jump=1)
            else:                                      # no jumping: same pattern, directly adjacent
                u, vv, d = self.graph()[k]
                ok = (d <= gap) & dk[u] & dk[vv] & (pk[u] == pk[vv])
                a = sparse.coo_matrix((np.ones(ok.sum(), np.int8), (u[ok], vv[ok])), shape=(idx.size, idx.size))
                lab = connected_components(a, directed=False)[1].astype(np.int64)
                lab[~dk] = -1
            ids, cnt = np.unique(lab[lab >= 0], return_counts=True)
            order = np.argsort(lab, kind="stable")
            starts = np.searchsorted(lab[order], ids)
            for cid, s0, n0 in zip(ids, starts, cnt):
                if n0 < QUEUE_MIN:
                    continue
                for cells in self._chunks(idx[order[s0:s0 + n0]]):
                    items.append({"section": self.pack.sections[k], "cells": cells, "score": float(w[cells].sum())})
        items.sort(key=lambda t: -t["score"])
        for j, it in enumerate(items):
            item_of[it["cells"]] = j
            it.update(id=j, n=int(it["cells"].size), score=round(it["score"], 1), confusion=self._confusion(it["cells"], b, v, c, kind),
                      both_pct=round(100 * float(both[it["cells"]].mean())),
                      domain=None if dom is None else self.dom_names["novae10" if "novae10" in self.dom_codes else sorted(self.dom_codes)[0]][int(dom[it["cells"][0]])],
                      bbox_px=[round(float(self.pack.px[it["cells"]].min()), 1), round(float(self.pack.py[it["cells"]].min()), 1),
                               round(float(self.pack.px[it["cells"]].max()), 1), round(float(self.pack.py[it["cells"]].max()), 1)])
        res = {"items": items, "item_of": item_of}
        with self.lock:
            if len(self._queue) > 8:
                self._queue.clear()
            self._queue[key] = res
        return res

    def _chunks(self, cells):
        """Cut a component into spatial chunks of <= QUEUE_MAX cells (k-means on slide mm, recursive)."""
        if cells.size <= QUEUE_MAX:
            return [cells]
        xy = np.column_stack([self.pack.x_mm[cells], self.pack.y_mm[cells]])
        k = int(np.ceil(cells.size / (0.8 * QUEUE_MAX)))
        _, lab = kmeans2(xy, k, minit="++", seed=0)
        parts = [cells[lab == j] for j in range(k) if (lab == j).any()]
        if len(parts) == 1:                            # degenerate (identical points): split by order
            parts = np.array_split(cells, k)
        return [c for p in parts for c in self._chunks(p)]

    def _confusion(self, cells, b, v, c, kind):
        """{'bill': code, 'vestrum': code, 'claude_code': code} - the commonest disputed pattern."""
        bb = b[cells]
        top_b = Counter(bb.tolist()).most_common(1)[0][0]
        m = cells[bb == top_b]
        out = {"bill_eugene_code": int(top_b)}
        for key, a in (("vestrum", v), ("claude_code", c)):
            vals = a[m]; vals = vals[(vals >= 0) & (vals != top_b)]
            out[key] = int(Counter(vals.tolist()).most_common(1)[0][0]) if vals.size else int(top_b)
            out[key + "_pct"] = round(100 * vals.size / max(m.size, 1))
        return out

    def queue_done(self, q, ops, op_cells):
        """Item ids that one active (non-section) op touched on >= 50% of their cells."""
        done = np.zeros(len(q["items"]), bool)
        if not q["items"]:
            return done
        size = np.array([it["n"] for it in q["items"]])
        for o in ops:
            if o.get("scope") or o["op"] == "clear":
                continue
            it = q["item_of"][op_cells(o)]
            hit = np.bincount(it[it >= 0], minlength=len(size))
            done |= hit >= 0.5 * size
        return done


def efficiency(ops):
    """Per tool: ops, cells, median ms from selection start to apply, and how many were undone."""
    undone = {o["target"] for o in ops if o["op"] == "undo"}
    by = {}
    for o in ops:
        if o["op"] not in ("base", "relabel", "clear"):
            continue
        t = by.setdefault(o.get("tool") or ("section" if o.get("scope") else "unknown"), {"n_ops": 0, "cells": 0, "ms": [], "undone": 0})
        t["n_ops"] += 1
        t["cells"] += int(o.get("n", 0))
        if o.get("sel_ms") is not None:
            t["ms"].append(float(o["sel_ms"]))
        t["undone"] += o["id"] in undone
    return {k: {"n_ops": t["n_ops"], "cells": t["cells"], "median_ms": round(float(np.median(t["ms"]))) if t["ms"] else None,
                "undone": t["undone"]} for k, t in sorted(by.items())}


def timed(fn, *a, **kw):
    t = time.perf_counter()
    r = fn(*a, **kw)
    return r, round(1000 * (time.perf_counter() - t), 1)
