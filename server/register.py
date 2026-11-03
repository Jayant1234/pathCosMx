"""Register the H&E to the slide: an affine slide mm -> H&E level-0 px (continuous px, y down).

Vendored from Pathogene (vendor/pathogene/backend, upstream commit in vendor/pathogene/UPSTREAM_COMMIT.txt),
imported unchanged: auto_orientation.create_he_mask (H&E tissue mask: border/fiducial removal) and
register_anchors._compute_affine (anchor fit: similarity, RANSAC; here also tried mirrored).
The CosMx side is a cell-density mask rendered from cell positions in slide mm (never labels).
Coarse stage (ours): exhaustive mask search, mirror x 10-deg rotations x scales, translation by FFT
cross-correlation (no tissue centroids, so a missing piece, debris or a crop do not shift it); the few best
distinct placements go on. Fine stage: OpenCV ECC, MOTION_AFFINE, between a cell-density rendering and the H&E
optical density on the tissue both share, from each placement; a texture-only fit score (edges eroded away)
picks the winner and says how sure it is (status "confident" / "uncertain" + reason), then one ECC pass at
4096 px removes the remaining quantisation.

Global affine only. Per-section matrices (sections cut and mounted separately) are an extension:
transform["sections"][name] is already honoured by server/pack.py for cell positions; fitting them would be
the same ECC restricted to one section's cells (template mask = that section), and pack.px_to_mm would need
per-section inverses for drawn geometry.
"""
import importlib.util
import sys
import time
from pathlib import Path

import numpy as np

from . import he as he_io

BASE = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1]))
VENDOR = BASE / "vendor" / "pathogene" / "backend"
COARSE_SIZE = 512
SEARCH_SIZE = 256          # mask search resolution (long side, px)
ANGLE_STEP = 10           # rotation step of the search (deg); ECC absorbs the rest
ECC_SIZES = (1024, 4096)
MARGIN_MM = 0.3


def _vendor(name):
    key = "pathogene_" + name
    if key not in sys.modules:
        spec = importlib.util.spec_from_file_location(key, VENDOR / f"{name}.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[key] = mod
        spec.loader.exec_module(mod)
    return sys.modules[key]


def h3(m):
    m = np.asarray(m, np.float64)
    return m if m.shape == (3, 3) else np.vstack([m[:2], [0, 0, 1]])


def apply(m, x, y):
    m, x, y = np.asarray(m, np.float64), np.asarray(x, np.float64), np.asarray(y, np.float64)
    return m[0, 0] * x + m[0, 1] * y + m[0, 2], m[1, 0] * x + m[1, 1] * y + m[1, 2]


# ---------------------------------------------------------------- images
def render_cells(x_mm, y_mm, A, W, H, sigma):
    """Cell-density image (H, W) float32 for cells mapped by A (mm -> continuous px), blurred."""
    import cv2
    px, py = apply(A, x_mm, y_mm)
    ix, iy = np.floor(px).astype(np.int64), np.floor(py).astype(np.int64)
    k = (ix >= 0) & (ix < W) & (iy >= 0) & (iy < H)
    img = np.zeros((H, W), np.float32)
    np.add.at(img, (iy[k], ix[k]), 1.0)
    return cv2.GaussianBlur(img, (0, 0), sigma), (ix[k], iy[k])


def density_mask(x_mm, y_mm, A, W, H, sigma):
    import cv2
    d, (ix, iy) = render_cells(x_mm, y_mm, A, W, H, sigma)
    thr = 0.2 * np.median(d[iy, ix]) if ix.size else 1
    m = (d > thr).astype(np.uint8) * 255
    return cv2.morphologyEx(m, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))


def optical_density(rgb, sigma):
    import cv2
    od = -np.log10(np.clip(rgb.astype(np.float32), 1, 255) / 255.0).mean(axis=2)
    return cv2.GaussianBlur(od, (0, 0), sigma)


def _norm(a):
    a = a.astype(np.float32)
    return (a - a.mean()) / (a.std() + 1e-6)


# ---------------------------------------------------------------- coarse (mask search)
def _dist_um(A, B, xs, ys):
    """Median distance (um, in A's resolution) between where A and B put the cells xs, ys."""
    um = 1000.0 / np.sqrt(abs(np.linalg.det(np.asarray(A, float)[:2, :2])))
    (ax, ay), (bx, by) = apply(A, xs, ys), apply(B, xs, ys)
    return float(np.median(np.hypot(ax - bx, ay - by)) * um)


def _search(he, base, Cinv, k0, scales, poses, cxm, cym, diag):
    """For each scale x (mirror, angle): the best translation of the CosMx mask over the H&E mask by FFT
    cross-correlation. -> [(Dice, A mm -> `he` px 3x3, mirror, angle, scale)]."""
    import cv2
    fft = np.fft                                                        # (scipy is not bundled in the app)
    th, tw = he.shape
    he_sum = float(he.sum()) + 1e-6
    out = []
    for s in scales:
        k = k0 * s
        L = int(np.ceil(diag * k)) + 4
        shape = (cv2.getOptimalDFTSize(th + L - 1), cv2.getOptimalDFTSize(tw + L - 1))
        F_he = fft.rfft2(he, shape)
        for mirror, ang in poses:
            t = np.radians(ang)
            P = np.eye(3)                                               # mm -> template px (y down, centred)
            P[:2, :2] = k * np.array([[np.cos(t), -np.sin(t)], [np.sin(t), np.cos(t)]]) @ np.diag([-1.0 if mirror else 1.0, -1.0])
            P[:2, 2] = L / 2 - P[:2, :2] @ [cxm, cym]
            tm = cv2.GaussianBlur(cv2.warpAffine(base, (P @ Cinv)[:2], (L, L), flags=cv2.INTER_LINEAR), (0, 0), 1.5)
            ov = fft.irfft2(F_he * np.conj(fft.rfft2(tm, shape)), shape)        # ov[m] = sum_p he(p + m) tm(p)
            ov = np.roll(ov, (L - 1, L - 1), axis=(0, 1))[:th + L - 1, :tw + L - 1]
            iy, ix = np.unravel_index(int(np.argmax(ov)), ov.shape)
            A = np.array([[1, 0, ix - (L - 1)], [0, 1, iy - (L - 1)], [0, 0, 1]]) @ P
            out.append((2 * float(ov[iy, ix]) / (he_sum + float(tm.sum())), A, mirror, ang, float(s)))
    return out


def _mask(he_mask, d):
    import cv2
    m = (he_mask > 0).astype(np.float32)
    if d > 1:
        m = cv2.resize(m, (he_mask.shape[1] // d, he_mask.shape[0] // d), interpolation=cv2.INTER_AREA)
    return cv2.GaussianBlur(m, (0, 0), 1.5)


def coarse(x_mm, y_mm, thumb_rgb, thumb_scale, mpp=None, log=print, progress=None, keep=4):
    """Exhaustive tissue-mask search, no tissue centroids (a torn-out piece, debris or a crop do not move it):
    every mirror x rotation (ANGLE_STEP) x scale at SEARCH_SIZE, the translation by FFT cross-correlation of
    the blurred masks (scored by Dice). Scale: the H&E's mpp when known (+-10 %), else around the tissue-area
    ratio (0.7-1.4x). The best `keep` DISTINCT placements are then searched finely (1 deg, 1 %) on the full
    thumbnail. Returns them best first, each with "A": mm -> H&E level-0 px, orientation, scale, mask score."""
    import cv2
    ao = _vendor("auto_orientation")
    he_mask = ao.create_he_mask(cv2.cvtColor(thumb_rgb, cv2.COLOR_RGB2BGR))
    d = max(1, int(round(max(he_mask.shape) / SEARCH_SIZE)))
    he_lo, he_hi = _mask(he_mask, d), _mask(he_mask, 1)
    # the CosMx tissue mask once, in mm (kb px/mm); templates are warped from it
    X0, X1 = x_mm.min() - MARGIN_MM, x_mm.max() + MARGIN_MM
    Y0, Y1 = y_mm.min() - MARGIN_MM, y_mm.max() + MARGIN_MM
    cxm, cym, diag = (X0 + X1) / 2, (Y0 + Y1) / 2, np.hypot(X1 - X0, Y1 - Y0)
    kb = 1.5 * COARSE_SIZE / max(X1 - X0, Y1 - Y0)
    if mpp:
        kb = max(kb, 1.5 * 1000.0 / (mpp * thumb_scale[0]))
    C = np.array([[kb, 0, -X0 * kb], [0, kb, -Y0 * kb], [0, 0, 1]])        # mm -> base px (y as in mm)
    bw, bh = int(np.ceil((X1 - X0) * kb)), int(np.ceil((Y1 - Y0) * kb))
    base = density_mask(x_mm, y_mm, C, bw, bh, sigma=max(1.0, 0.025 * kb)).astype(np.float32) / 255
    Cinv = np.linalg.inv(C)
    if mpp:
        k0, scales = 1000.0 / (mpp * thumb_scale[0] * d), np.exp(np.linspace(-np.log(1.1), np.log(1.1), 7))
    else:
        k0, scales = np.sqrt(float(he_lo.sum()) / (float(base.sum()) / kb ** 2)), np.geomspace(0.7, 1.4, 19)
    poses = [(m, a) for m in (False, True) for a in range(0, 360, ANGLE_STEP)]
    log(f"coarse: H&E mask {he_lo.shape[1]}x{he_lo.shape[0]}, {len(scales)} scales around {k0:.1f} px/mm, "
        f"{len(poses)} orientations")
    found = []
    for j, s in enumerate(scales):
        found += _search(he_lo, base, Cinv, k0, [s], poses, cxm, cym, diag)
        if progress:
            progress(0.05 + 0.3 * (j + 1) / len(scales), f"searching orientations (scale {j + 1}/{len(scales)})")
    found.sort(key=lambda f: -f[0])
    rng = np.random.default_rng(0)
    sub = rng.choice(x_mm.size, min(2000, x_mm.size), replace=False)
    xs, ys = x_mm[sub], y_mm[sub]
    T = np.diag([d, d, 1.0])
    top = []
    for f in found:                                  # non-maximum suppression: one candidate per placement
        if not any(_dist_um(t[1], T @ f[1], xs, ys) < 0.1 * diag * 1000 for t in top):
            top.append((f[0], T @ f[1], f[2], f[3], f[4]))
            if len(top) == keep:
                break
    T0 = np.diag([thumb_scale[0], thumb_scale[1], 1.0])
    cands = []
    for score0, _, mirror, ang, s in top:            # fine: +-ANGLE_STEP/2 by 1 deg, +-3 % by 1 %, full thumbnail
        fine = _search(he_hi, base, Cinv, k0 * d, s * np.exp(np.linspace(-0.03, 0.03, 7)),
                       [(mirror, ang + da) for da in range(-ANGLE_STEP // 2, ANGLE_STEP // 2 + 1)], cxm, cym, diag)
        score, A, mirror, ang, s = max(fine, key=lambda f: f[0])
        cands.append({"rotation": ang % 360, "flip_x": mirror, "scale": round(s, 3), "mask_score": round(score, 4),
                      "A": (T0 @ A)[:2]})
        log(f"  candidate: mirror {mirror!s:5} rot {ang % 360:3d}  scale {s:.3f}  mask Dice {score0:.3f} -> {score:.3f}")
    if progress:
        progress(0.45, "placements found")
    return cands


# ---------------------------------------------------------------- fine (ECC)
def _tissue(he_rgb):
    """Non-glass pixels (the vendored mask's fiducial clean-up is slow on noisy large images and not needed here)."""
    import cv2
    m = (cv2.cvtColor(he_rgb, cv2.COLOR_RGB2GRAY) < 235).astype(np.uint8)
    m = cv2.morphologyEx(m, cv2.MORPH_OPEN, np.ones((5, 5), np.uint8))
    return cv2.morphologyEx(m, cv2.MORPH_CLOSE, np.ones((9, 9), np.uint8)) > 0


def ecc_refine(x_mm, y_mm, A0, he_rgb, he_scale, sigma_um=12.0, iters=150, log=print):
    """One ECC pass at the resolution of `he_rgb` (level-0 px per image px = he_scale), on the cells' support
    that is also H&E tissue (holes, folds and crops do not pull). Returns (A mm -> level-0 px, correlation)."""
    import cv2
    H, W = he_rgb.shape[:2]
    D = np.diag([1 / he_scale[0], 1 / he_scale[1], 1.0])
    A = D @ h3(A0)                                                       # mm -> this image
    um_px = 1000.0 / np.sqrt(abs(np.linalg.det(A[:2, :2])))
    sig = max(1.0, sigma_um / um_px)
    tmpl, _ = render_cells(x_mm, y_mm, A, W, H, sig)
    img = optical_density(he_rgb, sig)
    r = max(4, int(round(2 * sig)))
    support = cv2.dilate((tmpl > 0.05 * tmpl.max()).astype(np.uint8), np.ones((9, 9), np.uint8))
    support &= cv2.dilate(_tissue(he_rgb).astype(np.uint8), np.ones((2 * r + 1, 2 * r + 1), np.uint8))
    warp = np.eye(2, 3, dtype=np.float32)
    crit = (cv2.TERM_CRITERIA_COUNT | cv2.TERM_CRITERIA_EPS, iters, 1e-6)
    try:
        cc, warp = cv2.findTransformECC(_norm(tmpl), _norm(img), warp, cv2.MOTION_AFFINE, crit, support, 5)
    except cv2.error as e:
        log(f"  ECC at {W}x{H} did not converge ({str(e).splitlines()[-1][:80]})")
        return h3(A0)[:2], None
    sv = np.linalg.svd(warp[:, :2].astype(np.float64), compute_uv=False)
    if sv.min() < 0.85 or sv.max() > 1 / 0.85:                         # collapsed or blown up: not a fit
        log(f"  ECC at {W}x{H} rejected: scale change {sv.min():.2f}-{sv.max():.2f}")
        return h3(A0)[:2], None
    Sh = np.array([[1, 0, -0.5], [0, 1, -0.5], [0, 0, 1]])                # ECC works on pixel-centre indices
    A = np.linalg.inv(Sh) @ h3(warp) @ Sh @ A
    log(f"  ECC at {W}x{H} ({um_px:.1f} um/px): correlation {cc:.4f}")
    return (np.linalg.inv(D) @ A)[:2], float(cc)


def fit_score(x_mm, y_mm, A0, he_rgb, he_scale, sigma_um):
    """Correlation of cell density and H&E optical density INSIDE the tissue both share (edges eroded away,
    1-99 % clipped): texture only, so a wrong placement of a similar outline scores low."""
    import cv2
    H, W = he_rgb.shape[:2]
    A = np.diag([1 / he_scale[0], 1 / he_scale[1], 1.0]) @ h3(A0)
    sig = max(1.0, sigma_um / (1000.0 / np.sqrt(abs(np.linalg.det(A[:2, :2])))))
    tmpl, _ = render_cells(x_mm, y_mm, A, W, H, sig)
    img = optical_density(he_rgb, sig)
    r = max(2, int(round(3 * sig)))
    k = np.ones((2 * r + 1, 2 * r + 1), np.uint8)
    inner = cv2.erode((tmpl > 0.2 * np.median(tmpl[tmpl > 0])).astype(np.uint8), k) & cv2.erode(_tissue(he_rgb).astype(np.uint8), k)
    if inner.sum() < 200:
        return -1.0
    a, b = tmpl[inner > 0], img[inner > 0]
    a, b = (np.clip(v, *np.percentile(v, [1, 99])) for v in (a, b))         # debris, pen, folds: outliers
    return float(np.corrcoef(a, b)[0, 1])


def local_offsets(x_mm, y_mm, A0, he_rgb, he_scale, sigma_um=10.0, n=4, search_um=150.0):
    """Residual misfit of a global fit: the tissue split into n x n tiles, each tile's shift between the cell
    density and the H&E (normalised cross-correlation within +-search_um). -> shifts in um (one per usable tile). A good affine on an
    undeformed section gives a few um; a deformed section (stretch, fold) or a wrong fit gives tens or more."""
    import cv2
    H, W = he_rgb.shape[:2]
    A = np.diag([1 / he_scale[0], 1 / he_scale[1], 1.0]) @ h3(A0)
    um_px = 1000.0 / np.sqrt(abs(np.linalg.det(A[:2, :2])))
    sig = max(1.0, sigma_um / um_px)
    m = int(np.ceil(search_um / um_px))
    tmpl, _ = render_cells(x_mm, y_mm, A, W, H, sig)
    img = optical_density(he_rgb, sig)
    both = (tmpl > 0.2 * np.median(tmpl[tmpl > 0])) & _tissue(he_rgb)
    ys, xs = np.nonzero(both)
    if xs.size < 1000:
        return np.array([])
    gx = np.linspace(xs.min(), xs.max() + 1, n + 1).astype(int)
    gy = np.linspace(ys.min(), ys.max() + 1, n + 1).astype(int)
    out = []
    for i in range(n):
        for j in range(n):
            sl = (slice(gy[i], gy[i + 1]), slice(gx[j], gx[j + 1]))
            h, w = both[sl].shape
            if h < 32 or w < 32 or both[sl].mean() < 0.5:
                continue
            a, b = (np.clip(v[sl], *np.percentile(v[sl], [1, 99])).astype(np.float32) for v in (tmpl, img))
            if min(h, w) <= 2 * m + 16:
                continue
            res = cv2.matchTemplate(b, a[m:-m, m:-m], cv2.TM_CCOEFF_NORMED)      # shifts up to +-search_um
            _, peak, _, (px, py) = cv2.minMaxLoc(res)
            if peak > 0.1:
                out.append(np.hypot(px - m, py - m) * um_px)
    return np.array(out)


def accept_finer(A, A2, cc2, x_mm, y_mm, max_move_um=100.0):
    """Take the finer ECC pass when it converged with a usable correlation and moved the cells only a little
    (a large jump at fine resolution means it locked onto something else)."""
    if cc2 is None or cc2 < 0.3:
        return False
    um = 1000.0 / np.sqrt(abs(np.linalg.det(np.asarray(A, float)[:2, :2])))
    xs = np.array([x_mm.min(), x_mm.max(), x_mm.min(), x_mm.max()])
    ys = np.array([y_mm.min(), y_mm.min(), y_mm.max(), y_mm.max()])
    (ax, ay), (bx, by) = apply(A, xs, ys), apply(A2, xs, ys)
    return float(np.hypot(ax - bx, ay - by).max() * um) < max_move_um


# ---------------------------------------------------------------- full auto run
MIN_R = 0.3            # texture fit score (fit_score at ECC_SIZES[0], 25 um blur)
MIN_MARGIN = 0.15      # best vs the runner-up placement (a different answer), same score
DISTINCT_UM = 100.0    # placements further apart than this (median cell distance) are different answers
MPP_TOL = 0.05         # recovered um/px vs the file's mpp
MAX_LOCAL_UM = 15.0    # 90th percentile of the per-tile residual shifts (local_offsets at <= 2048 px)
MIN_TILES = 3          # tiles where the local check could measure a shift


def auto(slide, x_mm, y_mm, standin_A, mpp=None, log=print, progress=None):
    """slide: opened OpenSlide (or he.VipsSlide). Returns a result dict (see describe()) with
    status "confident" or "uncertain" (+ reason): confident only when the fit score is high, clearly beats
    every different placement, fits locally everywhere it can be checked, and (if the file has an mpp) the
    fitted resolution agrees with it."""
    import cv2
    t0 = time.time()
    progress = progress or (lambda f, m: None)
    progress(0.02, "reading the H&E thumbnail")
    thumb, tscale = he_io.thumbnail(slide, COARSE_SIZE)
    cands = coarse(x_mm, y_mm, thumb, tscale, mpp, log, progress)
    if not cands:
        raise RuntimeError("no tissue found in the H&E thumbnail")
    lo, lo_s = he_io.thumbnail(slide, ECC_SIZES[0])
    sub = np.random.default_rng(0).choice(x_mm.size, min(2000, x_mm.size), replace=False)
    xs, ys = x_mm[sub], y_mm[sub]
    tried = []
    for j, c in enumerate(cands):
        A, _ = ecc_refine(x_mm, y_mm, c["A"], thumb, tscale, sigma_um=60.0, log=log)        # wide capture first
        A, _ = ecc_refine(x_mm, y_mm, A, lo, lo_s, sigma_um=25.0, log=log)
        r = fit_score(x_mm, y_mm, A, lo, lo_s, 25.0)
        tried.append((r, c, A))
        log(f"  mirror {c['flip_x']} rot {c['rotation']}: fit score {r:.3f}")
        progress(0.45 + 0.35 * (j + 1) / len(cands), f"checked placement {j + 1}/{len(cands)}")
    tried.sort(key=lambda t: -t[0])
    r, best, A = tried[0]
    rival = next((t for t in tried[1:] if _dist_um(A, t[2], xs, ys) > DISTINCT_UM), None)
    margin = r - rival[0] if rival else 1.0
    progress(0.85, "fine alignment")
    hi, hi_s = he_io.thumbnail(slide, min(ECC_SIZES[1], max(slide.dimensions)))
    A2, cc2 = ecc_refine(x_mm, y_mm, A, hi, hi_s, sigma_um=10.0, log=log)
    if accept_finer(A, A2, cc2, x_mm, y_mm):
        A = A2
    progress(0.95, "checking the fit locally")
    k = min(1.0, 2048 / max(hi.shape[:2]))
    md = cv2.resize(hi, (int(hi.shape[1] * k), int(hi.shape[0] * k)), interpolation=cv2.INTER_AREA) if k < 1 else hi
    off = local_offsets(x_mm, y_mm, A, md, (hi_s[0] / k, hi_s[1] / k))
    p90 = float(np.percentile(off, 90)) if off.size else None
    progress(1.0, "done")
    res = describe(A, standin_A, method="auto")
    reasons = []
    if off.size < MIN_TILES:
        reasons.append("the fit could not be checked locally (too little matching texture)")
    elif p90 > MAX_LOCAL_UM:
        reasons.append(f"cells and tissue are offset locally by up to {p90:.0f} µm (deformed section or a "
                       f"poor fit: a single affine cannot match everywhere)")
    if r < MIN_R:
        reasons.append(f"low fit score (r = {r:.2f}, needs {MIN_R})")
    if margin < MIN_MARGIN:
        reasons.append(f"another placement {_dist_um(A, rival[2], xs, ys) / 1000:.1f} mm away fits almost as well "
                       f"(r = {rival[0]:.2f} vs {r:.2f})")
    if mpp and abs(res["um_per_px"] / mpp - 1) > MPP_TOL:
        reasons.append(f"fitted resolution {res['um_per_px']:.3f} µm/px disagrees with the file's {mpp:.3f} µm/px")
    res.update(score=round(float(r), 4), margin=round(float(margin), 4),
               local_um={"tiles": int(off.size), "p90": None if p90 is None else round(p90, 1),
                         "max": round(float(off.max()), 1) if off.size else None},
               coarse={k: best[k] for k in ("rotation", "flip_x", "scale", "mask_score")},
               seconds=round(time.time() - t0, 1), confident=not reasons,
               status="uncertain" if reasons else "confident", reason="; ".join(reasons))
    res["summary"] = f"auto · {res['orientation']} · {res['um_per_px']:.2f} µm/px · fit r={r:.2f}"
    return res


# ---------------------------------------------------------------- anchors
def anchors(pairs, standin_A, px_unit=1.0):
    """pairs: [{"mm": [x, y], "he": [px, py]}, ...] (>= 3). Similarity fit (Pathogene _compute_affine),
    tried as is and mirrored; the lower mean residual wins. px_unit = level-0 px per overview px where the
    points were clicked: Pathogene's RANSAC threshold (5 px) is applied in those units, as in Pathogene."""
    ra = _vendor("register_anchors")
    if len(pairs) < 3:
        raise ValueError("place at least 3 matching point pairs")
    src = np.array([p["mm"] for p in pairs], np.float64) * 1000.0           # micrometres
    dst = np.array([p["he"] for p in pairs], np.float64)
    U = np.diag([px_unit, px_unit, 1.0])
    dst_u = (dst / px_unit).tolist()
    best = None
    for mirror in (False, True):
        F = np.diag([-1.0 if mirror else 1.0, 1.0, 1.0])
        s = src * ([-1, 1] if mirror else [1, 1])
        try:
            M, n_in = ra._compute_affine(s.tolist(), dst_u)
        except RuntimeError:
            continue
        A = U @ M @ F @ np.diag([1000.0, 1000.0, 1.0])                       # mm -> um -> (mirror) -> px
        err = ra._reprojection_error(M, s.tolist(), dst_u)
        if best is None or err < best[0]:
            best = (err, A, n_in)
    if best is None:
        raise ValueError("these points do not define a transform; spread them over the tissue")
    err, A, n_in = best
    res = describe(A[:2], standin_A, method="anchors")
    px, py = apply(A, src[:, 0] / 1000, src[:, 1] / 1000)
    resid_px = np.hypot(px - dst[:, 0], py - dst[:, 1])
    res.update(residual_um=[round(float(r * res["um_per_px"]), 2) for r in resid_px],
               mean_residual_um=round(float(resid_px.mean() * res["um_per_px"]), 2), n_pairs=len(pairs), inliers=n_in)
    res["summary"] = f"anchors ({len(pairs)}) · {res['orientation']} · mean residual {res['mean_residual_um']:.1f} µm"
    res.update(confident=True, status="confident", reason="")
    return res


def describe(A, standin_A, method):
    """Orientation of the H&E relative to the stand-in, and the H&E's um/px implied by the fit."""
    A = np.asarray(A, np.float64)[:2]
    L = A[:, :2]
    R = L @ np.linalg.inv(np.asarray(standin_A, np.float64)[:, :2])        # stand-in px -> H&E px
    mirrored = bool(np.linalg.det(R) < 0)
    Rm = R @ np.diag([-1, 1]) if mirrored else R
    angle = float(np.degrees(np.arctan2(Rm[1, 0], Rm[0, 0]))) % 360
    um = 1000.0 / float(np.sqrt(abs(np.linalg.det(L))))
    o = f"rotated {angle:.0f}°" + (" + mirrored" if mirrored else "")
    return {"method": method, "mm_to_px": A.round(6).tolist(), "um_per_px": round(um, 4),
            "rotation_deg": round(angle, 2), "mirrored": mirrored, "orientation": o}


def transform_json(res, info, fp, dzi_name):
    return {
        "kind": "he",
        "description": f"H&E {info['name']} registered to slide mm ({res['method']})",
        "um_per_px": res["um_per_px"],
        "image": {"width": info["width"], "height": info["height"], "dzi": dzi_name,
                  "base": info["path"], "reader": info["reader"], "fingerprint": fp["id"]},
        "mm_to_px": res["mm_to_px"],
        "sections": {},
        "registration": dict({k: v for k, v in res.items() if k != "mm_to_px"},
                             he_path=info["path"], fingerprint=fp, accepted=time.strftime("%Y-%m-%dT%H:%M:%S")),
    }
