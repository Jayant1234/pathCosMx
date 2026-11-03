#!/usr/bin/env python3
"""Start PathQC: a local server plus the app window (pywebview), or the web browser when no window can open.

  python run.py                                   # first real pack found, else the demo pack
  python run.py --browser                         # use the web browser instead of the app window
  python run.py --pack packs/hnsc_slide1 --port 8010 --no-browser    # server only (CI, remote use)
  python run.py --image-transform path/to/transform_he.json   # registered H&E instead of the stand-in

Packs are looked for in this order: --pack; ~/Documents/PathQC/packs/<name>/ (real data, delivered
separately); ./packs/<name>/ (developer checkout); the bundled synthetic demo pack.
Sessions and results are written inside the chosen pack folder.
The first start shows a start screen (reviewer, pack, optional H&E + registration); the choices are kept in
~/Documents/PathQC/settings.json, so later starts open the viewer directly (header: "Change slide / H&E").
The window is pywebview (macOS WKWebView, Windows Edge WebView2, Linux GTK/Qt when installed); closing it stops
the server. The running server's address is in ~/Documents/PathQC/server.json and pathqc.log.
"""
import argparse
import json
import os
import shutil
import socket
import sys
import threading
import webbrowser
from pathlib import Path

FROZEN = getattr(sys, "frozen", False)
BASE = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))   # bundled files or checkout
HOME = Path(os.environ.get("PATHQC_HOME") or Path.home() / "Documents" / "PathQC")   # user's data and work


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


def _file_types(exts):
    return (("Slides (" + ";".join("*" + e for e in exts) + ")",) if exts else ()) + ("All files (*.*)",)


def open_window(url, title="PathQC"):
    """The app window; blocks until it is closed. Raises when no GUI backend can start (then: browser)."""
    import webview
    from server import picker
    if sys.platform.startswith("linux") and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        raise RuntimeError("no display")
    win = webview.create_window(title, url, width=1500, height=950, min_size=(900, 600), text_select=True)

    def dialog(kind, title, exts):             # the start screen's file / folder chooser -> native dialog
        ft = webview.FileDialog
        r = win.create_file_dialog(ft.FOLDER if kind == "folder" else ft.OPEN, file_types=() if kind == "folder" else _file_types(exts))
        return (r[0] if isinstance(r, (list, tuple)) else r) if r else None

    picker.provider = dialog
    try:
        webview.start(private_mode=False, storage_path=str(HOME / "webview"))
    finally:
        picker.provider = None


def check_gui():
    """--check-gui: can this build open its window? Writes a JSON line to stdout (when there is one) and to
    ~/Documents/PathQC/check-gui.json (a windowed Windows exe has no stdout); exit 0 if the backend imports."""
    out = {"platform": sys.platform, "frozen": FROZEN}
    try:
        from webview._version import __version__
        out["webview"] = __version__
        mod = {"darwin": "webview.platforms.cocoa", "win32": "webview.platforms.winforms"}.get(sys.platform, "webview.platforms.gtk")
        __import__(mod)
        out["backend"] = mod
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
    HOME.mkdir(parents=True, exist_ok=True)
    (HOME / "check-gui.json").write_text(json.dumps(out))
    if sys.stdout:
        print(json.dumps(out), flush=True)
    return 0 if "backend" in out else 1


def main():
    if "--check-gui" in sys.argv:
        sys.exit(check_gui())
    ap = argparse.ArgumentParser()
    ap.add_argument("--pack", default=None)
    ap.add_argument("--port", type=int, default=8010)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--results", default=None, help="where Save writes (default: <pack>/results)")
    ap.add_argument("--image-transform", default=None,
                    help="transform.json for a registered H&E (made by the registration step)")
    ap.add_argument("--browser", action="store_true", help="open in the web browser, not the app window")
    ap.add_argument("--no-browser", action="store_true", help="server only: no window, no browser")
    args, _ = ap.parse_known_args()            # macOS may pass -psn_... to an app opened from Finder
    if FROZEN and (sys.stdout is None or not sys.stdout.isatty()):
        _log_to_file()
    pack = choose_pack(args.pack)
    ensure_image(pack)
    from werkzeug.serving import make_server
    from server.shell import create_shell     # start screen + settings around the review app
    app = create_shell(pack, args.results, args.image_transform, pack_from_cli=bool(args.pack), host=args.host)
    port = free_port(args.port)
    srv = make_server(args.host, port, app, threaded=True)
    url = f"http://{args.host}:{port}/"
    print(f"PathQC on {url}  (pack {pack})", flush=True)
    HOME.mkdir(parents=True, exist_ok=True)
    (HOME / "server.json").write_text(json.dumps({"url": url, "port": port, "pid": os.getpid(), "pack": str(pack)}))
    if args.no_browser:
        srv.serve_forever()
        return
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    if not args.browser:
        try:
            open_window(url)
            print("window closed: stopping", flush=True)
            srv.shutdown()
            return
        except Exception as e:
            print(f"no app window ({type(e).__name__}: {e}); opening the web browser instead", flush=True)
    webbrowser.open(url)
    try:
        threading.Event().wait()              # serve until Ctrl+C / the process is ended
    except KeyboardInterrupt:
        srv.shutdown()


if __name__ == "__main__":
    main()
