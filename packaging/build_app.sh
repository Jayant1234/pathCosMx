#!/usr/bin/env bash
# Build the PathQC desktop app with PyInstaller (macOS: dist/PathQC.app; Linux: dist/PathQC/).
# Run from the repository root. Builds the synthetic demo pack first; real packs are never bundled.
set -euo pipefail
python pack/make_demo_pack.py --out demo_pack
SEP=":"
python -m PyInstaller --noconfirm --clean --windowed --name PathQC \
  --add-data "web${SEP}web" \
  --add-data "demo_pack/cells.tsv.gz${SEP}demo_pack" \
  --add-data "demo_pack/manifest.json${SEP}demo_pack" \
  --add-data "vendor/pathogene/backend/auto_orientation.py${SEP}vendor/pathogene/backend" \
  --add-data "vendor/pathogene/backend/register_fine.py${SEP}vendor/pathogene/backend" \
  --add-data "vendor/pathogene/backend/register_anchors.py${SEP}vendor/pathogene/backend" \
  --hidden-import _libvips \
  --hidden-import openslide --hidden-import openslide.deepzoom --collect-all openslide_bin \
  --collect-submodules server \
  --collect-submodules pack \
  --exclude-module pyarrow --exclude-module matplotlib --exclude-module tkinter --exclude-module IPython \
  --osx-bundle-identifier org.pathcosmx.pathqc \
  run.py
echo "built:"; ls dist
