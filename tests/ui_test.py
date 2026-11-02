#!/usr/bin/env python3
"""Drive the PathQC UI headless on the synthetic demo pack and check the outcomes through the API.

  python tests/ui_test.py --exe source --shots /tmp/shots              # run.py from this checkout
  python tests/ui_test.py --exe dist/PathQC/PathQC --shots /tmp/shots  # a frozen build
  options: --browser chromium|webkit  --port 8020  --only A,C  --headed

One function per build step (test_A, test_C, ...). Exit 0 only if every step passes.
"""
import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import traceback
import urllib.request
from pathlib import Path

import numpy as np
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
SRC = ["bill_eugene_code", "vestrum", "claude_code"]


class App:
    """The app under test, started in a temp folder, plus API helpers."""

    def __init__(self, exe, port):
        self.port, self.base = port, f"http://127.0.0.1:{port}"
        self.tmp = Path(tempfile.mkdtemp(prefix="pathqc_ui_"))
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                raise RuntimeError(f"port {port} is busy")
        if exe == "source":
            self.pack = self.tmp / "demo"
            if (ROOT / "demo_pack" / "manifest.json").exists():
                shutil.copytree(ROOT / "demo_pack", self.pack)
            else:
                subprocess.run([sys.executable, str(ROOT / "pack" / "make_demo_pack.py"), "--out", str(self.pack)], check=True)
            cmd = [sys.executable, str(ROOT / "run.py"), "--pack", str(self.pack), "--results", str(self.tmp / "results")]
            env = dict(os.environ)
        else:
            self.pack = self.tmp / "Documents" / "PathQC" / "packs" / "demo"
            cmd = [str(Path(exe).resolve())]
            env = dict(os.environ, HOME=str(self.tmp), USERPROFILE=str(self.tmp))
        self.log = open(self.tmp / "app.log", "w")
        self.proc = subprocess.Popen(cmd + ["--no-browser", "--port", str(port)], env=env, cwd=ROOT,
                                     stdout=self.log, stderr=subprocess.STDOUT)
        for _ in range(240):
            try:
                urllib.request.urlopen(self.base + "/", timeout=2)
                break
            except Exception:
                if self.proc.poll() is not None:
                    raise RuntimeError("app exited: " + (self.tmp / "app.log").read_text()[-3000:])
                time.sleep(0.5)
        else:
            raise RuntimeError("app did not start")
        buf = self.get("/api/cells.bin", raw=True)
        n = int(np.frombuffer(buf, np.uint32, 1)[0])
        o = 4
        self.px = np.frombuffer(buf, np.float32, n, o).astype(np.float64); o += 4 * n
        self.py = np.frombuffer(buf, np.float32, n, o).astype(np.float64); o += 4 * n
        self.sec = np.frombuffer(buf, np.uint8, n, o); o += n
        self.src = {k: np.frombuffer(buf, np.uint8, n, o + j * n) for j, k in enumerate(SRC)}
        self.n = n

    def get(self, path, raw=False):
        with urllib.request.urlopen(self.base + path, timeout=60) as r:
            d = r.read()
        return d if raw else json.loads(d)

    def post(self, path, body):
        req = urllib.request.Request(self.base + path, json.dumps(body).encode(), {"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read())

    def final(self, reviewer):
        b = self.get(f"/api/final.bin?reviewer={reviewer}", raw=True)
        return np.frombuffer(b[:2 * self.n], np.uint16).copy(), np.frombuffer(b[2 * self.n:], np.uint8).copy()

    def ops_file(self, reviewer):
        return self.pack / "sessions" / reviewer / "ops.jsonl"

    def stop(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print("   ok:", msg)


def open_ui(page, app, reviewer):
    page.goto(f"{app.base}/?reviewer={reviewer}")
    page.wait_for_function("window.PathQC && PathQC.S.final && PathQC.S.panels.length === 5 && PathQC.S.panels.every(P => P.viewer.world.getItemAt(0))", timeout=60000)
    page.wait_for_timeout(600)


def go_section(page, k):
    page.select_option("#secJump", str(k))
    page.wait_for_timeout(900)


def wait_ops(page, n):
    page.wait_for_function(f"PathQC.S.ops.length === {n}", timeout=20000)
    page.wait_for_timeout(250)


def shot(page, shots, name):
    p = shots / f"{name}.png"
    page.screenshot(path=str(p))
    print("   shot:", p)


def client(page, k, x, y):
    return page.evaluate(f"PathQC.toClient({k}, {x}, {y})")


def sel_list(page):
    return set(page.evaluate("Array.from(PathQC.S.selList)"))


def in_poly(px, py, poly):
    poly = np.asarray(poly, float)
    inside = np.zeros(px.shape, bool)
    j = len(poly) - 1
    for i in range(len(poly)):
        xi, yi = poly[i]; xj, yj = poly[j]
        with np.errstate(divide="ignore", invalid="ignore"):
            cross = ((yi > py) != (yj > py)) & (px < (xj - xi) * (py - yi) / (yj - yi) + xi)
        inside ^= cross
        j = i
    return inside


def dist_to_polyline(px, py, pts):
    pts = np.asarray(pts, float)
    if len(pts) == 1:
        return np.hypot(px - pts[0, 0], py - pts[0, 1])
    d = np.full(px.shape, np.inf)
    for a, b in zip(pts[:-1], pts[1:]):
        ab = b - a
        t = np.clip(((px - a[0]) * ab[0] + (py - a[1]) * ab[1]) / max(ab @ ab, 1e-12), 0, 1)
        d = np.minimum(d, np.hypot(px - a[0] - t * ab[0], py - a[1] - t * ab[1]))
    return d


def geoms(page):
    return page.evaluate("PathQC.S.selGeoms")


# ------------------------------------------------------------------------------------------- step A
def test_A(page, app, shots):
    """Start card -> pick per section / all sections; Restart from…; Clear (section, all) + Undo."""
    R = "tester_a"
    open_ui(page, app, R)
    secs = app.get(f"/api/state?reviewer={R}")["manifest"]["sections"]
    s1, s2 = app.sec == 0, app.sec == 1
    bill = app.src["bill_eugene_code"]
    check(page.locator("#clearBtn").count() == 0, "no separate 'clear selection' button in the toolbar")
    check(page.get_by_role("button", name="Clear", exact=True).count() == 1,
          "exactly one button is called Clear (on the layer panel)")

    go_section(page, 0)
    card = page.locator("#startCard")
    check(card.is_visible(), "start card shown on an empty layer")
    t = card.inner_text()
    check(f"for {secs[0]}" in t and "in-sample" in t, f"card names {secs[0]} and shows the dev flag")
    shot(page, shots, "A1_start_card_S1")

    bb = card.locator("[data-start=vestrum]").bounding_box()
    for _ in range(3):                                   # a fast triple click sends one op
        page.mouse.click(bb["x"] + 8, bb["y"] + 8)
    wait_ops(page, 1)
    page.wait_for_timeout(500)
    check(len(app.get(f"/api/state?reviewer={R}")["ops"]) == 1, "fast repeated clicks on the start card log one op")
    final, tag = app.final(R)
    v = app.src["vestrum"]
    ok = s1 & (v != 255)
    check(np.array_equal(final[ok], v[ok]) and (tag[ok] == 3).all(), "S1 final labels == Vestrum labels (final.bin)")
    check(np.array_equal(final[s2], bill[s2]) and (tag[s2] == 0).all(), "S2 untouched by the S1 pick")
    check(not card.is_visible(), "start card hidden once S1 has a pick")
    shot(page, shots, "A2_S1_vestrum")

    go_section(page, 1)
    check(card.is_visible() and f"for {secs[1]}" in card.inner_text(), "card returns for the empty section S2")
    check("in-sample" not in card.inner_text(), "no dev flag on the held-out section")
    card.locator("#startAll").check()
    card.locator("[data-start=claude_code]").click()
    wait_ops(page, 2)
    final, tag = app.final(R)
    c = app.src["claude_code"]
    check(np.array_equal(final[c != 255], c[c != 255]) and (tag[c != 255] == 4).all(), "apply to all sections: every section = Claude Code")
    ops = app.get(f"/api/state?reviewer={R}")["ops"]
    check(ops[-1]["scope"] == "all" and ops[0]["scope"] == "section" and ops[0]["sections"] == [secs[0]],
          "ops logged with their section scope")
    lines = app.ops_file(R).read_text().splitlines()
    check(all("cells" not in json.loads(l) and len(l) < 2000 for l in lines),
          f"whole-section ops store their sections, not cell lists ({max(map(len, lines))} bytes/line)")

    page.click("#restartBtn")
    page.locator("#ctx .mi", has_text="Bill_eugene_code labels").first.click()
    wait_ops(page, 3)
    final, tag = app.final(R)
    check(np.array_equal(final[s2], bill[s2]) and (tag[s2] == 2).all(), "Restart from… Bill_eugene_code for S2")
    check((tag[s1 & (c != 255)] == 4).all(), "S1 keeps its Claude pick")

    app.post("/api/comments", {"reviewer": R, "item": {"text": "keep me", "category": "Other", "targets": [],
                                                        "geometry": {"type": "Point", "px": [[float(app.px[0]), float(app.py[0])]]}}})
    page.click("#layerClearBtn")
    page.locator("#ctx .mi", has_text="this section").click()
    wait_ops(page, 4)
    final, tag = app.final(R)
    check((tag[s2] == 0).all() and np.array_equal(final[s2], bill[s2]), "Clear S2: layer empty for S2")
    check((tag[s1 & (c != 255)] == 4).all(), "Clear S2 leaves S1 alone")
    page.wait_for_timeout(300)
    check(card.is_visible() and f"for {secs[1]}" in card.inner_text(), "start card back after Clear")
    shot(page, shots, "A3_S2_cleared")

    page.click("#undoBtn")
    wait_ops(page, 3)
    final, tag = app.final(R)
    check((tag[s2] == 2).all(), "Undo restores S2 (Bill_eugene_code picked)")
    page.wait_for_timeout(300)
    check(not card.is_visible(), "start card hidden again after Undo")

    page.click("#layerClearBtn")
    page.locator("#ctx .mi", has_text="all sections").click()
    wait_ops(page, 4)
    final, tag = app.final(R)
    st = app.get(f"/api/state?reviewer={R}")
    check((tag == 0).all() and np.array_equal(final, bill.astype(np.uint16)), "Clear all sections: layer empty everywhere")
    check(len(st["comments"]) == 1, "comments survive Clear")
    card.locator("[data-start=vestrum]").click()        # an op after the clear still applies
    wait_ops(page, 5)
    final, tag = app.final(R)
    check((tag[s2 & (v != 255)] == 3).all() and (tag[s1] == 0).all(), "pick after Clear applies only where made")
    page.keyboard.press("Control+z")
    wait_ops(page, 4)
    page.keyboard.press("Control+z")
    wait_ops(page, 3)
    final, tag = app.final(R)
    check((tag[s2] == 2).all() and (tag[s1 & (c != 255)] == 4).all(), "Undo of Clear-all restores every section")
    shot(page, shots, "A4_after_undo")

    # a log written before section ops were stored by scope (explicit cells) still replays
    L = "tester_a_legacy"
    d = app.pack / "sessions" / L
    d.mkdir(parents=True)
    idx = np.nonzero(s1)[0]
    (d / "ops.jsonl").write_text(json.dumps({"op": "base", "source": "vestrum", "mm": [[0, 0], [1, 0], [1, 1]], "cells": idx.tolist(),
                                             "n": int(idx.size), "scope": "section", "sections": [secs[0]], "id": "legacy0001",
                                             "reviewer": L, "ts": "2026-11-01T00:00:00"}) + "\n")
    final, tag = app.final(L)
    check((tag[s1 & (v != 255)] == 3).all() and (tag[~s1] == 0).all(), "legacy section op with a cell list replays")


# ------------------------------------------------------------------------------------------- step C
def test_C(page, app, shots):
    """Selection tools with add/subtract, composition, class-filtered relabel, number keys, Enter, Esc."""
    R = "tester_c"
    open_ui(page, app, R)
    go_section(page, 0)
    page.locator("#startCard [data-start=bill_eugene_code]").click()      # start S1 from Bill
    wait_ops(page, 1)
    page.wait_for_timeout(300)
    b = page.evaluate("PathQC.S.secBoxes[0]")
    W, H = b["x1"] - b["x0"], b["y1"] - b["y0"]
    X = lambda f: b["x0"] + f * W
    Y = lambda f: b["y0"] + f * H
    M = 1                                        # draw on the reviewer-layer panel

    def drag(pts, mods=(), steps=6):
        for k in mods:
            page.keyboard.down(k)
        x, y = client(page, M, *pts[0])
        page.mouse.move(x, y)
        page.mouse.down()
        for p in pts[1:]:
            x, y = client(page, M, *p)
            page.mouse.move(x, y, steps=steps)
        page.mouse.up()
        for k in mods:
            page.keyboard.up(k)
        page.wait_for_timeout(150)

    def rect_expect(g):
        xs, ys = [p[0] for p in g["px"]], [p[1] for p in g["px"]]
        return set(np.nonzero(in_poly(app.px, app.py, g["px"]))[0]) if g["type"] == "Polygon" else set()

    # rectangle, Shift adds, Alt subtracts
    page.keyboard.press("r")
    drag([(X(.05), Y(.1)), (X(.25), Y(.4))])
    s_a = sel_list(page); g = geoms(page)
    exp_a = rect_expect(g[0])
    check(len(s_a) > 50 and s_a == exp_a, f"rectangle selects the cells inside it ({len(s_a)})")
    drag([(X(.2), Y(.3)), (X(.35), Y(.55))], mods=["Shift"])
    s_b = sel_list(page); exp_b = rect_expect(geoms(page)[1])
    check(s_b == exp_a | exp_b and len(s_b) > len(s_a), f"Shift+rectangle adds ({len(s_a)} -> {len(s_b)})")
    drag([(X(.1), Y(.15)), (X(.3), Y(.35))], mods=["Alt"])
    s_c = sel_list(page); exp_c = rect_expect(geoms(page)[2])
    check(s_c == (exp_a | exp_b) - exp_c and len(s_c) < len(s_b), f"Alt+rectangle subtracts ({len(s_b)} -> {len(s_c)})")
    shot(page, shots, "C1_rect_add_subtract")

    # composition summary in the popover
    lst = np.array(sorted(s_c))
    txt = page.locator("#selComp").inner_text()
    vals, cnt = np.unique(app.src["bill_eugene_code"][lst], return_counts=True)
    pct = round(100 * cnt.max() / len(lst))
    check(txt.startswith(f"{len(lst):,} cells: Bill {pct}%"), f"composition line: '{txt}'")

    # Esc deselects
    page.keyboard.press("Escape")
    check(len(sel_list(page)) == 0 and page.locator("#pop").is_hidden(), "Esc deselects and closes the popover")

    # lasso (freehand), then Shift+lasso from Pan
    page.keyboard.press("l")
    inked = "(() => { const c = PathQC.S.panels[1].cv, d = c.getContext('2d').getImageData(0, 0, c.width, c.height).data; let n = 0; for (let i = 0; i < d.length; i += 16) if (d[i + 3] > 0) n++; return n; })()"
    ink0 = page.evaluate(inked)
    x, y = client(page, M, X(.6), Y(.5)); page.mouse.move(x, y); page.mouse.down()
    for k in range(1, 8):
        page.mouse.move(x + 6 * k, y + 3 * k)
    ink1 = page.evaluate(inked)
    page.keyboard.press("Escape"); page.mouse.up(); page.wait_for_timeout(150)
    check(ink0 > 1000 and ink1 >= 0.95 * ink0, f"cells stay drawn while a lasso is traced (cached layer: {ink0} -> {ink1} px)")
    check(page.evaluate("PathQC.S.draw") is None and len(sel_list(page)) == 0, "Esc mid-lasso drops the shape")
    circ = [(X(.15 + .1 * np.cos(t)), Y(.4 + .15 * np.sin(t))) for t in np.linspace(0, 2 * np.pi, 24)]
    drag(circ, steps=3)
    s = sel_list(page); g = geoms(page)[0]
    check(len(s) > 30 and s == set(np.nonzero(in_poly(app.px, app.py, g["px"]))[0]), f"lasso selects the cells inside ({len(s)})")
    page.keyboard.press("v")
    circ2 = [(X(.3 + .06 * np.cos(t)), Y(.4 + .1 * np.sin(t))) for t in np.linspace(0, 2 * np.pi, 20)]
    drag(circ2, mods=["Shift"], steps=3)
    s2 = sel_list(page); g2 = geoms(page)[1]
    check(s2 == s | set(np.nonzero(in_poly(app.px, app.py, g2["px"]))[0]) and len(s2) > len(s), f"Shift+drag in Pan adds a lasso ({len(s)} -> {len(s2)})")
    shot(page, shots, "C2_lasso")
    page.keyboard.press("Escape")

    # polygon: Enter closes; Shift + polygon closed by double-click
    page.keyboard.press("p")
    for p in [(X(.05), Y(.6)), (X(.3), Y(.6)), (X(.18), Y(.9))]:
        page.mouse.click(*client(page, M, *p)); page.wait_for_timeout(400)
    page.keyboard.press("Enter")
    page.wait_for_timeout(150)
    s = sel_list(page); g = geoms(page)[0]
    check(len(g["px"]) == 3 and len(s) > 20 and s == set(np.nonzero(in_poly(app.px, app.py, g["px"]))[0]), f"polygon + Enter selects the triangle ({len(s)})")
    page.keyboard.down("Shift")
    pts = [(X(.32), Y(.6)), (X(.45), Y(.6)), (X(.45), Y(.85))]
    for p in pts[:-1]:
        page.mouse.click(*client(page, M, *p)); page.wait_for_timeout(400)
    page.mouse.dblclick(*client(page, M, *pts[-1]))
    page.keyboard.up("Shift")
    page.wait_for_timeout(150)
    s2 = sel_list(page); g2 = geoms(page)
    check(len(g2) == 2 and len(g2[1]["px"]) == 3 and s2 == s | set(np.nonzero(in_poly(app.px, app.py, g2[1]["px"]))[0]),
          f"Shift+polygon closed by double-click adds ({len(s)} -> {len(s2)})")
    shot(page, shots, "C3_polygon")
    page.keyboard.press("Escape")

    # brush: radius in screen px; Alt+brush subtracts; ] grows the brush
    page.keyboard.press("b")
    page.keyboard.press("]")
    check(page.evaluate("PathQC.S.brushPx") > 18, "] enlarges the brush")
    drag([(X(.05), Y(.3)), (X(.2), Y(.32)), (X(.35), Y(.3))], steps=12)
    s = sel_list(page); g = geoms(page)[0]
    d = dist_to_polyline(app.px, app.py, g["px"])
    inner, outer = set(np.nonzero(d <= 0.85 * g["r_px"])[0]), set(np.nonzero(d <= g["r_px"] + 1)[0])
    check(len(s) > 20 and inner <= s <= outer, f"brush selects the cells under the stroke ({len(s)})")
    drag([(X(.2), Y(.2)), (X(.2), Y(.4))], mods=["Alt"], steps=8)
    s2 = sel_list(page); g2 = geoms(page)[1]
    d2 = dist_to_polyline(app.px, app.py, g2["px"])
    removed = s - s2
    check(s2 < s and removed and all(d2[i] <= g2["r_px"] + 1 for i in removed), f"Alt+brush subtracts ({len(s)} -> {len(s2)})")
    page.mouse.move(*client(page, M, X(.5), Y(.5)))
    shot(page, shots, "C4_brush")
    page.keyboard.press("Escape")

    # single cells: click, Shift+click adds, Alt+click removes
    page.keyboard.press("s")
    from scipy.spatial import cKDTree
    on = np.nonzero(app.sec == 0)[0]
    dnn = cKDTree(np.column_stack([app.px[on], app.py[on]])).query(np.column_stack([app.px[on], app.py[on]]), k=2)[0][:, 1]
    iso = on[dnn > 4 * dnn.mean()] if (dnn > 4 * dnn.mean()).sum() >= 2 else on[np.argsort(-dnn)[:2]]
    i0, i1 = int(iso[0]), int(iso[-1])
    page.mouse.click(*client(page, M, app.px[i0], app.py[i0]))
    check(sel_list(page) == {i0}, "cell tool selects one cell")
    page.keyboard.down("Shift"); page.mouse.click(*client(page, M, app.px[i1], app.py[i1])); page.keyboard.up("Shift")
    check(sel_list(page) == {i0, i1}, "Shift+click adds a cell")
    page.keyboard.down("Alt"); page.mouse.click(*client(page, M, app.px[i0], app.py[i0])); page.keyboard.up("Alt")
    check(sel_list(page) == {i1}, "Alt+click removes a cell")
    page.keyboard.press("Escape")

    # class-filtered relabel: dominant class pre-ticked, number key + Enter
    page.keyboard.press("r")
    page.focus("#secJump")                       # a focused dropdown must not swallow the keys after a draw
    drag([(X(.05), Y(.05)), (X(.4), Y(.95))])
    check(page.evaluate("document.activeElement.id") != "secJump", "drawing on a panel takes the focus off the Go to dropdown")
    sel = np.array(sorted(sel_list(page)))
    before, _ = app.final(R)
    vals, cnt = np.unique(before[sel], return_counts=True)
    dom = int(vals[cnt.argmax()])
    boxes = page.locator("#pop .classes input")
    checked = [int(boxes.nth(k).get_attribute("data-c")) for k in range(boxes.count()) if boxes.nth(k).is_checked()]
    check(boxes.count() >= 3 and checked == [dom], f"classes listed ({boxes.count()}), only the dominant one ticked")
    target = int(page.locator("#pop .qk").nth(2).get_attribute("data-q"))
    if target == dom:
        target = int(page.locator("#pop .qk").nth(3).get_attribute("data-q")); key = "4"
    else:
        key = "3"
    page.keyboard.press(key)
    check(page.evaluate("PathQC.S.target") == target, f"number key {key} chooses the target label")
    shot(page, shots, "C5_class_filter_popover")
    page.keyboard.press("Enter")
    wait_ops(page, 2)
    after, tag = app.final(R)
    hit = sel[before[sel] == dom]
    other = np.setdiff1d(np.arange(app.n), hit)
    check((after[hit] == target).all() and (tag[hit] == 1).all(), f"Enter relabels the {len(hit)} ticked-class cells")
    check(np.array_equal(after[other], before[other]), "unticked classes and unselected cells unchanged")
    op = json.loads(app.ops_file(R).read_text().splitlines()[-1])
    check(op["op"] == "relabel" and sorted(op["cells"]) == sorted(hit.tolist()) and op["geoms"][0]["type"] == "Polygon"
          and op["filter_ref"] == "corr" and len(op["filter_names"]) == 1 and abs(op["geoms"][0]["mm"][0][0]) < 1e3,
          "op carries the explicit cells, the filter and the geometry in mm")

    # tick a different class by hand, untick the pre-ticked one, choose the label in the dropdown
    page.wait_for_timeout(200)
    boxes = page.locator("#pop .classes input")
    codes = [int(boxes.nth(k).get_attribute("data-c")) for k in range(boxes.count())]
    second = codes[1]
    for k, cde in enumerate(codes):
        if boxes.nth(k).is_checked():
            boxes.nth(k).uncheck()
    boxes.nth(1).check()
    before = after
    to2 = [c for c in page.evaluate("PathQC.S.vocab.map(v => v.code)") if c not in (second, target)][0]
    page.select_option("#pop [data-a=to]", str(to2))
    page.locator("#pop [data-a=relabel]").click()
    wait_ops(page, 3)
    after, _ = app.final(R)
    hit2 = sel[before[sel] == second]
    other = np.setdiff1d(np.arange(app.n), hit2)
    check(len(hit2) > 0 and (after[hit2] == to2).all() and np.array_equal(after[other], before[other]),
          f"only the hand-ticked class changes ({len(hit2)} cells)")

    # reference panel: tick by Vestrum's labels
    page.select_option("#pop [data-a=ref]", "vestrum")
    page.wait_for_timeout(150)
    v = app.src["vestrum"]
    vv, vc = np.unique(v[sel], return_counts=True)
    vdom = int(vv[vc.argmax()])
    check(page.evaluate("[...PathQC.S.ticked]") == [vdom], "reference Vestrum: its dominant class is pre-ticked")
    before = after
    page.keyboard.press("1")
    t1 = page.evaluate("PathQC.S.target")
    page.keyboard.press("Enter")
    wait_ops(page, 4)
    after, _ = app.final(R)
    hit3 = sel[v[sel] == vdom]
    check((after[hit3] == t1).all() and np.array_equal(np.delete(after, hit3), np.delete(before, hit3)),
          f"relabel filtered by Vestrum's classes ({len(hit3)} cells)")

    # base pick for the selection, then Undo
    before = after
    page.locator("#pop [data-base=claude_code]").click()
    wait_ops(page, 5)
    after, tag = app.final(R)
    c = app.src["claude_code"]
    ok = sel[c[sel] != 255]
    check((after[ok] == c[ok]).all() and (tag[ok] == 4).all(), "Use Claude Code labels for the selected cells")
    shot(page, shots, "C6_after_base_pick")
    page.keyboard.press("Control+z")
    wait_ops(page, 4)
    after, _ = app.final(R)
    check(np.array_equal(after, before), "Undo reverts the selection base pick")
    page.keyboard.press("Escape")
    check(len(sel_list(page)) == 0, "Esc deselects")
    shot(page, shots, "C7_final")


STEPS = {"A": test_A, "C": test_C}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--exe", default="source")
    ap.add_argument("--shots", default="ui_shots")
    ap.add_argument("--browser", default="chromium", choices=["chromium", "webkit", "firefox"])
    ap.add_argument("--port", type=int, default=8020)
    ap.add_argument("--only", default=None, help="comma-separated steps, e.g. A,C")
    ap.add_argument("--headed", action="store_true")
    args = ap.parse_args()
    shots = Path(args.shots); shots.mkdir(parents=True, exist_ok=True)
    steps = args.only.split(",") if args.only else list(STEPS)
    app = App(args.exe, args.port)
    failed = []
    try:
        with sync_playwright() as pw:
            browser = getattr(pw, args.browser).launch(headless=not args.headed)
            for name in steps:
                print(f"== step {name}")
                page = browser.new_page(viewport={"width": 1600, "height": 1000})
                errors = []
                page.on("pageerror", lambda e: errors.append(str(e)))
                try:
                    STEPS[name](page, app, shots)
                    check(not errors, f"no page errors {errors[:3]}")
                    print(f"PASS step {name}")
                except Exception as e:
                    traceback.print_exc()
                    try:
                        page.screenshot(path=str(shots / f"FAIL_{name}.png"))
                    except Exception:
                        pass
                    print(f"FAIL step {name}: {e}  page errors: {errors[:3]}")
                    failed.append(name)
                page.close()
            browser.close()
    finally:
        app.stop()
    if failed:
        print("app log tail:\n" + (app.tmp / "app.log").read_text()[-3000:])
    print("RESULT:", "FAIL " + ",".join(failed) if failed else "PASS all")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
