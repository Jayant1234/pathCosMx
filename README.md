# PathQC — pathologist review of cell-type calls

Five synced panels of one CosMx slide: **H&E** · **`<YourName>_labels`** · **Bill_eugene_code** (the lab's
annotation) · **Vestrum** · **Claude Code**. A pathologist pins comments on coordinates, picks the method that looks most accurate
for a region as a working base, relabels on top, draws named annotations, and saves a corrected labels
file and a whole-slide image. Built from the coworker's Pathogene app (`vendor/pathogene/`, upstream
commit in `UPSTREAM_COMMIT.txt`): its OpenSeadragon viewer, DZI tiling and H&E registration code.

## Get the app (pathologists)

1. Download the latest build: **Actions → Build PathQC app → latest run → Artifacts →
   `PathQC-macos-arm64`** (Apple Silicon Macs) or `PathQC-linux-x64`.
2. **Mac:** unzip, move `PathQC.app` to Applications. The app is not signed by Apple yet, so the first
   time: right-click → **Open** → **Open**. On macOS 15+, if that is not offered: System Settings →
   Privacy & Security → **Open Anyway**. After that it opens with a double-click.
3. The app opens in your browser. With no data it shows a **synthetic demo slide** (banner "DEMO").
4. **Real data** is sent separately (a ~3 MB slide pack, never on GitHub): put the folder in
   `~/Documents/PathQC/packs/<name>/` and restart the app. Your work and saved results stay in that folder.
   The app's log is `~/Documents/PathQC/pathqc.log`.

## Run from source (developers)

```bash
python -m venv ~/envs/pathqc && ~/envs/pathqc/bin/pip install -r requirements.txt
~/envs/pathqc/bin/python run.py              # opens http://127.0.0.1:8010 (next free port if taken)
# on a remote machine: ssh -L 8010:127.0.0.1:8010 <host>, then open the URL locally
```

Build the app locally: `pip install -r packaging/requirements-app.txt && bash packaging/build_app.sh`,
then `python packaging/smoke_test.py dist/PathQC/PathQC` (Linux) or `dist/PathQC.app/Contents/MacOS/PathQC`
(macOS). GitHub Actions (`.github/workflows/app.yml`) does both on every push to `main`.

## Build a slide pack (our side, once per slide)

```bash
python pack/build_pack.py --sandbox <synthesis_agent>/jayant_agent/sandbox \
       --coords <dir with coords_dev.tsv, coords_test.tsv> --out packs/hnsc_slide1
python pack/make_standin.py --pack packs/hnsc_slide1  # stand-in image until the H&E arrives
python pack/make_demo_pack.py --out demo_pack         # synthetic pack (what CI bundles)
```

A pack is `cells.tsv.gz` (one row per cell: id, section, split, slide mm, the three labels), `manifest.json`
(chosen runs and their ARIs, vocabulary) and, once built, `transform.json` + `image/`. Real packs hold
patient-derived data: `packs/` is git-ignored and must never be pushed to this public repository.

`build_pack.py` picks one run per method and split: the candidate with the highest ARI against
Bill_eugene_code (all candidates and their ARIs are in `manifest.json`). Current picks: Vestrum dev
rep81 (0.708) + held-out rep902 (0.679; cut at 90 min, labels complete); Claude Code dev rep1 (0.637) +
held-out rep952 (0.655). S1 and S4 are the dev sections: Vestrum's knowledge file was written from them,
so the app flags them.

## Using the app

Five synced panels: the **slide** (bare) and **`<YourName>_labels`** on top, **Bill_eugene_code**,
**Vestrum** and **Claude Code** below. `<YourName>_labels` is built from your selections: cells you have
not picked or relabelled show faint (header switch: faint / as Bill_eugene_code / hidden) and are saved
as Bill_eugene_code by default. The three method panels never change.

1. **Draw a region**: <kbd>Shift</kbd>+drag on any panel (or the Draw region tool, or right-click → Draw region here).
2. **Pick the labels to start from**: the box next to the region lists Bill_eugene_code / Vestrum / Claude Code
   with how much each already agrees with your layer. Or right-click inside the region on the panel that looks
   right → *Use this panel's labels here*. The choice is logged; your layer's panel outlines it in that method's colour.
3. **Fix on top**: relabel the region, or only the cells you have as one class; "+ new label…" creates a label.

Whichever panel you draw on, every change goes to your layer. A region you never use disappears when you click
elsewhere or press Esc. **Clear** drops the current selection; **Reset to initial…** (Log tab, type your name to
confirm) archives the whole session to `sessions/_archive/` and starts again from Bill_eugene_code.

Right-click menu, by what is under the pointer: region (use this panel's labels, relabel, comment, discard) ·
comment (edit, delete) · annotation (rename, delete) · past edit (select again, undo this edit) · cell (show
labels, relabel this cell, comment) · anywhere (draw region, comment, annotate here).

| Key | Action |
|---|---|
| V / E / C / R / A | Pan · Draw region · Comment point · Comment area · Annotate |
| Shift+drag | Draw a region from any tool |
| D | Only cells where the three methods disagree |
| Esc | Cancel drawing / back to Pan / clear selection |
| Ctrl+Z | Undo the last edit (the Log tab undoes any single edit) |

Undoing an earlier edit does not recompute later ones: each edit keeps the cells it recorded when it was made.

## Save → `packs/<pack>/results/<reviewer>_<time>/`

| File | Content |
|---|---|
| `<YourName>_labels.tsv` | every cell: `final_label`, `source` (bill_eugene_code-default / vestrum-base / claude_code-base / bill_eugene_code-base / manual), op id, time, comment ids, the three original labels, slide mm and image px |
| `base_picks.tsv` | each edit: which method was copied as base (or relabel target), filter, cell count, sections, dev flag |
| `corrected_slide.tif` | pyramidal tiled TIFF: slide + corrected labels + annotations + comment pins (opens in QuPath) |
| `comments.geojson`, `annotations.geojson` | QuPath-readable, image px, with slide-mm coordinates in the properties |
| `ops.jsonl`, `vocab.json`, `vocab_log.json`, `summary.json` | full edit log, final vocabulary, totals |

## Code map

| Path | Role |
|---|---|
| `run.py` | chooses the pack (`--pack` → saved in settings → user folder → checkout → bundled demo), builds the stand-in on first start, starts the server, opens the browser |
| `server/pack.py` | loads a pack; mm ↔ image-px transform; point-in-polygon in slide mm (shapely); binary cell payload |
| `server/session.py` | per-reviewer state. **Append-only edit log**: corrected label = Bill_eugene_code, then each non-undone op in order (last wins). Geometry is stored in slide mm so work survives the image swap. Vocabulary with merges |
| `server/app.py` | routes: tiles, `cells.bin`, `final.bin`, `/api/region` (stats), `/api/ops`, `/api/undo`, comments, annotations, vocab, save |
| `server/shell.py` | start screen + setup API; rebuilds the review app when the pack or image changes; H&E tile route |
| `server/settings.py`, `server/picker.py` | per-user settings + H&E fingerprint; native file/folder dialog with paste fallback |
| `server/he.py`, `server/register.py` | H&E reader (OpenSlide, pyvips fallback), DZI tiler, export bands; registration (Pathogene coarse + ECC fine, anchors) |
| `server/export.py` | results folder; the slide image is painted band by band and streamed to a pyramidal TIFF (memory ≈ one band) |
| `web/app.js` | five OpenSeadragon viewers on one tile set, leader→follower sync, canvas overlays with a 256-px spatial grid index (only visible cells are drawn), freehand drawing, side panes |
| `pack/build_pack.py`, `pack/make_standin.py`, `pack/make_demo_pack.py`, `pack/cells_io.py` | pack builder; stand-in image from cell positions only (never labels); synthetic demo pack; cell table I/O |
| `packaging/` | PyInstaller build script, app requirements, end-to-end smoke test of the built app |
| `vendor/pathogene/` | the coworker's app, unchanged: registration (`auto_orientation.py`, `register_fine.py`, `register_anchors.py`), `make_dzi.py` |

## Start screen and the real H&E

The first start opens a **start screen**: your name; the slide pack (the one found is shown as connected;
*Choose another pack folder…*); **H&E (optional)**: *Locate H&E file…* (SVS, NDPI, MRXS, SCN, pyramidal TIFF:
whatever OpenSlide reads; plain TIFFs fall back to pyvips) or paste its full path; then **Continue with stand-in**
or **Continue with H&E → register**. Where no native file dialog exists (headless, over SSH) the paste field is
the way in; `server/picker.py` has a `provider` hook for a desktop shell's dialog.

Registration (`server/register.py`): **Automatic** runs Pathogene's mask registration (vendored, unchanged:
16-orientation centroid + grid search of scale/shift on tissue masks) on thumbnails, the CosMx side drawn
from cell positions, then an OpenCV ECC affine refinement at 1024 and 4096 px. **Anchor points**: click a
landmark on the H&E, then on the stand-in, ≥ 3 pairs (Pathogene's similarity fit, mirror allowed), optionally
refined the same way. Check the overlay (cells over the H&E, opacity slider), then **Accept**: the viewer
opens with the H&E as the slide panel ("H&E") and every overlay mapped through the new transform. Tiles come
from the H&E file on request (OpenSlide Deep Zoom, LRU cache); Save paints the whole-slide image over the H&E
at about 1 µm/px (`export.HE_EXPORT_UM`; `max_dim` overrides).

Your choices are kept in `~/Documents/PathQC/settings.json` (reviewer, pack, H&E path + fingerprint =
size + sha256 of the first 4 MB and last 1 MB, registrations per pack and fingerprint). The accepted transform
is `<pack>/he/<fingerprint id>/transform.json` (the stand-in's `transform.json` is not touched). Next start goes
straight to the viewer; **Change slide / H&E** in the header returns to the start screen. If the saved H&E has
moved or changed, the start screen says so; a re-located file with the same fingerprint reuses its registration.

Registration is one global affine. If sections were cut and mounted separately, per-section matrices
(`transform["sections"]`, already honoured for cell positions by `server/pack.py`) are the extension.
Tests without a real H&E: `tests/make_fake_he.py` (known transform), `tests/test_registration.py`,
`tests/ui_test.py --only B`.
