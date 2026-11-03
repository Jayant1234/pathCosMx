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

Five synced panels: the **slide** (bare) and **`<YourName>_labels`** on top, **Bill_eugene_code**, **Vestrum**
and **Claude Code** below. Every change goes to `<YourName>_labels`, whichever panel you work on; the three
method panels never change. Cells you never touch are saved as Bill_eugene_code (`bill_eugene_code-default`).

**Start, Restart, Clear (whole sections).** On an empty section the start card asks which labels to start
from. *Apply to all sections not started yet* fills only sections without picks or relabels; sections you
have worked on keep their work. **Restart from…** (layer header) overwrites the section in view, or all
sections after a dialog that names how many already have work. **Clear** empties the section (or all) — picks
and relabels go, comments and annotations stay, Undo brings it back, and the start card returns.
**Reset to initial…** (Log tab, type your name) archives the whole session to `sessions/_archive/`.

**Select, then fix.** Select cells with any tool, then in the box by the selection either use one method's
labels for them, or relabel — by default only the ticked classes (the dominant class is pre-ticked).

| Key | Action |
|---|---|
| V | Pan (Shift+drag lassos) |
| L (or E) · P · R · B · S | Lasso · Polygon (click corners; double-click or Enter closes) · Rectangle · Brush · single Cell |
| [ ] | Brush smaller / larger |
| W · K · N · G | Assist: Wand · Compartment · Novae region · Refine border (below) |
| Shift / Alt / Ctrl (⌘) + click or drag | add to / subtract from / (Assist tools) keep only inside the selection |
| 1–9 · Enter | choose the new label (recent first) · apply the relabel |
| Esc | close a menu, cancel a drawing, deselect, then back to Pan |
| C · M · A | Comment point · Comment area · Annotate |
| D | only cells where the methods disagree |
| J / Shift+J | Review tab: next not-done / previous hotspot |
| Ctrl+Z (⌘Z) | undo the last edit (the Log tab undoes any single edit) |

Right-click shows what is under the pointer: selection, comment, annotation, past edit (select again, undo),
cell (show labels, relabel, comment). Undoing an earlier edit does not recompute later ones.

### Assist tools (opt-in; each only makes a selection, you still confirm in the box)

All work on a per-section cell graph (Delaunay on slide mm; the *max gap* slider, default 30 µm on a real
slide, drops longer edges). Tools whose pack attribute is missing are hidden.

- **Wand W**: click a cell → the connected cells with the same label in *that panel's* layer (the slide panel
  and your layer use your labels). *Jump* crosses 0–2 cells of other labels.
- **Compartment K**: the connected epithelium (PanCK) or immune (CD45) region of the clicked cell; per-section
  Otsu threshold on log intensity, the sliders shift it. **Snap to compartment** (in the box) keeps the majority one.
- **Novae N**: connected cells of the clicked cell's Novae domain (5 / 10 / 20 domains).
- **Refine border G** (random walker): with a selection, place seeds — **click = inside, Alt+click = outside**
  (here Alt does *not* subtract), or set the *inside / outside* toggle in the Assist row and just click.
  Weights use PanCK/CD45, the label layer and the compartment. On real tissue it ties a plain PanCK threshold
  (median accuracy 0.88 vs 0.88 at epithelium/stroma borders) rather than beating it: it is best for
  mixed-label borders where the stains do not separate.
- **What is this?** (in the box): compartment, Novae domain, how the methods label it. **Suggest** pre-sets the
  label and ticks; it never applies.

**Review tab.** Two queues of disagreement hotspots: *Any agent ≠ Bill* (one Bill → agent confusion in one
Novae domain per item, adjacent cells only) and *Both agents agree, Bill differs* (1-cell gaps allowed).
Items are 20–1,500 cells (bigger patches are cut into spatial pieces), ranked by expected fixes (a cell counts
1 when both agents differ from Bill, ⅓ when one does). J jumps to the next item not done and selects it; an
item is done when one edit covers half of it.

**Efficiency log.** Each edit records how its selection was made: `tool`, `tools` (every tool that shaped it)
and `sel_ms` (selection start → apply) in `ops.jsonl`; `summary.json` → `efficiency_by_tool` gives ops, cells,
median ms and undone count per tool.

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
| `server/assist.py` | assisted selection: cell graph (cached in `sessions/_cache/*.npz`), wand / compartment / Novae floods, random walker, review queue, efficiency summary |
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
