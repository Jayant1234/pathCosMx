#!/usr/bin/env python3
"""Start a built PathQC binary in an empty home folder and drive its API end to end.

Checks: page, state, cells, tiles, a region pick + relabel, undo, and Save with the slide image.
Exit 0 only if every check passes. On failure prints the app's log and emits GitHub annotations.

Usage: python packaging/smoke_test.py <path to the PathQC executable> [port, default 8765]
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 8765
BASE = f"http://127.0.0.1:{PORT}"
R = "ci_reviewer"


def call(path, body=None, raw=False):
    req = urllib.request.Request(BASE + path, data=None if body is None else json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="GET" if body is None else "POST")
    with urllib.request.urlopen(req, timeout=60) as r:
        data = r.read()
    return data if raw else json.loads(data)


def main():
    exe = Path(sys.argv[1]).resolve()
    home = Path(tempfile.mkdtemp(prefix="pathqc_home_"))
    env = dict(os.environ, HOME=str(home), USERPROFILE=str(home), PATHQC_NO_DIALOG="1")
    proc = subprocess.Popen([str(exe), "--no-browser", "--port", str(PORT)], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    checks = []
    try:
        for _ in range(180):                              # first start builds the stand-in image
            try:
                urllib.request.urlopen(BASE + "/", timeout=2); break
            except Exception:
                if proc.poll() is not None:
                    raise RuntimeError(f"app exited with code {proc.returncode}")
                time.sleep(1)
        else:
            raise RuntimeError("app did not answer within 180 s")
        checks.append("page")
        st = call(f"/api/state?reviewer={R}")
        assert st["manifest"]["n_cells"] > 0
        checks.append(f"state ({st['manifest']['n_cells']} cells, demo={st['manifest'].get('demo')})")
        cells = call("/api/cells.bin", raw=True); assert len(cells) > 1000; checks.append("cells.bin")
        dzi = call("/tiles/slide.dzi", raw=True); assert b"Image" in dzi; checks.append("tiles")
        w, h = st["image"]["width"], st["image"]["height"]
        poly = [[w * .2, h * .2], [w * .5, h * .2], [w * .5, h * .8], [w * .2, h * .8]]
        reg = call("/api/region", {"reviewer": R, "polygon": poly}); assert reg["n"] > 0
        op = call("/api/ops", {"reviewer": R, "op": "base", "source": "vestrum", "polygon": poly})
        top = reg["composition"]["corrected"][0][0]
        call("/api/ops", {"reviewer": R, "op": "relabel", "to": 3, "filter": top, "polygon": poly})
        call("/api/undo", {"reviewer": R})
        checks.append(f"edit ({op['n']} cells picked, relabel, undo)")
        sv = call("/api/save", {"reviewer": R, "image": True})
        for _ in range(240):
            s = call(f"/api/save/status?reviewer={R}")
            if s.get("image") != "running":
                break
            time.sleep(1)
        assert s.get("image") == "done", f"slide image: {s}"
        out = Path(sv["dir"])
        for f in ("ci_reviewer_labels.tsv", "base_picks.tsv", "summary.json", "corrected_slide.tif"):
            assert (out / f).exists() and (out / f).stat().st_size > 0, f"missing {f}"
        checks.append("save (labels file + slide image)")
        print("PASS:", "; ".join(checks))
        return 0
    except Exception as e:
        print("FAIL after:", "; ".join(checks) or "nothing", "->", e)
        log = home / "Documents" / "PathQC" / "pathqc.log"
        text = log.read_text(errors="replace") if log.exists() else ""
        try:
            proc.terminate(); out, _ = proc.communicate(timeout=10); text += (out or b"").decode(errors="replace")
        except Exception:
            pass
        print("---- app output ----\n" + text[-6000:])
        tail = " | ".join(l for l in text.strip().splitlines()[-12:])[:3000]
        print(f"::error title=PathQC smoke test::{e} :: {tail}")
        return 1
    finally:
        if proc.poll() is None:
            proc.terminate()


if __name__ == "__main__":
    sys.exit(main())
