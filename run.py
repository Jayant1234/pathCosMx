#!/usr/bin/env python3
"""Start PathQC locally and open it in the browser.

  python run.py                                   # first real pack found, else the demo pack
  python run.py --pack packs/hnsc_slide1 --port 8010 --no-browser
  python run.py --image-transform path/to/transform_he.json   # registered H&E instead of the stand-in

Packs are looked for in this order: --pack; ~/Documents/PathQC/packs/<name>/ (real data, delivered
separately); ./packs/<name>/ (developer checkout); the bundled synthetic demo pack.
Sessions and results are written inside the chosen pack folder.
The first start shows a start screen (reviewer, pack, optional H&E + registration); the choices are kept in
~/Documents/PathQC/settings.json, so later starts open the viewer directly (header: "Change slide / H&E").
"""
import argparse
import json
import shutil
import socket
import sys
import threading
import webbrowser
from pathlib import Path

FROZEN = getattr(sys, "frozen", False)
BASE = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))   # bundled files or checkout
HOME = Path.home() / "Documents" / "PathQC"                              # user's data and work


def _log_to_file():
    """A windowed app has no console: send output to ~/Documents/PathQC/pathqc.log."""
    HOME.mkdir(parents=True, exist_ok=True)
    f = open(HOME / "pathqc.log", "a", buffering=1, encoding="utf-8")
    sys.stdout = sys.stderr = f


def _packs_in(folder):
    return sorted((p.parent for p in Path(folder).glob("*/manifest.json")), key=lambda p: -p.stat().st_mtime)


def _is_demo(pack):
    return json.loads((pack / "manifest.json").read_text()).get("demo", False)


def choose_pack(arg):
    if arg:
        return Path(arg)
    for folder in (HOME / "packs", BASE / "packs"):
        real = [p for p in _packs_in(folder) if not _is_demo(p)]
        if real:
            return real[0]
    demo = HOME / "packs" / "demo"            # writable copy of the bundled demo
    if not (demo / "manifest.json").exists():
        demo.mkdir(parents=True, exist_ok=True)
        for f in ("cells.tsv.gz", "manifest.json"):
            shutil.copy(BASE / "demo_pack" / f, demo / f)
    return demo


def ensure_image(pack):
    if not (pack / "transform.json").exists() or not (pack / "image" / "slide.dzi").exists():
        from pack.make_standin import build
        print(f"building the stand-in image for {pack} (first start only)…", flush=True)
        build(pack)


def free_port(start):
    for port in range(start, start + 50):
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) != 0:
                return port
    raise RuntimeError("no free port")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pack", default=None)
    ap.add_argument("--port", type=int, default=8010)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--results", default=None, help="where Save writes (default: <pack>/results)")
    ap.add_argument("--image-transform", default=None,
                    help="transform.json for a registered H&E (made by the registration step)")
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()
    if FROZEN and (sys.stdout is None or not sys.stdout.isatty()):
        _log_to_file()
    pack = choose_pack(args.pack)
    ensure_image(pack)
    from server.shell import create_shell     # start screen + settings around the review app
    app = create_shell(pack, args.results, args.image_transform, pack_from_cli=bool(args.pack), host=args.host)
    port = free_port(args.port)
    url = f"http://{args.host}:{port}/"
    print(f"PathQC on {url}  (pack {pack})", flush=True)
    if not args.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    app.run(host=args.host, port=port, threaded=True)


if __name__ == "__main__":
    main()
