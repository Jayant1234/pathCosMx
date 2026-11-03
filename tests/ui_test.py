#!/usr/bin/env python3
"""Drive the PathQC UI headless on the synthetic demo pack and check the outcomes through the API.

  python tests/ui_test.py --exe source --shots /tmp/shots              # run.py from this checkout
  python tests/ui_test.py --exe dist/PathQC/PathQC --shots /tmp/shots  # a frozen build
  options: --browser chromium|webkit  --port 8020  --only A,C  --headed

One function per build step (test_A, test_B, ...). Exit 0 only if every step passes. Each app runs with a
temp PATHQC_HOME (settings.json) and PATHQC_NO_DIALOG=1. test_B starts its own app on --port + 1 (it restarts it).
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
import urllib.error
import urllib.request
from pathlib import Path

import numpy as np
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
SRC = ["bill_eugene_code", "vestrum", "claude_code"]


class App:
    """The app under test, started in a temp folder, plus API helpers."""

    def __init__(self, exe, port):
        self.exe, self.port, self.base = exe, port, f"http://127.0.0.1:{port}"
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
        # never the real ~/Documents/PathQC/settings.json, never a native dialog; the viewer is opened with
        # ?reviewer=, which bypasses the start screen
        env.update(PATHQC_HOME=str(self.tmp / "Documents" / "PathQC"), PATHQC_NO_DIALOG="1")
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
    s1v, s2c = s1 & (v != 255), s2 & (c != 255)
    check(np.array_equal(final[s2c], c[s2c]) and (tag[s2c] == 4).all(), "apply to all (not started) on S2: S2 = Claude Code")
    check(np.array_equal(final[s1v], v[s1v]) and (tag[s1v] == 3).all(), "apply to all does not overwrite S1, which already has a pick")
    ops = app.get(f"/api/state?reviewer={R}")["ops"]
    check(ops[-1]["scope"] == "section" and ops[-1]["sections"] == [secs[1]] and ops[0]["scope"] == "section" and ops[0]["sections"] == [secs[0]],
          "ops logged with their section scope (only the untouched section)")
    lines = app.ops_file(R).read_text().splitlines()
    check(all("cells" not in json.loads(l) and len(l) < 2000 for l in lines),
          f"whole-section ops store their sections, not cell lists ({max(map(len, lines))} bytes/line)")

    page.click("#restartBtn")
    page.locator("#ctx .mi", has_text="Bill_eugene_code labels").first.click()
    wait_ops(page, 3)
    final, tag = app.final(R)
    check(np.array_equal(final[s2], bill[s2]) and (tag[s2] == 2).all(), "Restart from… Bill_eugene_code for S2")
    check((tag[s1v] == 3).all(), "S1 keeps its Vestrum pick")

    app.post("/api/comments", {"reviewer": R, "item": {"text": "keep me", "category": "Other", "targets": [],
                                                        "geometry": {"type": "Point", "px": [[float(app.px[0]), float(app.py[0])]]}}})
    page.click("#layerClearBtn")
    page.locator("#ctx .mi", has_text="this section").click()
    wait_ops(page, 4)
    final, tag = app.final(R)
    check((tag[s2] == 0).all() and np.array_equal(final[s2], bill[s2]), "Clear S2: layer empty for S2")
    check((tag[s1v] == 3).all(), "Clear S2 leaves S1 alone")
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
    check((tag[s2] == 2).all() and (tag[s1v] == 3).all(), "Undo of Clear-all restores every section")
    shot(page, shots, "A4_after_undo")

    # Restart from… -> all sections overwrites everything, after a confirm that counts the worked sections
    page.click("#restartBtn")
    page.locator("#ctx .mi", has_text="Claude Code labels (all sections)").click()
    page.wait_for_selector("#dlg[open]")
    t = page.locator("#dlg").inner_text()
    check("2 of 2 sections" in t and secs[0] in t and secs[1] in t, f"Restart all asks first, naming the worked sections: '{t[:120]}'")
    shot(page, shots, "A5_restart_all_confirm")
    page.locator("#dlg button[value=cancel]").click()
    page.wait_for_timeout(400)
    check(len(app.get(f"/api/state?reviewer={R}")["ops"]) == 3, "Cancel: nothing applied")
    page.click("#restartBtn")
    page.locator("#ctx .mi", has_text="Claude Code labels (all sections)").click()
    page.locator("#restartAllOk").click()
    wait_ops(page, 4)
    final, tag = app.final(R)
    check(np.array_equal(final[c != 255], c[c != 255]) and (tag[c != 255] == 4).all() and app.get(f"/api/state?reviewer={R}")["ops"][-1]["scope"] == "all",
          "confirmed Restart all: every section = Claude Code")
    page.keyboard.press("Control+z")
    wait_ops(page, 3)
    # with every section untouched, the start card's 'apply to all' covers all of them
    page.click("#layerClearBtn")
    page.locator("#ctx .mi", has_text="all sections").click()
    wait_ops(page, 4)
    page.wait_for_timeout(300)
    card.locator("#startAll").check()
    card.locator("[data-start=vestrum]").click()
    wait_ops(page, 5)
    final, tag = app.final(R)
    check((tag[v != 255] == 3).all() and app.get(f"/api/state?reviewer={R}")["ops"][-1]["scope"] == "all", "start card on an empty layer: apply to all = every section")

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


# ------------------------------------------------------------------------------------------- step E
EPI = ["Keratin", "Squam1", "Squam2", "Squam3", "Basal(core)", "Basal(stem)", "Basal(ifn)"]


def test_E(page, app, shots):
    """Assisted selection: wand, compartment wand, Novae click, refine border, guess + Suggest, review queue, efficiency log."""
    import pandas as pd
    from scipy.spatial import Delaunay
    R = "tester_e"
    open_ui(page, app, R)
    page.wait_for_function("PathQC.S.assist && PathQC.S.comp", timeout=30000)
    cells = pd.read_csv(app.pack / "cells.tsv.gz", sep="\t", dtype=str, keep_default_na=False)
    bill_name = cells.bill_eugene_code.values
    novae = {k: cells[k].values for k in ("novae5", "novae10")}
    xmm, ymm = cells.x_mm.astype(float).values, cells.y_mm.astype(float).values
    epi = np.isin(bill_name, EPI)
    s1 = app.sec == 0
    check(page.locator("#modes3").is_visible() and page.locator("#modes3 [data-mode=comp]").is_visible()
          and page.locator("#modes3 [data-mode=novae]").is_visible(), "Assist group shown with every tool (pack has stains + Novae)")
    gap = page.evaluate("PathQC.S.gap")
    check(30 <= gap <= 60, f"max gap slider default {gap} um (sparse demo pack: wider than the real slide's 30 um)")

    # the test's own graph: Delaunay per section, edges <= gap (um)
    def graph(g):
        nb = [[] for _ in range(app.n)]
        for k in np.unique(app.sec):
            idx = np.nonzero(app.sec == k)[0]
            xy = np.column_stack([xmm[idx], ymm[idx]]) * 1000
            t = Delaunay(xy).simplices
            e = np.unique(np.sort(np.vstack([t[:, [0, 1]], t[:, [1, 2]], t[:, [0, 2]]]), 1), axis=0)
            e = e[np.hypot(*(xy[e[:, 0]] - xy[e[:, 1]]).T) <= g]
            for a, b in idx[e]:
                nb[a].append(b); nb[b].append(a)
        return nb
    nb = graph(gap)

    def component(seed, ok):
        seen, st = {seed}, [seed]
        while st:
            i = st.pop()
            for j in nb[i]:
                if ok[j] and j not in seen:
                    seen.add(j); st.append(j)
        return seen

    go_section(page, 0)
    page.locator("#startCard [data-start=bill_eugene_code]").click()
    wait_ops(page, 1)
    nops = 1

    from scipy.spatial import cKDTree
    nn_px = cKDTree(np.column_stack([app.px, app.py])).query(np.column_stack([app.px, app.py]), k=2)[0][:, 1]

    def click_cell(k, cands, mods=()):
        """Click the first candidate cell that is on panel k (not under the popover/header) and at least
        2 screen px from its nearest neighbour (WebKit rounds mouse coordinates to whole pixels)."""
        a, b = client(page, k, 0, 0), client(page, k, 1000, 0)
        scale = (b[0] - a[0]) / 1000
        for i in sorted(cands, key=lambda i: nn_px[i] * scale < 2):
            x, y = client(page, k, app.px[i], app.py[i])
            if page.evaluate(f"(() => {{ const e = document.elementFromPoint({x}, {y}); return !!e && e.closest('.panel') === PathQC.S.panels[{k}].el && !e.closest('.ph'); }})()"):
                for m in mods:
                    page.keyboard.down(m)
                page.mouse.click(x, y)
                for m in mods:
                    page.keyboard.up(m)
                page.wait_for_function("!PathQC.S.busy", timeout=5000)
                page.wait_for_timeout(350)
                return int(i)
        raise AssertionError("no clickable candidate cell")

    def drag_rect(k, x0, y0, x1, y1):
        """Rectangle tool drag on panel k between two image points, kept clear of the panel header."""
        r = page.evaluate(f"(() => {{ const b = PathQC.S.panels[{k}].el.getBoundingClientRect(); return [b.left, b.top, b.right, b.bottom]; }})()")
        a, b = client(page, k, x0, y0), client(page, k, x1, y1)
        cl = lambda p: (min(max(p[0], r[0] + 4), r[2] - 4), min(max(p[1], r[1] + 40), r[3] - 4))
        a, b = cl(a), cl(b)
        page.keyboard.press("r")
        page.mouse.move(*a); page.mouse.down(); page.mouse.move(*b, steps=5); page.mouse.up(); page.wait_for_timeout(250)
        return sel_list(page)

    def biggest(mask, cands):
        """Candidates ordered by the size of their same-class component (largest first)."""
        size = {}
        out = []
        for i in cands:
            if i not in size:
                c = component(i, mask)
                for j in c:
                    size[j] = len(c)
            out.append(i)
        return sorted(out, key=lambda i: -size[i])

    def central(mask, sec=0):
        """Cells of the mask in a section, ordered from the section centre outwards."""
        idx = np.nonzero(mask & (app.sec == sec))[0]
        cx, cy = np.median(app.px[app.sec == sec]), np.median(app.py[app.sec == sec])
        return idx[np.argsort(np.hypot(app.px[idx] - cx, app.py[idx] - cy))][:400]

    # 1. magic wand on the Bill panel: same label, connected, maximal
    page.keyboard.press("w")
    page.wait_for_timeout(300)
    check(page.locator("#assistBar #abGap").is_visible() and page.locator("#abJump").is_visible(), "W: wand mode shows max gap + jump")
    ker = bill_name == "Keratin"
    deg = np.array([sum(ker[j] for j in nb[i]) for i in range(app.n)])
    seed = click_cell(2, biggest(ker, [i for i in central(ker) if deg[i] >= 3]))
    s = sel_list(page)
    comp = component(seed, ker)
    check(seed in s and all(bill_name[i] == "Keratin" for i in s), f"wand on a Bill Keratin cell: only Keratin cells ({len(s)})")
    check(s == comp and len(s) >= 20, f"wand region = the connected Keratin component of the seed (graph check, {len(comp)} cells; diff {sorted(s ^ comp)[:6]})")
    shot(page, shots, "E1_wand_keratin")
    seed2 = click_cell(2, biggest(bill_name == "Squam2", [i for i in central(bill_name == "Squam2") if i not in s]), mods=["Shift"])
    s2 = sel_list(page)
    check(s2 == s | component(seed2, bill_name == "Squam2"), f"Shift+wand adds the Squam2 region ({len(s)} -> {len(s2)})")
    click_cell(2, [seed], mods=["Alt"])
    check(sel_list(page) == s2 - s, "Alt+wand subtracts the Keratin region again")
    page.keyboard.press("Escape")
    page.select_option("#abJump", "1")
    seed = click_cell(2, [seed])
    sj = sel_list(page)
    check(sj >= s and all(bill_name[i] == "Keratin" for i in sj), f"jump 1: superset, still only Keratin ({len(s)} -> {len(sj)})")
    page.select_option("#abJump", "0")
    # Ctrl limits the flood to the current selection
    page.keyboard.press("Escape")
    sx = [app.px[i] for i in s]; sy = [app.py[i] for i in s]
    rect = drag_rect(2, min(sx) - 30, min(sy) - 60, (min(sx) + max(sx)) / 2, max(sy) + 60)
    check(len(rect & s) > 5 and not s <= rect, f"rectangle over part of the Keratin region ({len(rect & s)} of {len(s)})")
    page.keyboard.press("w")
    page.wait_for_timeout(300)
    click_cell(2, list(rect & s), mods=["Control"])
    sc = sel_list(page)
    check(sc and sc <= rect and sc <= s and all(bill_name[i] == "Keratin" for i in sc), f"Ctrl+wand keeps only Keratin cells inside the selection ({len(rect)} -> {len(sc)})")
    page.keyboard.press("Escape")

    # wand on the reviewer panel uses the reviewer's labels; apply a relabel to set up Suggest later
    seed = click_cell(1, biggest(ker, [i for i in central(ker) if deg[i] >= 3]))
    s = sel_list(page)
    check(s == component(seed, ker), "wand on the reviewer panel (= Bill after the start pick) gives the same region")
    sq3 = page.evaluate("PathQC.S.vocab.find(v => v.name === 'Squam3').code")
    page.select_option("#pop [data-a=to]", str(sq3))
    page.locator("#pop [data-a=relabel]").click()
    nops += 1; wait_ops(page, nops)
    op = json.loads(app.ops_file(R).read_text().splitlines()[-1])
    check(op["tool"] == "wand" and op["tools"] == ["wand"] and op["sel_ms"] > 0 and op["n"] == len(s), f"relabel op logs tool=wand, sel_ms={op['sel_ms']}")

    # 2. guess + Suggest on that region (agents say Keratin, the reviewer now says Squam3)
    g = page.locator("#selGuess").inner_text()
    check("Epithelial" in g and "Novae" in g and "agree: Keratin" in g and "yours Squam3" in g, f"guess line: '{g}'")
    page.locator("#pop [data-a=suggest]").click()
    kcode = page.evaluate("PathQC.S.vocab.find(v => v.name === 'Keratin').code")
    check(page.evaluate("PathQC.S.target") == kcode and page.evaluate("[...PathQC.S.ticked]") == [sq3]
          and page.locator("#pop [data-a=to]").input_value() == str(kcode), "Suggest pre-sets target Keratin and ticks Squam3 (does not apply)")
    check(len(app.get(f"/api/state?reviewer={R}")["ops"]) == nops, "Suggest applied nothing by itself")
    shot(page, shots, "E2_guess_suggest")
    page.keyboard.press("Enter")
    nops += 1; wait_ops(page, nops)
    op = json.loads(app.ops_file(R).read_text().splitlines()[-1])
    final, _ = app.final(R)
    check(op["to_name"] == "Keratin" and "suggest" in op["tools"] and (final[list(s)] == kcode).all(), "Enter after Suggest relabels back to Keratin; op tools include 'suggest'")
    page.keyboard.press("Escape")

    # 3. compartment wand: an epithelial cell -> the section's epithelium
    page.keyboard.press("k")
    page.wait_for_timeout(300)
    check(page.locator("#abP").is_visible() and "≥" in page.locator("#abPV").inner_text(), f"K: PanCK threshold slider shown ({page.locator('#abPV').inner_text()})")
    seed = click_cell(2, central(epi & (cells.panck.astype(float).values > 6000)))
    s = np.array(sorted(sel_list(page)))
    truth = epi & s1
    prec, rec = epi[s].mean(), np.isin(np.nonzero(truth)[0], s).mean()
    check(prec >= 0.95 and rec >= 0.9 and (app.sec[s] == 0).all(), f"compartment wand: epithelium of S1, precision {prec:.3f}, recall {rec:.3f}")
    shot(page, shots, "E3_compartment")
    t0 = page.locator("#abPV").inner_text()
    page.locator("#abP").fill("1")
    page.wait_for_timeout(500)
    check(page.locator("#abPV").inner_text() != t0, f"moving the PanCK slider moves the threshold ({t0} -> {page.locator('#abPV').inner_text()})")
    page.locator("#abP").fill("0")
    page.wait_for_timeout(500)
    page.keyboard.press("Escape")

    # 4. Novae region at two resolutions
    page.keyboard.press("n")
    page.wait_for_timeout(300)
    seed = click_cell(2, central(epi))
    s = sel_list(page)
    dom = novae["novae10"][seed]
    check(all(novae["novae10"][i] == dom for i in s) and s == component(seed, novae["novae10"] == dom), f"Novae click: connected cells of domain {dom} ({len(s)})")
    page.select_option("#abRes", "novae5")
    page.wait_for_timeout(400)
    seed = click_cell(2, [seed])
    s5 = sel_list(page)
    d5 = novae["novae5"][seed]
    check(all(novae["novae5"][i] == d5 for i in s5) and s5 >= s, f"resolution 5: domain {d5}, coarser region ({len(s)} -> {len(s5)})")
    page.keyboard.press("Escape")

    # 5. refine border (random walker) on a box straddling the epithelium / stroma border
    bottom = 2.2 + 0.25 * np.sin(1.7 * xmm) - 0.48                 # demo epithelium lower edge (mm)
    zone = s1 & (xmm > 1.1) & (xmm < 1.7) & (ymm > bottom - 0.25) & (ymm < bottom + 0.2)
    zb = (app.px[zone].min(), app.py[zone].min(), app.px[zone].max(), app.py[zone].max())
    box = np.array(sorted(drag_rect(1, *zb)))
    check(0.2 < epi[box].mean() < 0.8, f"box straddles the border ({len(box)} cells, {epi[box].mean():.0%} epithelial)")
    page.keyboard.press("g")
    page.wait_for_timeout(300)
    check(page.evaluate("PathQC.S.mode") == "walker", "G: refine-border mode with a selection")
    yb = ymm[box]
    ins = [i for i in box[np.argsort(-yb)] if epi[i]][:2]
    outs = [i for i in box[np.argsort(yb)] if not epi[i]][:2]
    for i in ins:
        click_cell(2, [i])
    for i in outs:
        click_cell(2, [i], mods=["Alt"])
    page.wait_for_timeout(400)
    new = sel_list(page)
    pred = np.isin(box, list(new))
    acc = (pred == epi[box]).mean()
    check(acc >= 0.9, f"random walker separates epithelium from stroma in the box: accuracy {acc:.3f} vs truth ({len(new)} selected)")
    shot(page, shots, "E4_walker")
    page.locator("#pop [data-base=bill_eugene_code]").click()          # an op made from the refined selection
    nops += 1; wait_ops(page, nops)
    op = json.loads(app.ops_file(R).read_text().splitlines()[-1])
    check(op["tool"] == "walker" and op["tools"] == ["rect", "walker"] and op["n"] == len(new), f"op logs tool=walker after rect ({op['tools']})")
    page.keyboard.press("Escape")
    check(page.evaluate("PathQC.S.mode") == "pan" and page.evaluate("PathQC.S.walk") is None, "Esc leaves refine mode and drops the seeds")
    # snap: keep the majority compartment
    box = np.array(sorted(drag_rect(1, *zb)))
    page.locator("#pop [data-a=snap]").click()
    page.wait_for_timeout(200)
    sn = np.array(sorted(sel_list(page)))
    comp_arr = np.array(page.evaluate("Array.from(PathQC.S.comp)"))
    check(len(sn) < len(box) and len(set(comp_arr[sn])) == 1 and set(sn) <= set(box), f"Snap to compartment keeps one compartment ({len(box)} -> {len(sn)})")
    # the walker's seed toggle: with "outside" on, a plain click places an outside seed (Alt is not needed)
    page.keyboard.press("g")
    page.wait_for_timeout(300)
    check("Alt+click = outside seed" in page.locator("#pop").inner_text() and page.locator("#abSide").is_visible(),
          "refine mode says what Alt means here (outside seed, not subtract) and shows the inside/outside toggle")
    page.locator("#abSide [data-side=out]").click()
    click_cell(2, [i for i in box if not epi[i]][:5])
    check(page.evaluate("[PathQC.S.walk.inside.length, PathQC.S.walk.outside.length]") == [0, 1], "toggle on 'outside': a plain click places an outside seed")
    shot(page, shots, "E4b_walker_toggle")
    page.keyboard.press("Escape")

    # 6. review queue
    page.locator("#tabs [data-tab=review]").click()
    page.wait_for_function("PathQC.S.queue && PathQC.S.queue.kind === 'any'", timeout=20000)
    q = app.get(f"/api/assist/queue?reviewer={R}&kind=any")
    sizes, scores = [it["n"] for it in q["items"]], [it["score"] for it in q["items"]]
    check(len(sizes) >= 3 and scores == sorted(scores, reverse=True) and 20 <= min(sizes) and max(sizes) <= 1500,
          f"queue: {len(sizes)} hotspots, 20-1500 cells, ranked by expected fixes {scores[:5]}")
    b_, v_, c_ = (app.src[k].astype(int) for k in SRC)
    vo, co = (v_ != 255) & (v_ != b_), (c_ != 255) & (c_ != b_)
    agent = np.where(vo & (~co | (v_ == c_)), v_, np.where(co & ~vo, c_, -2))
    pure = True
    for it in q["items"][:12]:
        cl = np.array(app.get(f"/api/assist/queue/any/{it['id']}?reviewer={R}")["cells"])
        pure &= len(set(zip(b_[cl], agent[cl], novae["novae10"][cl]))) == 1
    check(pure, "each 'any' item is one (Bill label, agent label) confusion in one Novae domain")
    import sys as _s; _s.path.insert(0, str(ROOT))
    from server.assist import Assist
    from server.pack import Pack
    big = np.nonzero(app.sec == 0)[0][:4000]
    ch = Assist(Pack(app.pack), app.tmp / "cache")._chunks(big)
    check(max(map(len, ch)) <= 1500 and np.array_equal(np.sort(np.concatenate(ch)), big), f"a 4,000-cell item is cut into spatial chunks {sorted(map(len, ch))}")
    check(page.locator("#reviewPane .item").count() == len(sizes) and f"/ {len(sizes)} done" in page.locator("#qProg").inner_text(), "queue listed in the Review tab with progress")
    page.keyboard.press("j")
    page.wait_for_function("PathQC.S.selList.length > 0 && PathQC.S.selTool === 'queue'", timeout=10000)
    first = q["items"][0]
    cells0 = set(app.get(f"/api/assist/queue/any/{first['id']}?reviewer={R}")["cells"])
    check(sel_list(page) == cells0, f"J selects item #1 ({first['n']} cells, {first['section']})")
    disp = (app.src["vestrum"] != app.src["bill_eugene_code"]) | (app.src["claude_code"] != app.src["bill_eugene_code"])
    check(np.mean([disp[i] for i in cells0]) > 0.6, "item cells are mostly disputed")
    shot(page, shots, "E5_queue_next")
    page.locator("#pop [data-base=claude_code]").click()
    nops += 1; wait_ops(page, nops)
    page.wait_for_function("PathQC.S.queue.n_done === 1", timeout=10000)
    check("1 / " in page.locator("#qProg").inner_text() and page.locator("#reviewPane .item.done").count() == 1, f"item marked done after the pick: '{page.locator('#qProg').inner_text()}'")
    page.keyboard.press("Escape")
    page.keyboard.press("j")
    page.wait_for_function(f"PathQC.S.queue.cur === 1 && PathQC.S.selList.length === {q['items'][1]['n']}", timeout=10000)
    check(sel_list(page) == set(app.get(f"/api/assist/queue/any/{q['items'][1]['id']}?reviewer={R}")["cells"]), "J again goes to the next item not done")
    page.keyboard.press("Shift+J")
    page.wait_for_function(f"PathQC.S.queue.cur === 0 && PathQC.S.selList.length === {first['n']}", timeout=10000)
    check(sel_list(page) == cells0, "Shift+J goes back to item #1")
    page.keyboard.press("Escape")
    page.keyboard.press("Control+z")
    nops -= 1; wait_ops(page, nops)
    page.wait_for_function("PathQC.S.queue.n_done === 0", timeout=10000)
    check(True, "undo of the pick un-marks the item")
    page.locator("#qKind [data-k=both]").click()
    page.wait_for_function("PathQC.S.queue.kind === 'both'", timeout=10000)
    qb = app.get(f"/api/assist/queue?reviewer={R}&kind=both")
    check(page.locator("#reviewPane .item").count() == len(qb["items"]), f"toggle to 'both agents agree' list ({len(qb['items'])} items on the demo)")
    shot(page, shots, "E6_review_tab")

    # 7. efficiency log in ops.jsonl and summary.json
    lines = [json.loads(l) for l in app.ops_file(R).read_text().splitlines()]
    tools = [o.get("tool") for o in lines if o["op"] in ("base", "relabel")]
    check(tools == ["section", "wand", "wand", "walker", "queue"], f"ops.jsonl tools in order: {tools}")
    undo_l = [o for o in lines if o["op"] == "undo"]
    check(undo_l and undo_l[-1].get("tool") == "queue", "undo record names the undone op's tool")
    sv = app.post("/api/save", {"reviewer": R, "image": False})
    summ = json.loads((Path(sv["dir"]) / "summary.json").read_text())
    eff = summ["efficiency_by_tool"]
    check(eff["wand"]["n_ops"] == 2 and eff["wand"]["undone"] == 0 and eff["wand"]["median_ms"] > 0
          and eff["queue"] == {"n_ops": 1, "cells": first["n"], "median_ms": eff["queue"]["median_ms"], "undone": 1}
          and eff["section"]["n_ops"] == 1 and eff["walker"]["n_ops"] == 1, f"summary.json efficiency_by_tool: {eff}")


# ---------------------------------------------------------------- step B: H&E at start-up
B_PORT = None          # set by test_B: --port + 1
B_REVIEWER = "Tester B"


def b_get(path, raw=False, port=None):
    with urllib.request.urlopen(f"http://127.0.0.1:{port or B_PORT}{path}", timeout=60) as r:
        data = r.read()
        return (data, r.status, r.headers.get("Content-Type")) if raw else json.loads(data)


def b_post(path, body, port=None):
    req = urllib.request.Request(f"http://127.0.0.1:{port or B_PORT}{path}", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.loads(r.read())


def b_start(exe, pack, home, results, log):
    cmd = ([sys.executable, str(ROOT / "run.py")] if exe == "source" else [str(exe)])
    cmd += ["--pack", str(pack), "--no-browser", "--port", str(B_PORT), "--results", str(results)]
    env = dict(os.environ, PATHQC_HOME=str(home), HOME=str(home), USERPROFILE=str(home), PATHQC_NO_DIALOG="1")
    with socket.socket() as s:
        if s.connect_ex(("127.0.0.1", B_PORT)) == 0:
            raise RuntimeError(f"port {B_PORT} is busy")       # run.py would move to another port
    proc = subprocess.Popen(cmd, env=env, stdout=open(log, "a"), stderr=subprocess.STDOUT, cwd=str(ROOT))
    for _ in range(240):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{B_PORT}/start", timeout=2)
            return proc
        except Exception:
            if proc.poll() is not None:
                raise RuntimeError(f"app exited ({proc.returncode}); see {log}")
            time.sleep(0.5)
    raise RuntimeError("app did not answer")


def b_stop(proc):
    if proc and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()


def b_err_um(A, truth, x, y):
    from server.register import apply
    ex, ey = apply(A, x, y)
    tx, ty = apply(truth["mm_to_he_px"], x, y)
    return np.hypot(ex - tx, ey - ty) * truth["um_per_px"]


def b_click_img(page, sel, x, y, natural_w, natural_h):
    page.evaluate("document.querySelector('#toast').hidden = true")
    page.locator(sel).scroll_into_view_if_needed()
    box = page.locator(sel).bounding_box()
    page.mouse.click(box["x"] + x * box["width"] / natural_w, box["y"] + y * box["height"] / natural_h)


def b_status(path, headers, body=None):
    req = urllib.request.Request(f"http://127.0.0.1:{B_PORT}{path}", headers=headers, method="POST" if body is not None else "GET",
                                 data=None if body is None else json.dumps(body).encode())
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def b_guard_checks(tmp):
    """Web pages from elsewhere get nothing: foreign Host (DNS rebinding) and cross-site POSTs are refused;
    our own page's requests pass. A reviewer name with & # survives the redirect. A broken pack keeps the current one."""
    js = {"Content-Type": "application/json"}
    assert b_status("/api/setup/state", {"Host": "evil.example:%d" % B_PORT}) == 403
    assert b_status("/api/state?reviewer=x", {"Host": "evil.example"}) == 403
    assert b_status("/api/setup/standin", {"Content-Type": "text/plain", "Origin": "http://evil.example"}, "x") == 403
    assert b_status("/api/setup/standin", dict(js, **{"Sec-Fetch-Site": "cross-site"}), {}) == 403
    assert b_status("/api/setup/standin", dict(js, **{"Sec-Fetch-Site": "same-site", "Origin": "http://127.0.0.1:1"}), {}) == 403
    assert b_status("/api/setup/state", {"Host": "localhost:%d" % B_PORT}) == 200
    own = {"Origin": f"http://127.0.0.1:{B_PORT}", "Sec-Fetch-Site": "same-origin"}
    assert b_status("/api/setup/reviewer", dict(js, **own), {"name": "Anne & Bob #2"}) == 200
    r = b_post("/api/setup/standin", {})
    assert r["redirect"] == "/?reviewer=Anne%20%26%20Bob%20%232", r["redirect"]
    bad = tmp / "badpack"
    bad.mkdir()
    (bad / "manifest.json").write_text("{}")                 # stand-in present, cells missing: create_app fails
    shutil.copy(tmp / "pack" / "transform.json", bad / "transform.json")
    (bad / "image").mkdir()
    (bad / "image" / "slide.dzi").write_text("")
    before = b_get("/api/setup/state")["pack"]["path"]
    assert b_status("/api/setup/pack", js, {"path": str(bad)}) == 400
    st = b_get("/api/setup/state")
    assert st["pack"]["path"] == before and b_get("/api/setup/state")["ready"], st
    assert b_status("/api/setup/standin/thumb.jpg?size=abc", {}) == 200


def test_B(page, app, shots):
    """Start screen -> paste fake H&E path -> anchors + auto registration -> accept -> viewer on the H&E;
    tiles; export over the H&E; settings persist across a restart; moved file is detected and re-located."""
    global B_PORT
    B_PORT, exe = app.port + 1, app.exe
    from make_fake_he import make
    from pack.cells_io import read_cells
    from server.register import apply

    tmp = Path(tempfile.mkdtemp(prefix="pathqc_B_"))
    home, results, pack, log = tmp / "home", tmp / "results", tmp / "pack", tmp / "app.log"
    home.mkdir()
    demo = ROOT / "demo_pack"
    if not (demo / "manifest.json").exists():
        subprocess.run([sys.executable, str(ROOT / "pack" / "make_demo_pack.py"), "--out", str(demo)], check=True)
    pack.mkdir()
    for f in ("cells.tsv.gz", "manifest.json"):
        shutil.copy(demo / f, pack / f)
    he_file, truth = make(pack, tmp / "slides" / "fake_he.tif")       # builds the pack's stand-in first
    cells = read_cells(pack, columns=["x_mm", "y_mm"])
    x, y = cells.x_mm.values.astype(float), cells.y_mm.values.astype(float)
    checks, proc = [], None
    try:
        proc = b_start(exe, pack, home, results, log)
        tiles = []
        page.on("response", lambda r: tiles.append(r.status) if "/tiles/he_" in r.url and r.url.endswith(".jpeg") else None)

        # 1. first start: the start screen
        page.goto(f"http://127.0.0.1:{B_PORT}/")
        page.wait_for_url("**/start")
        page.wait_for_selector("#packInfo >> text=demo")
        assert page.locator("#packOk").is_visible()
        page.fill("#reviewer", B_REVIEWER)
        page.click("#pickHe")                                         # no dialog here -> paste fallback
        page.wait_for_selector("#toast >> text=paste the full path")
        page.fill("#hePath", str(he_file))
        page.click("#heOpen")
        page.wait_for_selector("#heInfo >> text=fake_he.tif")
        page.screenshot(path=str(shots / "B1_start_he_opened.png"), full_page=True)
        checks.append("start screen, paste-path fallback, H&E opened")

        # 2. registration: anchors (4 clicks per side) then automatic
        page.click("#heBtn")
        page.wait_for_selector("#stepReg:not([hidden])")
        page.click("#regTabs button[data-tab=anchors]")
        page.wait_for_function("document.querySelector('#heCanvas').width > 100 && document.querySelector('#siCanvas').width > 100")
        page.locator(".st-anchors").scroll_into_view_if_needed()
        page.wait_for_timeout(400)                                    # let the layout settle before clicking
        hw, hh = page.evaluate("[document.querySelector('#heCanvas').width, document.querySelector('#heCanvas').height]")
        sw, shh = page.evaluate("[document.querySelector('#siCanvas').width, document.querySelector('#siCanvas').height]")
        st = b_get("/api/setup/state")
        assert st["he"]["reader"] == "openslide", f"H&E opened with {st['he']['reader']}, not OpenSlide (bundling?)"
        sa = json.loads((pack / "transform.json").read_text())["mm_to_px"]
        idx = [int(np.argmin(x + y)), int(np.argmax(x + y)), int(np.argmin(x - y)), int(np.argmax(x - y))]
        for k, i in enumerate(idx):
            hx, hy = apply(truth["mm_to_he_px"], x[i], y[i])
            b_click_img(page, "#heCanvas", hx * hw / st["he"]["width"], hy * hh / st["he"]["height"], hw, hh)
            px, py = apply(sa, x[i], y[i])
            b_click_img(page, "#siCanvas", px * sw / st["standin"]["width"], py * shh / st["standin"]["height"], sw, shh)
            page.wait_for_selector(f"#pairList >> text={k + 1} pair")
        page.uncheck("#anchorRefine")
        page.click("#anchorFit")
        page.wait_for_selector("#checkCard:not([hidden])")
        cand = b_get("/api/setup/state")["candidate"]
        e = b_err_um(cand["mm_to_px"], truth, x, y)
        page.screenshot(path=str(shots / "B2_anchors.png"), full_page=True)
        assert cand["method"] == "anchors" and np.percentile(e, 95) < 40, \
            f"anchor fit off: p95 {np.percentile(e, 95):.1f} um, residuals {cand.get('residual_um')}"
        checks.append(f"anchor clicks (4 pairs on overview images): p95 {np.percentile(e, 95):.1f} um")
        page.check("#anchorRefine")
        page.click("#anchorFit")
        page.wait_for_selector("#regResult >> text=anchors+refine", timeout=120_000)
        cand = b_get("/api/setup/state")["candidate"]
        e = b_err_um(cand["mm_to_px"], truth, x, y)
        assert np.percentile(e, 95) < 10, f"anchors + refine off: p95 {np.percentile(e, 95):.1f} um"
        checks.append(f"anchors + automatic refine: p95 {np.percentile(e, 95):.2f} um")

        page.click("#regTabs button[data-tab=auto]")
        page.click("#regRun")
        page.wait_for_selector("#regMsg >> text=Done in", timeout=240_000)
        page.fill("#opacity", "0.9")
        page.dispatch_event("#opacity", "input")
        cand = b_get("/api/setup/state")["candidate"]
        e = b_err_um(cand["mm_to_px"], truth, x, y)
        page.screenshot(path=str(shots / "B3_auto_overlay.png"), full_page=True)
        assert cand["method"] == "auto" and np.percentile(e, 95) < 10, f"auto registration off: p95 {np.percentile(e, 95):.1f} um"
        checks.append(f"auto registration: median {np.median(e):.2f} / p95 {np.percentile(e, 95):.2f} / max {e.max():.2f} um")
        assert cand["status"] == "confident", f"easy fake H&E not confident: {cand.get('reason')}"
        assert page.locator("#regWarn").is_hidden() and "primary" in page.get_attribute("#acceptBtn", "class")
        # an uncertain result (the server's answer rewritten in flight): warning with the reason, accept demoted
        def uncertain(route):
            r = route.fetch()
            j = r.json()
            if j.get("candidate"):
                j["candidate"].update(status="uncertain", confident=False, reason="test reason xyz")
            route.fulfill(response=r, json=j)
        done = "!document.querySelector('#regRun').disabled && document.querySelector('#regMsg').textContent.startsWith('Done')"
        page.route("**/api/setup/state", uncertain)
        page.click("#regRun")
        page.wait_for_function(done, timeout=240_000)
        assert page.locator("#regWarn").is_visible() and "test reason xyz" in page.inner_text("#regWarn")
        assert "anchor points" in page.inner_text("#regWarn")
        assert "primary" not in page.get_attribute("#acceptBtn", "class") and "anyway" in page.inner_text("#acceptBtn")
        page.screenshot(path=str(shots / "B3b_uncertain.png"), full_page=True)
        page.unroute("**/api/setup/state")
        page.click("#regRun")
        page.wait_for_function(done, timeout=240_000)
        assert page.locator("#regWarn").is_hidden() and "primary" in page.get_attribute("#acceptBtn", "class")
        checks.append(f"confidence: {cand['status']} (r {cand['score']:.2f}, margin {cand['margin']:.2f}); "
                      "uncertain shows the reason and demotes Accept")

        # 3. accept -> viewer on the H&E
        page.click("#acceptBtn")
        page.wait_for_url("**/?reviewer=*")
        page.wait_for_selector(".panel .ph .t")
        title = page.locator(".panel .ph .t").first.inner_text()
        assert title == "H&E", f"slide panel title is {title!r}"
        assert "H&E" in page.locator("#imgbadge").inner_text()
        page.wait_for_timeout(2500)
        page.screenshot(path=str(shots / "B4_viewer_he.png"))
        assert tiles and all(s == 200 for s in tiles), f"H&E tiles in the browser: {tiles[:10]}"
        assert not (pack / "transform.json").read_text().count('"he"'), "stand-in transform was overwritten"
        s = b_get("/api/state?reviewer=" + urllib.request.quote(B_REVIEWER))
        assert s["image"]["kind"] == "he" and s["image"]["dzi"].startswith("/tiles/he_")
        dzi, code, _ = b_get(s["image"]["dzi"], raw=True)
        top = int(np.ceil(np.log2(max(truth["width"], truth["height"]))))
        tile, tcode, ttype = b_get(s["image"]["dzi"].replace(".dzi", f"_files/{top}/0_0.jpeg"), raw=True)
        assert code == 200 and b"Image" in dzi and tcode == 200 and ttype == "image/jpeg" and len(tile) > 500
        buf = b_get("/api/cells.bin", raw=True)[0]
        n = int(np.frombuffer(buf[:4], np.uint32)[0])
        cpx = np.frombuffer(buf[4:4 + 4 * n], np.float32)
        cpy = np.frombuffer(buf[4 + 4 * n:4 + 8 * n], np.float32)
        tx, ty = apply(truth["mm_to_he_px"], x, y)
        ce = np.hypot(cpx - tx, cpy - ty) * truth["um_per_px"]
        assert np.percentile(ce, 95) < 10, "cell overlay not mapped through the H&E transform"
        checks.append(f"viewer: title H&E, {len(tiles)} H&E tiles HTTP 200, cells mapped (p95 {np.percentile(ce, 95):.2f} um)")

        # 4. save: the whole-slide image is painted over the H&E
        sv = b_post("/api/save", {"reviewer": B_REVIEWER, "image": True})
        for _ in range(240):
            stt = b_get("/api/save/status?reviewer=" + urllib.request.quote(B_REVIEWER))
            if stt.get("image") != "running":
                break
            time.sleep(0.5)
        assert stt.get("image") == "done", stt
        import pyvips
        out = pyvips.Image.new_from_file(str(Path(sv["dir"]) / "corrected_slide.tif"))
        assert (out.width, out.height) == (truth["width"], truth["height"]), (out.width, out.height)
        pyvips.Image.thumbnail(str(Path(sv["dir"]) / "corrected_slide.tif"), 900).write_to_file(str(shots / "B5_export_over_he.png"))
        checks.append(f"export over the H&E ({out.width}x{out.height})")

        # 5. restart: straight to the viewer, still H&E
        b_stop(proc)
        proc = b_start(exe, pack, home, results, log)
        page.goto(f"http://127.0.0.1:{B_PORT}/")
        page.wait_for_url("**/?reviewer=*")
        page.wait_for_selector(".panel .ph .t")
        assert page.locator(".panel .ph .t").first.inner_text() == "H&E"
        stg = json.loads((home / "settings.json").read_text())
        ent = next(iter(stg["packs"].values()))
        assert stg["reviewer"] == B_REVIEWER and ent["image"] == "he" and ent["he"]["fingerprint"]["id"] and stg["registrations"]
        page.wait_for_timeout(1500)
        page.screenshot(path=str(shots / "B6_after_restart.png"))
        checks.append("restart opens the viewer on the H&E; settings.json has reviewer, pack, H&E + fingerprint, registration")

        # 6. the H&E moves: told on start, re-locate, its registration is reused
        b_stop(proc)
        moved = tmp / "moved" / "fake_he.tif"
        moved.parent.mkdir()
        shutil.move(str(he_file), moved)
        proc = b_start(exe, pack, home, results, log)
        page.goto(f"http://127.0.0.1:{B_PORT}/")
        page.wait_for_url("**/start")
        page.wait_for_selector("#heProblem:not([hidden]) >> text=no longer")
        page.screenshot(path=str(shots / "B7_moved_file.png"), full_page=True)
        page.fill("#hePath", str(moved))
        page.click("#heOpen")
        page.wait_for_selector("#savedHeBtn:not([hidden]) >> text=saved registration")
        page.click("#savedHeBtn")
        page.wait_for_url("**/?reviewer=*")
        page.wait_for_selector(".panel .ph .t")
        assert page.locator(".panel .ph .t").first.inner_text() == "H&E"
        checks.append("moved H&E detected at start; re-located file reuses its registration (same fingerprint)")

        # 7. Change slide / H&E -> stand-in
        page.click("#changeSlide")
        page.wait_for_url("**/start")
        page.click("#standinBtn")
        page.wait_for_url("**/?reviewer=*")
        page.wait_for_selector(".panel .ph .t")
        assert page.locator(".panel .ph .t").first.inner_text().startswith("Stand-in")
        checks.append("Change slide / H&E -> continue with stand-in")
        b_guard_checks(tmp)
        checks.append("foreign Host / cross-site POST refused (403); reviewer '&#' redirect; broken pack keeps current")
        print("PASS test_B:", "; ".join(checks))
    except Exception as e:
        print("FAIL test_B after:", "; ".join(checks) or "nothing", "->", repr(e))
        print("---- app log ----\n" + (log.read_text()[-4000:] if log.exists() else ""))
        raise
    finally:
        b_stop(proc)


def test_F(page, app, shots):
    """Sealed real data: a demo copy sealed with a test password is offered on the start screen; a wrong password
    is refused; the right one unlocks it into <PathQC>/packs, switches to it, and the next start opens it."""
    from pack.seal import seal
    port, exe = app.port + 2, app.exe
    tmp = Path(tempfile.mkdtemp(prefix="pathqc_F_"))
    src = tmp / "src"
    shutil.copytree(app.pack, src, ignore=shutil.ignore_patterns("sessions", "results", "image", "*.json"))
    m = json.loads((app.pack / "manifest.json").read_text())
    m.update(pack="sealed_demo", demo=False, dataset="sealed test slide")
    (src / "manifest.json").write_text(json.dumps(m))
    seal(src, tmp / "sealed" / "sealed_demo.pqc", "test-password-123")
    home = tmp / "home"
    cmd = ([sys.executable, str(ROOT / "run.py")] if exe == "source" else [str(Path(exe).resolve())]) + ["--no-browser", "--port", str(port)]
    env = dict(os.environ, PATHQC_HOME=str(home), HOME=str(tmp), USERPROFILE=str(tmp), PATHQC_NO_DIALOG="1", PATHQC_SEALED=str(tmp / "sealed"))
    log = tmp / "app.log"
    base = f"http://127.0.0.1:{port}"

    def start():
        p = subprocess.Popen(cmd, env=env, stdout=open(log, "a"), stderr=subprocess.STDOUT, cwd=str(tmp))
        for _ in range(240):
            try:
                urllib.request.urlopen(base + "/api/setup/state", timeout=2)
                return p
            except Exception:
                if p.poll() is not None:
                    raise RuntimeError("app exited: " + log.read_text()[-3000:])
                time.sleep(0.5)
        raise RuntimeError("app did not answer")

    proc = start()
    try:
        page.goto(base + "/start")
        page.wait_for_selector("#sealedCard:not([hidden]) >> text=sealed_demo")
        page.fill("#reviewer", "Sealed Tester")
        with urllib.request.urlopen(base + "/api/setup/state") as r:
            k = [s["name"] for s in json.loads(r.read())["sealed"]].index("sealed_demo")     # the app may carry others
        pw, ub = f"[data-pw='{k}']", f"[data-unlock='{k}']"
        page.fill(pw, "not-the-password")
        page.click(ub)
        page.wait_for_selector("#toast:not([hidden]) >> text=Wrong password")
        check(not (home / "packs" / "sealed_demo").exists(), "wrong password leaves nothing on disk")
        page.fill(pw, "test-password-123")
        shot(page, shots, "F1_sealed_card")
        page.click(ub)
        page.wait_for_selector("text=unlocked, in use", timeout=120000)
        shot(page, shots, "F2_unlocked")
        with urllib.request.urlopen(base + "/api/setup/state") as r:
            st = json.loads(r.read())
        check(st["pack"]["name"] == "sealed_demo" and st["pack"]["n_cells"] == app.n, f"switched to the unlocked pack {st['pack']}")
        check((home / "packs" / "sealed_demo" / "cells.tsv.gz").read_bytes() == (src / "cells.tsv.gz").read_bytes(), "cells identical")
        page.click("#standinBtn")
        page.wait_for_url("**/?reviewer=*")
        page.wait_for_selector(".panel .ph .t")
        b_stop(proc)
        proc = start()                                     # next start: the unlocked pack, straight to the viewer
        with urllib.request.urlopen(base + "/api/setup/state") as r:
            st = json.loads(r.read())
        check(st["pack"]["name"] == "sealed_demo" and st["ready"], "next start opens the unlocked pack")
        print("PASS test_F: sealed pack offered; wrong password refused (nothing written); unlock switches; persists")
    except Exception:
        print("---- app log ----\n" + log.read_text()[-4000:])
        raise
    finally:
        b_stop(proc)


STEPS = {"A": test_A, "B": test_B, "C": test_C, "E": test_E, "F": test_F}


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
