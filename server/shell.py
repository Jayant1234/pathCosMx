"""The app shell: start screen + setup API around the review app (server/app.py), which it rebuilds when the
pack or the slide image changes.

  /                 start screen until set up, then the viewer (/?reviewer=<name>)
  /start            reviewer name, slide pack, H&E (optional) -> registration -> accept
  /api/setup/...    the start screen's API (below)
  /tiles/...        H&E Deep Zoom tiles from the H&E file when the H&E is the slide image
  everything else   the review app for the current pack and image

Choices persist in the user's settings file (server/settings.py), so the next start goes straight to the viewer.
An accepted registration is written to <pack>/he/<fingerprint id>/transform.json (the stand-in's
<pack>/transform.json is never touched) and recorded in the settings per (pack, H&E fingerprint).
"""
import io
import json
import re
import sys
import threading
import traceback
from pathlib import Path
from urllib.parse import quote, urlsplit

import numpy as np
from flask import Flask, Response, abort, jsonify, redirect, request, send_from_directory

from . import picker, settings as S
from .app import WEB, create_app

LOOPBACK = {"127.0.0.1", "localhost", "::1"}
HE_TILE = re.compile(r"^/tiles/(he_[0-9a-f]+)(\.dzi|_files/(\d+)/(\d+)_(\d+)\.jpeg)$")


def ensure_standin(pack):
    if not (pack / "transform.json").exists() or not (pack / "image" / "slide.dzi").exists():
        from pack.make_standin import build
        print(f"building the stand-in image for {pack} (first start only)…", flush=True)
        build(pack)


def viewer_url(name):
    return "/?reviewer=" + quote(name or "", safe="")


def guard(environ, hosts):
    """-> None, or why a request is refused. A local app must not answer a web page from elsewhere:
    the Host must be this machine (DNS rebinding) and a state-changing request must come from our own page
    (cross-site POST). `hosts` None = any Host (served on a network address on purpose)."""
    host = environ.get("HTTP_HOST", "")
    if hosts is not None and (urlsplit("//" + host).hostname or "") not in hosts:
        return f"host {host!r} is not allowed"
    if environ.get("REQUEST_METHOD", "GET") in ("GET", "HEAD", "OPTIONS"):
        return None
    if environ.get("HTTP_SEC_FETCH_SITE", "same-origin") not in ("same-origin", "none"):
        return "cross-site request refused"
    origin = environ.get("HTTP_ORIGIN")
    if origin is not None and urlsplit(origin).netloc != host:
        return f"origin {origin!r} refused"
    return None


def pack_summary(pack):
    m = json.loads((pack / "manifest.json").read_text())
    return {"path": str(pack), "name": m.get("pack", pack.name), "dataset": m.get("dataset", ""),
            "n_cells": m.get("n_cells"), "demo": bool(m.get("demo")), "sections": m.get("sections", [])}


class Shell:
    def __init__(self, pack, results=None, cli_transform=None, pack_from_cli=False):
        self.results, self.cli_transform = results, cli_transform
        self.lock = threading.RLock()
        self.job = {"state": "idle"}
        self.candidate = None          # last registration result (auto or anchors), not yet accepted
        self.he = None                 # {"info", "fingerprint", "slide"} of the H&E being set up
        self.tiler = None
        st = S.load()
        saved = Path(st["pack"]) if st.get("pack") else None
        if not pack_from_cli and saved and (saved / "manifest.json").exists():
            pack = saved
        self.problem = None
        self.switch(Path(pack))

    # ---------- current state
    def entry(self, st=None):
        return S.pack_entry(st if st is not None else S.load(), self.pack)

    def switch(self, pack):
        """Load `pack` with the image the settings choose for it (stand-in when the H&E is not usable)."""
        with self.lock:
            pack = Path(pack).resolve()
            ensure_standin(pack)
            problem, transform = None, self.cli_transform
            e = S.pack_entry(S.load(), pack)
            if not transform and e.get("image") == "he":
                ok, problem = S.check_he(e.get("he"))
                tf = S.load().get("registrations", {}).get(S.reg_key(pack, e["he"]["fingerprint"]["id"])) if ok else None
                if ok and tf and Path(tf).exists():
                    transform = tf
                elif ok:
                    problem = "The H&E is not registered to this slide yet."
            inner = create_app(pack, self.results, transform)          # raises on a bad pack: keep the current one
            self.pack, self.problem, self.transform_path, self.inner = pack, problem, transform, inner
            self.tr = json.loads(Path(transform).read_text()) if transform else None
            self.tiler = None    # not closed here: tile requests in flight still hold it; its slide closes when released
            if self.tr and self.tr.get("kind") == "he" and self.tr["image"].get("reader"):   # ours: tiles from the file
                from .he import Tiler
                self.tiler = Tiler(self.tr["image"]["base"])
                self.tiler.name = Path(self.tr["image"]["dzi"]).stem

    def ready(self):
        st = S.load()
        if not st.get("reviewer"):
            return False
        if self.cli_transform:
            return True
        e = S.pack_entry(st, self.pack)
        return e.get("image") in ("standin", "he") and not self.problem

    def standin_transform(self):
        return json.loads((self.pack / "transform.json").read_text())

    def cells_mm(self):
        from pack.cells_io import read_cells
        c = read_cells(self.pack, columns=["x_mm", "y_mm"])
        return c.x_mm.values.astype(np.float64), c.y_mm.values.astype(np.float64)

    def open_he(self, path):
        from . import he
        f = Path(path).expanduser()
        if not f.is_file():
            raise ValueError(f"No file at {f}")
        if self.job.get("state") == "running":
            raise ValueError("a registration is running on the current H&E; wait for it to finish")
        slide, reader = he.open_slide(f)
        info = he.slide_info(slide, reader, f.resolve())
        fp = S.fingerprint(f)
        with self.lock:
            if self.he:
                self.he["slide"].close()
            self.he = {"info": info, "fingerprint": fp, "slide": slide, "thumbs": {}}
            self.candidate = None
            self.job = {"state": "idle"}
        return info, fp

    def he_thumb(self, size):
        from . import he
        h = self.he
        if size not in h["thumbs"]:
            h["thumbs"][size] = he.thumbnail(h["slide"], size)[0]
        return h["thumbs"][size]

    def state(self):
        st = S.load()
        e = S.pack_entry(st, self.pack)
        he_saved = e.get("he")
        out = {
            "reviewer": st.get("reviewer", ""),
            "settings_file": str(S.path()),
            "pack": pack_summary(self.pack),
            "image": "he" if (self.tr and self.tr.get("kind") == "he") else "standin",
            "chosen": e.get("image"),
            "problem": self.problem,
            "ready": self.ready(),
            "saved_he": he_saved,
            "cli_transform": self.cli_transform,
            "registration": (self.tr or {}).get("registration"),
            "standin": {k: self.standin_transform()["image"][k] for k in ("width", "height")},
            "job": {k: v for k, v in self.job.items() if k != "result"},
            "candidate": self.candidate,
            "exts": list(S.HE_EXTENSIONS),
        }
        if self.he:
            out["he"] = dict(self.he["info"], fingerprint=self.he["fingerprint"])
            key = S.reg_key(self.pack, self.he["fingerprint"]["id"])
            out["he"]["registered"] = bool(st.get("registrations", {}).get(key))
        return out

    # ---------- registration
    def run_auto(self):
        from . import register
        h = self.he
        if not h:
            raise ValueError("open an H&E file first")
        with self.lock:
            if self.job.get("state") == "running":
                return
            self.job = {"state": "running", "progress": 0.0, "message": "starting", "log": []}
        job = self.job

        def prog(f, m):
            job["progress"], job["message"] = round(f, 3), m

        def log(m):
            job["log"] = (job["log"] + [m])[-40:]

        def work():
            try:
                x, y = self.cells_mm()
                res = register.auto(h["slide"], x, y, self.standin_transform()["mm_to_px"], mpp=h["info"]["mpp"],
                                    log=log, progress=prog)
                self.candidate = res
                job["state"] = "done"
            except Exception as e:
                traceback.print_exc()
                job["state"], job["message"] = "failed", f"{e}"

        threading.Thread(target=work, daemon=True).start()

    def run_anchors(self, pairs, refine):
        from . import register
        h = self.he
        if not h:
            raise ValueError("open an H&E file first")
        sa = self.standin_transform()["mm_to_px"]
        inv = np.linalg.inv(np.vstack([sa, [0, 0, 1]]))
        pts = []
        for p in pairs:
            mx, my = register.apply(inv, p["standin"][0], p["standin"][1])
            pts.append({"mm": [float(mx), float(my)], "he": [float(v) for v in p["he"]]})
        res = register.anchors(pts, sa, px_unit=max(h["slide"].dimensions) / 1024)   # clicks are on 1024-px overviews
        if refine:
            from . import he
            x, y = self.cells_mm()
            lo, lo_s = he.thumbnail(h["slide"], register.ECC_SIZES[0])
            A, cc = register.ecc_refine(x, y, res["mm_to_px"], lo, lo_s, sigma_um=25.0, log=lambda m: None)
            hi, hi_s = he.thumbnail(h["slide"], min(register.ECC_SIZES[1], max(h["slide"].dimensions)))
            A2, cc2 = register.ecc_refine(x, y, A, hi, hi_s, sigma_um=10.0, log=lambda m: None)
            if register.accept_finer(A, A2, cc2, x, y):
                ref = register.describe(A2, sa, "anchors+refine")
                px, py = register.apply(A2, [p["mm"][0] for p in pts], [p["mm"][1] for p in pts])
                d = np.hypot(px - np.array([p["he"][0] for p in pts]), py - np.array([p["he"][1] for p in pts]))
                ref.update(score=round(cc2, 4), n_pairs=len(pts), anchor_fit=res["summary"],
                           residual_um=[round(float(v * ref["um_per_px"]), 1) for v in d],
                           mean_residual_um=round(float(d.mean() * ref["um_per_px"]), 1), confident=cc2 >= 0.5,
                           status="confident" if cc2 >= 0.5 else "uncertain",
                           reason="" if cc2 >= 0.5 else f"low refinement score (r = {cc2:.2f})")
                ref["summary"] = (f"anchors ({len(pts)}) + refine · {ref['orientation']} · {ref['um_per_px']:.2f} µm/px"
                                  f" · fit r={cc2:.2f}")
                res = ref
            else:
                res["refine_note"] = "automatic refinement did not improve the fit; using the anchor fit"
        self.candidate = res
        return res

    def accept(self):
        from . import register
        h, res = self.he, self.candidate
        if not h or not res:
            raise ValueError("run a registration first")
        fid = h["fingerprint"]["id"]
        d = self.pack / "he" / fid
        d.mkdir(parents=True, exist_ok=True)
        tf = d / "transform.json"
        tf.write_text(json.dumps(register.transform_json(res, h["info"], h["fingerprint"], f"he_{fid}.dzi"), indent=2))
        st = S.load()
        e = S.pack_entry(st, self.pack)
        e.update(image="he", he={"path": h["info"]["path"], "name": h["info"]["name"], "fingerprint": h["fingerprint"]})
        st.setdefault("registrations", {})[S.reg_key(self.pack, fid)] = str(tf)
        S.save(st)
        self.switch(self.pack)
        return str(tf)


def create_shell(pack, results=None, cli_transform=None, pack_from_cli=False, host="127.0.0.1"):
    sh = Shell(pack, results, cli_transform, pack_from_cli)
    hosts = LOOPBACK if host in LOOPBACK else None          # --host 0.0.0.0 etc.: served on the network on purpose
    app = Flask(__name__, static_folder=None)
    app.shell = sh

    def body():
        return request.get_json(silent=True) or {}

    def thumb_size():
        return min(4096, max(64, request.args.get("size", 1024, type=int)))

    def ok(**kw):
        return jsonify(dict(sh.state(), **kw))

    @app.route("/")
    def index():
        if request.args.get("reviewer"):
            return send_from_directory(WEB, "index.html")
        if not sh.ready():
            return redirect("/start")
        return redirect(viewer_url(S.load()["reviewer"]))

    @app.route("/start")
    def start():
        return send_from_directory(WEB, "start.html")

    @app.route("/api/setup/state")
    def state():
        return ok()

    @app.route("/api/setup/reviewer", methods=["POST"])
    def reviewer():
        name = (body().get("name") or "").strip()
        if not name:
            abort(400, "enter your name")
        st = S.load()
        st["reviewer"] = name
        S.save(st)
        return ok()

    @app.route("/api/setup/pick", methods=["POST"])
    def pick():
        if body().get("kind") == "folder":
            return jsonify(picker.pick_folder())
        return jsonify(picker.pick_file(exts=S.HE_EXTENSIONS))

    @app.route("/api/setup/pack", methods=["POST"])
    def set_pack():
        p = Path((body().get("path") or "").strip().strip('"').strip("'")).expanduser()
        if not (p / "manifest.json").exists():
            abort(400, f"{p} is not a slide pack (no manifest.json in it)")
        try:
            sh.switch(p)
        except Exception as e:
            traceback.print_exc()
            abort(400, f"cannot open this pack: {e}")
        st = S.load()
        st["pack"] = str(sh.pack)
        S.save(st)
        sh.candidate = None
        return ok()

    @app.route("/api/setup/he", methods=["POST"])
    def set_he():
        p = (body().get("path") or "").strip().strip('"').strip("'")
        if not p:
            abort(400, "give the path of the H&E file")
        try:
            sh.open_he(p)
        except Exception as e:
            abort(400, f"{e}")
        return ok()

    @app.route("/api/setup/he/saved", methods=["POST"])
    def reopen_saved():
        """Re-open the H&E saved for this pack (e.g. to re-run its registration)."""
        e = sh.entry()
        if not e.get("he"):
            abort(400, "no H&E saved for this pack")
        try:
            sh.open_he(e["he"]["path"])
        except Exception as ex:
            abort(400, f"{ex}")
        return ok()

    @app.route("/api/setup/he/thumb.jpg")
    def he_thumb():
        if not sh.he:
            abort(404)
        from PIL import Image
        a = sh.he_thumb(thumb_size())
        buf = io.BytesIO()
        Image.fromarray(a).save(buf, "JPEG", quality=88)
        return Response(buf.getvalue(), mimetype="image/jpeg")

    @app.route("/api/setup/standin/thumb.jpg")
    def standin_thumb():
        import pyvips
        size = thumb_size()
        f = sh.pack / sh.standin_transform()["image"]["base"]
        return Response(pyvips.Image.thumbnail(str(f), size, height=size).jpegsave_buffer(Q=88), mimetype="image/jpeg")

    @app.route("/api/setup/cells_mm.bin")
    def cells_mm():
        x, y = sh.cells_mm()
        n = int(request.args.get("n", 40000))
        if x.size > n:
            k = np.random.default_rng(0).choice(x.size, n, replace=False)
            x, y = x[k], y[k]
        return Response(np.column_stack([x, y]).astype(np.float32).tobytes(), mimetype="application/octet-stream")

    @app.route("/api/setup/register/auto", methods=["POST"])
    def reg_auto():
        try:
            sh.run_auto()
        except ValueError as e:
            abort(400, f"{e}")
        return ok()

    @app.route("/api/setup/register/anchors", methods=["POST"])
    def reg_anchors():
        b = body()
        try:
            sh.run_anchors(b.get("pairs") or [], bool(b.get("refine", True)))
        except (ValueError, RuntimeError) as e:
            abort(400, f"{e}")
        return ok()

    @app.route("/api/setup/register/accept", methods=["POST"])
    def reg_accept():
        try:
            sh.accept()
        except ValueError as e:
            abort(400, f"{e}")
        return ok(redirect=viewer_url(S.load().get("reviewer")))

    @app.route("/api/setup/standin", methods=["POST"])
    def use_standin():
        st = S.load()
        S.pack_entry(st, sh.pack)["image"] = "standin"
        S.save(st)
        sh.switch(sh.pack)
        return ok(redirect=viewer_url(st.get("reviewer")))

    @app.route("/api/setup/use_he", methods=["POST"])
    def use_saved_he():
        """Use an existing registration: of the H&E just opened (same fingerprint, e.g. a moved file:
        the registration is re-pointed to the new path), else of the H&E saved for this pack."""
        st = S.load()
        e = S.pack_entry(st, sh.pack)
        if sh.he:
            info, fp = sh.he["info"], sh.he["fingerprint"]
            tf = st.get("registrations", {}).get(S.reg_key(sh.pack, fp["id"]))
            if not tf or not Path(tf).exists():
                abort(400, "this H&E is not registered to this pack yet")
            t = json.loads(Path(tf).read_text())
            t["image"]["base"] = info["path"]
            Path(tf).write_text(json.dumps(t, indent=2))
            e["he"] = {"path": info["path"], "name": info["name"], "fingerprint": fp}
        elif not e.get("he"):
            abort(400, "no H&E saved for this pack")
        e["image"] = "he"
        S.save(st)
        sh.switch(sh.pack)
        if sh.problem:
            abort(400, sh.problem)
        return ok(redirect=viewer_url(st.get("reviewer")))

    def he_tiles(m):
        t = sh.tiler
        if not t or m.group(1) != t.name:
            abort(404)
        if m.group(2) == ".dzi":
            return Response(t.dzi(), mimetype="application/xml")
        try:
            data = t.tile(int(m.group(3)), int(m.group(4)), int(m.group(5)))
        except ValueError:
            abort(404)
        return Response(data, mimetype="image/jpeg", headers={"Cache-Control": "max-age=86400"})

    @app.errorhandler(400)
    def bad(e):
        return jsonify({"error": e.description}), 400

    outer = app.wsgi_app

    def dispatch(environ, start_response):
        why = guard(environ, hosts)
        if why:
            start_response("403 Forbidden", [("Content-Type", "text/plain; charset=utf-8")])
            return [f"PathQC: {why}".encode()]
        p = environ.get("PATH_INFO", "")
        if p == "/" or p == "/start" or p.startswith("/api/setup/"):
            return outer(environ, start_response)
        m = HE_TILE.match(p)
        if m and sh.tiler:
            environ["pathqc.tile"] = m
            return outer(environ, start_response)
        return sh.inner(environ, start_response)

    @app.route("/tiles/<path:p>")
    def tiles(p):
        return he_tiles(request.environ["pathqc.tile"])

    app.wsgi_app = dispatch
    return app
