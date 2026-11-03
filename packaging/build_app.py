#!/usr/bin/env python3
"""Build the PathQC desktop app with PyInstaller, on macOS, Windows and Linux.

  python packaging/build_app.py          # run from anywhere; works in the repository root

Output: macOS dist/PathQC.app · Windows dist/PathQC/PathQC.exe · Linux dist/PathQC/PathQC.
Builds the synthetic demo pack first. Real packs are bundled only password-sealed (sealed/*.pqc, pack/seal.py).
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SEP = ";" if sys.platform == "win32" else ":"
DATA = ["web:web", "demo_pack/cells.tsv.gz:demo_pack", "demo_pack/manifest.json:demo_pack",
        "vendor/pathogene/backend/auto_orientation.py:vendor/pathogene/backend",
        "vendor/pathogene/backend/register_fine.py:vendor/pathogene/backend",
        "vendor/pathogene/backend/register_anchors.py:vendor/pathogene/backend"]
HIDDEN = ["_libvips", "openslide", "openslide.deepzoom", "webview"]
GUI = {"darwin": ["webview.platforms.cocoa"],
       "win32": ["webview.platforms.winforms", "webview.platforms.edgechromium", "clr"]}.get(sys.platform, [])
EXCLUDE = ["pyarrow", "matplotlib", "tkinter", "IPython", "PyQt5", "PyQt6", "PySide2", "PySide6", "qtpy"]


def main():
    subprocess.run([sys.executable, "pack/make_demo_pack.py", "--out", "demo_pack"], cwd=ROOT, check=True)
    data = DATA + [f"{f.relative_to(ROOT).as_posix()}:sealed" for f in sorted((ROOT / "sealed").glob("*.pqc"))]
    cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--windowed", "--name", "PathQC"]
    for d in data:
        src, dst = d.split(":")
        cmd += ["--add-data", f"{src}{SEP}{dst}"]
    for h in HIDDEN + GUI:
        cmd += ["--hidden-import", h]
    cmd += ["--collect-all", "openslide_bin", "--collect-submodules", "server", "--collect-submodules", "pack"]
    for x in EXCLUDE:
        cmd += ["--exclude-module", x]
    cmd += ["--osx-bundle-identifier", "org.pathcosmx.pathqc", "run.py"]
    subprocess.run(cmd, cwd=ROOT, check=True)
    if sys.platform == "darwin":                  # the window loads http://127.0.0.1: allow local http, sharp text
        import plistlib
        f = ROOT / "dist/PathQC.app/Contents/Info.plist"
        info = plistlib.loads(f.read_bytes())
        info.update(NSHighResolutionCapable=True, NSAppTransportSecurity={"NSAllowsLocalNetworking": True},
                    CFBundleDisplayName="PathQC")
        f.write_bytes(plistlib.dumps(info))
        subprocess.run(["codesign", "--force", "--deep", "--sign", "-", str(ROOT / "dist/PathQC.app")], check=True)   # re-seal (ad hoc)
    exe ={"darwin": "dist/PathQC.app/Contents/MacOS/PathQC", "win32": "dist/PathQC/PathQC.exe"}.get(sys.platform, "dist/PathQC/PathQC")
    if not (ROOT / exe).exists():
        sys.exit(f"build finished but {exe} is missing")
    out = ROOT / ("dist/PathQC.app" if sys.platform == "darwin" else "dist/PathQC")
    size = sum(f.stat().st_size for f in out.rglob("*") if f.is_file() and not f.is_symlink())
    print(f"built {exe}  ({size / 2**20:.0f} MB in {out.name})")


if __name__ == "__main__":
    main()
