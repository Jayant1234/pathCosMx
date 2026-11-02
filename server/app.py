"""PathQC Flask server: serves the viewer, the slide tiles, the cells, and the review API."""
import sys
import threading
import time
import traceback
from collections import Counter
from pathlib import Path

import numpy as np
import shapely
from flask import Flask, Response, abort, jsonify, request, send_from_directory

from shapely.geometry import LineString, MultiPoint, Point, Polygon

from . import export
from .assist import COMP_NAMES, Assist, timed
from .pack import SOURCE_KEYS, Pack
from .session import Session, TAG_NAMES, slug

WEB = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1])) / "web"   # bundled app or checkout


def create_app(pack_dir, results_dir=None, image_transform=None):
    pack = Pack(Path(pack_dir), image_override=image_transform)
    results_root = Path(results_dir or pack.path / "results")
    sessions_root = pack.path / "sessions"
    sessions = {}
    section_cells = {s: np.nonzero(pack.sec == k)[0] for k, s in enumerate(pack.sections)}
    save_status = {}
    assist = Assist(pack, sessions_root / "_cache")
    app = Flask(__name__, static_folder=None)

    def sess():
        name = (request.args.get("reviewer") or (request.get_json(silent=True) or {}).get("reviewer") or "").strip()
        if not name:
            abort(400, "reviewer name required")
        key = slug(name)
        if key not in sessions:
            sessions[key] = Session(sessions_root, name, pack.vocab, pack.n, section_cells)
        return sessions[key]

    def geometry_in(g):
        """Browser geometry (image px) -> stored geometry (slide mm, plus type)."""
        if g["type"] not in ("Point", "Polygon"):
            abort(400, "geometry must be Point or Polygon")
        return {"type": g["type"], "mm": pack.geom_px_to_mm(g["px"])}

    def geometry_out(item):
        g = item["geometry"]
        return dict(item, geometry=dict(g, px=pack.geom_mm_to_px(g["mm"])))

    # ---------- static
    @app.route("/")
    def index():
        return send_from_directory(WEB, "index.html")

    @app.route("/web/<path:p>")
    def web(p):
        return send_from_directory(WEB, p)

    @app.route("/tiles/<path:p>")
    def tiles(p):
        dzi = pack.path / pack.transform["image"]["dzi"]
        return send_from_directory(dzi.parent, p, max_age=86400)

    # ---------- data
    @app.route("/api/state")
    def state():
        s = sess()
        m = pack.manifest
        return jsonify({
            "manifest": m,
            "image": {
                "kind": pack.transform.get("kind"), "description": pack.transform.get("description", ""),
                "width": pack.transform["image"]["width"], "height": pack.transform["image"]["height"],
                "dzi": "/tiles/" + Path(pack.transform["image"]["dzi"]).name,
                "um_per_px": pack.transform.get("um_per_px"),
                "registration": pack.transform.get("registration"),
            },
            "reviewer": s.reviewer,
            "vocab": s.vocab,
            "comments": [geometry_out(c) for c in s.comments],
            "annotations": [geometry_out(a) for a in s.annotations],
            "ops": [summarise_op(o) for o in s.active_ops()],
            "tag_names": TAG_NAMES,
        })

    def summarise_op(o):
        return {k: o.get(k) for k in ("id", "op", "source", "to", "to_name", "filter", "filter_name",
                                      "filter_ref", "filter_names", "scope", "sections",
                                      "n", "ts", "reviewer", "tool")} | {"geometry": {"type": "Polygon",
                                                                              "px": pack.geom_mm_to_px(o["mm"])}}

    @app.route("/api/cells.bin")
    def cells_bin():
        return Response(pack.cells_payload(), mimetype="application/octet-stream")

    @app.route("/api/final.bin")
    def final_bin():
        final, tag, _ = sess().replay(pack.src)
        return Response(final.astype(np.uint16).tobytes() + tag.tobytes(), mimetype="application/octet-stream")

    @app.route("/api/cell/<int:i>")
    def cell(i):
        if not 0 <= i < pack.n:
            abort(404)
        s = sess()
        final, tag, opi = s.replay(pack.src)
        names = {v["code"]: v["name"] for v in s.vocab}
        root = s.resolve_table()
        row = pack.cells.iloc[i]
        ops = s.active_ops()
        return jsonify({
            "index": i, "cell_id": row.cell_id, "section": row.section, "split": row.split,
            "x_mm": float(row.x_mm), "y_mm": float(row.y_mm),
            "labels": {k: row[k] for k in SOURCE_KEYS},
            "corrected": names.get(int(root[final[i]]), ""), "source": TAG_NAMES[int(tag[i])],
            "op": summarise_op(ops[opi[i]]) if opi[i] >= 0 else None,
        })

    # ---------- regions and edits
    def region_cells(s, body):
        poly_mm = pack.geom_px_to_mm(body["polygon"])
        idx = pack.cells_in_polygon(poly_mm)
        filt = body.get("filter")
        if filt is not None and idx.size:
            final, _, _ = s.replay(pack.src)
            root = s.resolve_table()
            idx = idx[root[final[idx]] == root[int(filt)]]
        return poly_mm, idx

    @app.route("/api/region", methods=["POST"])
    def region():
        s = sess()
        body = request.get_json()
        _, idx = region_cells(s, body)
        root = s.resolve_table()
        final, _, _ = s.replay(pack.src)
        comp = {}
        for k in SOURCE_KEYS:
            v = pack.src[k][idx]
            c = Counter(int(root[x]) if x != 255 else -1 for x in v)
            comp[k] = sorted(([code, n] for code, n in c.items()), key=lambda t: -t[1])
        cf = Counter(int(root[x]) for x in final[idx])
        comp["corrected"] = sorted(([code, n] for code, n in cf.items()), key=lambda t: -t[1])
        secs = Counter(pack.cells.section.values[idx])
        return jsonify({"n": int(idx.size), "composition": comp, "sections": dict(secs)})

    def bbox_mm(idx, d=0.004):                     # 4 um box around the cells, for display
        x0, x1 = pack.x_mm[idx].min() - d, pack.x_mm[idx].max() + d
        y0, y1 = pack.y_mm[idx].min() - d, pack.y_mm[idx].max() + d
        return [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]

    def selection_geoms(geoms, idx):
        """Browser selection shapes (image px) -> stored shapes (mm) + one outline polygon for display."""
        a, b = pack.px_to_mm([0, 1], [0, 0])
        mm_per_px = float(np.hypot(a[1] - a[0], b[1] - b[0]))
        out, add, sub, assisted = [], [], [], False
        for g in geoms:
            mm = pack.geom_px_to_mm(g["px"])
            rec = {"mode": "sub" if g.get("mode") == "sub" else "add", "type": g["type"], "mm": mm}
            if g["type"] == "Brush":
                rec["r_mm"] = round(float(g["r_px"]) * mm_per_px, 6)
                shp = (LineString(mm) if len(mm) > 1 else Point(mm[0])).buffer(rec["r_mm"])
            elif g["type"] == "Point":
                shp = Point(mm[0]).buffer(0.004)
            elif g["type"] == "Assist":            # a wand / walker / queue pick: seed points only, outline from cells
                rec["tool"] = str(g.get("tool", ""))[:24]
                assisted = True
                shp = None
            else:
                shp = shapely.make_valid(Polygon(mm)) if len(mm) >= 3 else None
            out.append(rec)
            if shp is not None:
                (sub if rec["mode"] == "sub" else add).append(shp)
        shape = shapely.union_all(add) if add else None
        if shape is not None and sub:
            shape = shape.difference(shapely.union_all(sub))
        if assisted or shape is None or shape.is_empty or shape.geom_type != "Polygon":
            pts = MultiPoint(np.column_stack([pack.x_mm[idx], pack.y_mm[idx]]))
            shape = (shapely.concave_hull(pts, ratio=0.15) if assisted and idx.size >= 4 else pts.convex_hull).buffer(0.004)
            if shape.geom_type != "Polygon":
                shape = pts.convex_hull.buffer(0.004)
        poly = shape.simplify(0.002).exterior.coords[:-1]
        return out, [[round(x, 6), round(y, 6)] for x, y in poly]

    @app.route("/api/ops", methods=["POST"])
    def add_op():
        s = sess()
        body = request.get_json()
        extra = {}
        if body.get("sections") is not None:       # whole section(s): start pick or clear
            secs = list(dict.fromkeys(x for x in body["sections"] if x in pack.sections))
            if not secs:
                abort(400, "unknown section")
            idx = np.concatenate([section_cells[x] for x in secs])
            poly_mm = bbox_mm(idx, 0)
            extra = {"scope": "all" if set(secs) == set(pack.sections) else "section", "sections": secs}
        elif body.get("cells") is not None:        # explicit cells (a selection, or a single cell)
            idx = np.unique(np.asarray([int(i) for i in body["cells"] if 0 <= int(i) < pack.n], dtype=np.int64))
            if not idx.size:
                abort(400, "no valid cells")
            if body.get("geoms"):
                extra["geoms"], poly_mm = selection_geoms(body["geoms"], idx)
            else:
                poly_mm = bbox_mm(idx)
        else:
            poly_mm, idx = region_cells(s, body)
        names = {v["code"]: v["name"] for v in s.vocab}
        op = {"op": body["op"], "mm": poly_mm, "n": int(idx.size), **extra}
        if body.get("tool"):                       # efficiency log: how the selection was made, and how long it took
            op["tool"] = str(body["tool"])[:24]
            op["tools"] = [str(t)[:24] for t in (body.get("tools") or [body["tool"]])][:12]
        if body.get("sel_ms") is not None:
            op["sel_ms"] = int(max(0, float(body["sel_ms"])))
        if "scope" not in op:                      # whole-section ops are resolved from their sections at replay
            op["cells"] = idx.tolist()
        if body.get("filter") is not None:
            op["filter"], op["filter_name"] = int(body["filter"]), names.get(int(body["filter"]))
        if body.get("filter_classes") is not None:  # class filter applied in the browser
            op["filter_ref"] = body.get("filter_ref")
            op["filter_names"] = [names.get(int(c), "not labelled") for c in body["filter_classes"]]
        if body["op"] == "base":
            if body.get("source") not in SOURCE_KEYS:
                abort(400, "unknown source")
            op["source"] = body["source"]
        elif body["op"] == "relabel":
            to = int(body["to"])
            if to not in names:
                abort(400, "unknown label")
            op["to"], op["to_name"] = to, names[to]
        elif body["op"] == "clear":
            if "sections" not in op:
                abort(400, "clear needs sections")
        else:
            abort(400, "op must be base, relabel or clear")
        if idx.size == 0:
            abort(400, "no cells in this region (after the class filter)")
        return jsonify(summarise_op(s.add_op(op)))

    @app.route("/api/undo", methods=["POST"])
    def undo():
        t = sess().undo((request.get_json() or {}).get("target"))
        return jsonify({"undone": t and summarise_op(t)})

    @app.route("/api/reset", methods=["POST"])
    def reset():
        """Archive the reviewer's whole session and start again from Bill_eugene_code."""
        s = sess()
        body = request.get_json() or {}
        if (body.get("confirm") or "").strip() != s.reviewer:
            abort(400, "type the reviewer name exactly to confirm the reset")
        dest = s.archive(sessions_root / "_archive")
        sessions.pop(slug(s.reviewer), None)
        return jsonify({"archived_to": str(dest)})

    # ---------- assisted selection (returns cells; the browser previews them, nothing is applied here)
    def layer_codes(s, layer):
        """Root label code per cell of a panel's layer: 'corr' (the reviewer's) or a source key / index."""
        root = s.resolve_table()
        if layer in (None, "corr", "mine"):
            a = s.replay(pack.src)[0]
        else:
            key = SOURCE_KEYS[int(layer)] if str(layer).isdigit() and int(layer) < len(SOURCE_KEYS) else layer
            if key not in pack.src:
                abort(400, "unknown layer")
            a = pack.src[key]
        return np.where(a == 255, -1, root[np.minimum(a, 255)]).astype(np.int64)

    def num(body, k, default, lo, hi):
        try:
            return float(min(hi, max(lo, float(body.get(k, default)))))
        except (TypeError, ValueError):
            return default

    def cell_list(v):
        try:
            a = np.asarray([int(i) for i in (v or [])], dtype=np.int64)
        except (TypeError, ValueError):
            abort(400, "cells must be a list of cell indices")
        return a[(a >= 0) & (a < pack.n)]

    @app.route("/api/assist/info")
    def assist_info():
        assist.graph()                             # built on first use (about a second on a real slide), then cached
        return jsonify(assist.info())

    @app.route("/api/assist/compartments.bin")
    def assist_comp():
        c = assist.compartments((num(request.args, "panck", 0, -5, 5), num(request.args, "cd45", 0, -5, 5)))
        if c is None:
            abort(404)
        return Response(c.tobytes(), mimetype="application/octet-stream")

    @app.route("/api/assist/domains.bin")
    def assist_domains():
        res = request.args.get("res")
        if res not in assist.dom_codes:
            abort(404)
        return Response(assist.dom_codes[res].tobytes(), mimetype="application/octet-stream")

    @app.route("/api/assist/flood", methods=["POST"])
    def assist_flood():
        s = sess()
        body = request.get_json(silent=True) or {}
        try:
            seed = int(body["seed"])
        except (KeyError, TypeError, ValueError):
            abort(400, "seed must be a cell index")
        if not 0 <= seed < pack.n:
            abort(400, "seed is not a cell")
        kind = body.get("kind", "wand")
        t0 = time.perf_counter()
        if kind == "wand":
            classes = layer_codes(s, body.get("layer"))
            names = {v["code"]: v["name"] for v in s.vocab}
            label = names.get(int(classes[seed]), "not labelled")
        elif kind == "compartment":
            classes = assist.compartments((num(body, "panck", 0, -5, 5), num(body, "cd45", 0, -5, 5)))
            if classes is None:
                abort(400, "this pack has no PanCK / CD45 stains")
            label = COMP_NAMES[int(classes[seed])]
        elif kind == "novae":
            res = body.get("res")
            if res not in assist.dom_codes:
                abort(400, "this pack has no Novae domains at that resolution")
            classes = assist.dom_codes[res]
            label = assist.dom_names[res][int(classes[seed])]
        else:
            abort(400, "kind must be wand, compartment or novae")
        gap = num(body, "gap", assist.default_gap, 5, 60)
        within = cell_list(body["within"]) if body.get("within") is not None else None
        cells = assist.flood(classes, seed, gap, int(num(body, "jump", 0, 0, 2)), within)
        return jsonify({"cells": cells.tolist(), "label": label, "kind": kind, "gap": gap,
                        "ms": round(1000 * (time.perf_counter() - t0), 1)})

    @app.route("/api/assist/walker", methods=["POST"])
    def assist_walker():
        s = sess()
        body = request.get_json(silent=True) or {}
        sel, ins, outs = cell_list(body.get("selection")), cell_list(body.get("inside")), cell_list(body.get("outside"))
        if not ins.size or not outs.size:
            abort(400, "place at least one inside and one outside seed")
        labels = layer_codes(s, body.get("layer"))
        cells, ms = timed(assist.walker, sel, ins.tolist(), outs.tolist(), labels,
                          num(body, "gap", assist.default_gap, 5, 60), num(body, "beta", 2.0, 0.01, 50),
                          shift=(num(body, "panck", 0, -5, 5), num(body, "cd45", 0, -5, 5)))
        return jsonify({"cells": np.unique(cells).tolist(), "ms": ms})

    def queue_state(s, kind):
        q = assist.queue("both" if kind == "both" else "any", s.resolve_table()[:256].astype(np.int64))
        return q, assist.queue_done(q, s.active_ops(), s.op_cells)

    @app.route("/api/assist/queue")
    def assist_queue():
        s = sess()
        kind = request.args.get("kind", "any")
        if kind not in ("any", "both"):
            abort(400, "kind must be any or both")
        (q, done), ms = timed(queue_state, s, kind)
        items = [{k: it[k] for k in ("id", "section", "n", "score", "both_pct", "domain", "confusion", "bbox_px")} | {"done": bool(d)}
                 for it, d in zip(q["items"], done)]
        return jsonify({"kind": kind, "items": items, "n_done": int(done.sum()), "ms": ms})

    @app.route("/api/assist/queue/<kind>/<int:item>")
    def assist_queue_item(kind, item):
        if kind not in ("any", "both"):
            abort(404)
        q, _ = queue_state(sess(), kind)
        if not 0 <= item < len(q["items"]):
            abort(404)
        return jsonify({"cells": q["items"][item]["cells"].tolist()})

    # ---------- comments, annotations, vocabulary
    def item_routes(kind):
        def upsert():
            s = sess()
            body = request.get_json()
            item = dict(body["item"])
            if "geometry" in item:
                item["geometry"] = geometry_in(item["geometry"])
            item.pop("px", None)
            try:
                return jsonify(geometry_out(s.upsert(kind, item)))
            except KeyError:
                abort(404)

        def delete(item_id):
            sess().delete(kind, item_id)
            return jsonify({"deleted": item_id})

        app.add_url_rule(f"/api/{kind}", f"{kind}_upsert", upsert, methods=["POST"])
        app.add_url_rule(f"/api/{kind}/<item_id>", f"{kind}_delete", delete, methods=["DELETE"])

    item_routes("comments")
    item_routes("annotations")

    @app.route("/api/vocab", methods=["POST"])
    def vocab():
        s = sess()
        try:
            s.vocab_action(request.get_json()["action"])
        except (ValueError, KeyError) as e:
            abort(400, str(e))
        return jsonify({"vocab": s.vocab})

    # ---------- save
    @app.route("/api/save", methods=["POST"])
    def save():
        s = sess()
        body = request.get_json() or {}
        out, final, root = export.write_results(pack, s, results_root)
        key = s.dir.name
        save_status[key] = {"dir": str(out), "image": "skipped", "progress": 0}
        if body.get("image", True):
            st = save_status[key]
            st["image"] = "running"

            def run():
                try:
                    export.slide_image(pack, s, final, root, out / "corrected_slide.tif", st,
                                       max_dim=body.get("max_dim"))
                    st["image"] = "done"
                except Exception as e:
                    st["image"] = "failed"
                    st["error"] = f"{e}"
                    traceback.print_exc()

            threading.Thread(target=run, daemon=True).start()
        return jsonify(save_status[key])

    @app.route("/api/save/status")
    def save_state():
        return jsonify(save_status.get(sess().dir.name, {}))

    @app.errorhandler(400)
    def bad(e):
        return jsonify({"error": e.description}), 400

    return app
