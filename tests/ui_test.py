#!/usr/bin/env python3
"""Playwright UI tests: start the app on a copy of the demo pack with a temp home and results dir, drive it
headless, assert through the API, save screenshots.

Usage: python tests/ui_test.py --exe <frozen exe or 'source'> --shots <dir> [--browser chromium|webkit] [--only B]
Exit non-zero on failure.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))


# ---------------------------------------------------------------- step B: H&E at start-up
B_PORT = int(os.environ.get("PATHQC_TEST_PORT", 8031))
B_REVIEWER = "Tester B"


def b_get(path, raw=False, port=B_PORT):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=60) as r:
        data = r.read()
        return (data, r.status, r.headers.get("Content-Type")) if raw else json.loads(data)


def b_post(path, body, port=B_PORT):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.loads(r.read())


def b_start(exe, pack, home, results, log):
    cmd = ([sys.executable, str(ROOT / "run.py")] if exe == "source" else [str(exe)])
    cmd += ["--pack", str(pack), "--no-browser", "--port", str(B_PORT), "--results", str(results)]
    env = dict(os.environ, PATHQC_HOME=str(home), HOME=str(home), PATHQC_NO_DIALOG="1")
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


def test_B(exe, shots, browser_name="chromium"):
    """Start screen -> paste fake H&E path -> anchors + auto registration -> accept -> viewer on the H&E;
    tiles; export over the H&E; settings persist across a restart; moved file is detected and re-located."""
    from playwright.sync_api import sync_playwright
    from make_fake_he import make
    from pack.cells_io import read_cells
    from server.register import apply

    shots = Path(shots)
    shots.mkdir(parents=True, exist_ok=True)
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
        with sync_playwright() as pw:
            br = getattr(pw, browser_name).launch()
            page = br.new_page(viewport={"width": 1400, "height": 1000})
            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)))
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
            assert not errors, f"page errors: {errors}"
            br.close()
        print("PASS test_B:", "; ".join(checks))
        return True
    except Exception as e:
        print("FAIL test_B after:", "; ".join(checks) or "nothing", "->", repr(e))
        print("---- app log ----\n" + (log.read_text()[-4000:] if log.exists() else ""))
        return False
    finally:
        b_stop(proc)


STEPS = {"B": test_B}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--exe", default="source")
    ap.add_argument("--shots", required=True)
    ap.add_argument("--browser", default="chromium")
    ap.add_argument("--only", default=None, help="comma-separated step letters")
    a = ap.parse_args()
    steps = a.only.split(",") if a.only else list(STEPS)
    ok = all([STEPS[s](a.exe, a.shots, a.browser) for s in steps])
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
