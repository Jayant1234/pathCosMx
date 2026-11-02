/* PathQC viewer: the reviewer's label layer + three method panels, synced; review tools. */
(() => {
'use strict';

const SRC = ['bill_eugene_code', 'vestrum', 'claude_code'];
const BASECOL = { bill_eugene_code: '#c27c0e', vestrum: '#0f9aa8', claude_code: '#7a5cd6', relabel: '#d6336c' };
const CATEGORIES = ['Lineage wrong', 'Basal state (stem/core/ifn)', 'Squamous axis (Squam1/2/Keratin)',
  'Immune cells in epithelium', 'DC / myeloid over-call', 'Unknown / abstention', 'Boundary or segmentation artifact',
  'Bill_eugene_code wrong', 'Other'];
const GRID = 256;               // spatial index bucket, image px
const NUCLEUS_UM = 3.5;         // drawn dot radius at full zoom

const $ = s => document.querySelector(s);
const el = (tag, cls, html) => { const e = document.createElement(tag); if (cls) e.className = cls; if (html != null) e.innerHTML = html; return e; };
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const fmt = n => Number(n).toLocaleString();
const nCells = n => `${fmt(n)} cell${n === 1 ? '' : 's'}`;

const S = {
  reviewer: null, manifest: null, image: null, vocab: [], root: null, byCode: {},
  cells: null, final: null, tag: null, comments: [], annotations: [], ops: [],
  panels: [], mode: 'pan', oneShot: false, draw: null,
  region: null, regionPanel: 0, regionInfo: null, regionFilter: null, regionUsed: false,
  hidden: new Set(), disagree: false, showOps: true, dot: 1, wash: 0.55, layerAlpha: 0.9, untouched: 'faint',
  sel: null, syncing: false, secBoxes: [],
};
const layerName = () => `${S.reviewer.replace(/[^A-Za-z0-9_.-]+/g, '_')}_labels`;
const sourceName = k => S.manifest.sources.find(s => s.key === k)?.name ?? k;

// ---------------------------------------------------------------- API
function q(path) { return path + (path.includes('?') ? '&' : '?') + 'reviewer=' + encodeURIComponent(S.reviewer); }
async function api(path, opts = {}) {
  const r = await fetch(q(path), opts.body ? { method: opts.method || 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ reviewer: S.reviewer, ...opts.body }) } : { method: opts.method || 'GET' });
  if (!r.ok) { let m = r.statusText; try { m = (await r.json()).error || m; } catch (e) {} throw new Error(m); }
  return r.headers.get('content-type')?.includes('json') ? r.json() : r.arrayBuffer();
}
function toast(msg, ms = 3200) { const t = $('#toast'); t.textContent = msg; t.hidden = false; clearTimeout(toast.h); toast.h = setTimeout(() => t.hidden = true, ms); }
function fail(e) { console.error(e); toast('Error: ' + e.message, 6000); }

// ---------------------------------------------------------------- vocabulary
function setVocab(v) {
  S.vocab = v; S.byCode = {};
  v.forEach(x => S.byCode[x.code] = x);
  S.root = new Int16Array(256).map((_, i) => i);
  for (const x of v) { let c = x.code, seen = new Set(); while (S.byCode[c]?.merged_into != null && !seen.has(c)) { seen.add(c); c = S.byCode[c].merged_into; } S.root[x.code] = c; }
  S.colour = new Array(256).fill('#9aa3ad');
  for (const x of v) S.colour[x.code] = S.byCode[S.root[x.code]].color;
}
const nameOf = c => c === 255 || c == null || c < 0 ? 'not labelled' : (S.byCode[S.root[c]]?.name ?? '?');
const codeOfName = n => S.vocab.find(x => x.name === n)?.code;

// ---------------------------------------------------------------- loading
async function load() {
  const st = await api('/api/state');
  S.manifest = st.manifest; S.image = st.image; setVocab(st.vocab);
  S.comments = st.comments; S.annotations = st.annotations; S.ops = st.ops;
  $('#packname').textContent = `${S.manifest.dataset} · ${fmt(S.manifest.n_cells)} cells`;
  const b = $('#imgbadge');
  if (S.image.kind === 'standin') { b.textContent = 'Stand-in image from cell positions — not H&E'; b.className = 'badge'; b.title = S.image.description; }
  else { b.textContent = 'H&E · ' + (S.image.registration?.summary || 'registered'); b.className = 'badge ok'; }
  $('#reviewerName').textContent = S.reviewer;
  if (!S.cells) await loadCells();
  await loadFinal();
  if (!S.panels.length) buildPanels();
  S.panels.find(P => P.mine).titleEl.textContent = layerName();
  clearSelection(false);
  renderAll();
}

async function loadCells() {
  const buf = await api('/api/cells.bin');
  const n = new Uint32Array(buf, 0, 1)[0];
  let o = 4;
  const px = new Float32Array(buf, o, n); o += 4 * n;
  const py = new Float32Array(buf, o, n); o += 4 * n;
  const sec = new Uint8Array(buf, o, n); o += n;
  const src = SRC.map((_, k) => new Uint8Array(buf, o + k * n, n));
  S.cells = { n, px, py, sec, src };
  const W = S.image.width, H = S.image.height, gw = Math.ceil(W / GRID), gh = Math.ceil(H / GRID);
  const bucket = new Int32Array(n), count = new Int32Array(gw * gh + 1);
  for (let i = 0; i < n; i++) { const b = Math.min(gh - 1, Math.max(0, py[i] / GRID | 0)) * gw + Math.min(gw - 1, Math.max(0, px[i] / GRID | 0)); bucket[i] = b; count[b + 1]++; }
  for (let b = 0; b < gw * gh; b++) count[b + 1] += count[b];
  const start = count.slice(), order = new Int32Array(n);
  for (let i = 0; i < n; i++) order[start[bucket[i]]++] = i;
  S.index = { gw, gh, start: count, order };
  S.secBoxes = S.manifest.sections.map(name => ({ name, x0: Infinity, y0: Infinity, x1: -Infinity, y1: -Infinity, dev: S.manifest.splits.dev.includes(name) }));
  for (let i = 0; i < n; i++) { const b = S.secBoxes[sec[i]]; if (px[i] < b.x0) b.x0 = px[i]; if (px[i] > b.x1) b.x1 = px[i]; if (py[i] < b.y0) b.y0 = py[i]; if (py[i] > b.y1) b.y1 = py[i]; }
  const sj = $('#secJump'); sj.innerHTML = '<option value="">whole slide</option>';
  S.secBoxes.forEach((b, k) => sj.add(new Option(`${b.name}${b.dev ? ' (dev)' : ''}`, k)));
}

async function loadFinal() {
  const buf = await api('/api/final.bin');
  const n = S.cells.n;
  S.final = new Uint16Array(buf.slice(0, 2 * n));
  S.tag = new Uint8Array(buf.slice(2 * n));
}

// ---------------------------------------------------------------- panels
function panelSpecs() {
  const src = S.manifest.sources;
  const runLine = s => s.kind === 'agent'
    ? Object.entries(s.runs).map(([sp, r]) => `${sp === 'test' ? 'held-out' : sp} ${r.chosen.rep} · ARI ${r.chosen.ari.toFixed(3)}`).join(' | ')
    : 'lab annotation (reference)';
  const standin = S.image.kind === 'standin';
  return [
    { title: standin ? 'Stand-in (not H&E)' : 'H&E', sub: standin ? 'drawn from cell positions' : '', layer: null, cls: 'top' },
    { title: layerName(), sub: 'built from your selections', layer: 'corr', mine: true, cls: 'top' },
    { title: src[0].name, sub: runLine(src[0]), layer: 0, cls: 'bot' },
    { title: src[1].name, sub: runLine(src[1]), layer: 1, cls: 'bot' },
    { title: src[2].name, sub: runLine(src[2]), layer: 2, cls: 'bot' },
  ];
}

function buildPanels() {
  const grid = $('#grid');
  panelSpecs().forEach((spec, k) => {
    const p = el('div', `panel ${spec.cls}` + (spec.mine ? ' mine' : ''));
    const osdDiv = el('div', 'osd');
    const cv = el('canvas', 'ov');
    const ph = el('div', 'ph');
    ph.append(el('span', 't', esc(spec.title)));
    if (spec.mine) {
      const ctl = el('span', 's opac', `untouched <select title="Cells you have not picked or relabelled (saved as Bill_eugene_code by default)">
        <option value="faint">faint</option><option value="colour">as Bill_eugene_code</option><option value="hide">hidden</option></select>
        labels <input type="range" min="0" max="1" step="0.05" value="${S.layerAlpha}">`);
      ctl.querySelector('select').onchange = e => { S.untouched = e.target.value; redraw(); };
      ctl.querySelector('input').oninput = e => { S.layerAlpha = +e.target.value; redraw(); };
      ph.append(ctl);
    }
    ph.append(el('span', 's', esc(spec.sub)));
    p.append(osdDiv, cv, ph); grid.append(p);
    const viewer = OpenSeadragon({
      element: osdDiv, tileSources: S.image.dzi, showNavigationControl: false, showNavigator: k === 0,
      navigatorPosition: 'BOTTOM_RIGHT', navigatorSizeRatio: 0.16, animationTime: 0.25, blendTime: 0,
      maxZoomPixelRatio: 6, visibilityRatio: 0.3, constrainDuringPan: false, immediateRender: true,
      gestureSettingsMouse: { clickToZoom: false, dblClickToZoom: true },
    });
    const P = { k, el: p, viewer, cv, ctx: cv.getContext('2d'), layer: spec.layer, mine: !!spec.mine, titleEl: ph.querySelector('.t') };
    S.panels.push(P);
    viewer.addHandler('viewport-change', () => { syncFrom(P); redraw(); });
    viewer.addHandler('open', () => { viewer.viewport.goHome(true); redraw(); });
    viewer.addHandler('canvas-click', e => { if (e.quick && S.mode === 'pan') panClick(P, e.position); });
    attachPointer(P);
  });
  new ResizeObserver(redraw).observe(grid);
}

function syncFrom(P) {
  if (S.syncing) return;
  S.syncing = true;
  const z = P.viewer.viewport.getZoom(), c = P.viewer.viewport.getCenter();
  for (const o of S.panels) if (o !== P) { o.viewer.viewport.zoomTo(z, null, true); o.viewer.viewport.panTo(c, true); }
  S.syncing = false;
}

function fitImageRect(x0, y0, x1, y1) {
  const P = S.panels[0], ti = P.viewer.world.getItemAt(0); if (!ti) return;
  const pad = 0.06 * Math.max(x1 - x0, y1 - y0);
  P.viewer.viewport.fitBounds(ti.imageToViewportRectangle(x0 - pad, y0 - pad, x1 - x0 + 2 * pad, y1 - y0 + 2 * pad));
}
function flyTo(pts) { const xs = pts.map(p => p[0]), ys = pts.map(p => p[1]); const r = pts.length === 1 ? 250 : 0; fitImageRect(Math.min(...xs) - r, Math.min(...ys) - r, Math.max(...xs) + r, Math.max(...ys) + r); }

function mapping(P) {   // image px <-> panel screen px (linear, no rotation)
  const ti = P.viewer.world.getItemAt(0); if (!ti) return null;
  const a = ti.imageToViewerElementCoordinates(new OpenSeadragon.Point(0, 0));
  const b = ti.imageToViewerElementCoordinates(new OpenSeadragon.Point(1000, 1000));
  const s = (b.x - a.x) / 1000;
  return { s, ox: a.x, oy: a.y, toS: (x, y) => [a.x + x * s, a.y + y * s], toI: (x, y) => [(x - a.x) / s, (y - a.y) / s] };
}

// ---------------------------------------------------------------- drawing
let raf = 0;
function redraw() { if (!raf) raf = requestAnimationFrame(() => { raf = 0; S.panels.forEach(drawPanel); placePopover(); }); }

function layerArray(P) { return P.layer === 'corr' ? S.final : S.cells.src[P.layer]; }

function agreeAll(i) {
  const r = S.root, a = S.cells.src[0][i], b = S.cells.src[1][i], c = S.cells.src[2][i];
  return a !== 255 && b !== 255 && c !== 255 && r[a] === r[b] && r[b] === r[c];
}

function drawPanel(P) {
  const dpr = window.devicePixelRatio || 1, W = P.cv.clientWidth, H = P.cv.clientHeight;
  if (P.cv.width !== Math.round(W * dpr) || P.cv.height !== Math.round(H * dpr)) { P.cv.width = Math.round(W * dpr); P.cv.height = Math.round(H * dpr); }
  const ctx = P.ctx; ctx.setTransform(dpr, 0, 0, dpr, 0, 0); ctx.clearRect(0, 0, W, H);
  const m = mapping(P); if (!m || !S.cells) return;
  const alpha = P.mine ? S.layerAlpha : 1;
  if (P.layer != null && alpha > 0) {          // the bare slide panel draws no cells
    if (S.wash > 0) { ctx.fillStyle = `rgba(250,250,252,${S.wash * alpha})`; ctx.fillRect(0, 0, W, H); }
    drawCells(ctx, m, layerArray(P), W, H, alpha, P.mine ? S.untouched : null);
  }
  drawSections(ctx, m);
  drawShapes(P, ctx, m);
}

function drawCells(ctx, m, arr, W, H, alpha, untouched) {
  const { px, py } = S.cells, { gw, gh, start, order } = S.index;
  const [ix0, iy0] = m.toI(0, 0), [ix1, iy1] = m.toI(W, H);
  const bx0 = Math.max(0, ix0 / GRID | 0), bx1 = Math.min(gw - 1, ix1 / GRID | 0);
  const by0 = Math.max(0, iy0 / GRID | 0), by1 = Math.min(gh - 1, iy1 / GRID | 0);
  const r = Math.min(9, Math.max(1.1, NUCLEUS_UM / (S.image.um_per_px || 1) * m.s)) * S.dot;
  const bins = new Map();
  for (let by = by0; by <= by1; by++) for (let bx = bx0; bx <= bx1; bx++) {
    const b = by * gw + bx;
    for (let j = start[b]; j < start[b + 1]; j++) {
      const i = order[j], c = arr[i];
      let root = c === 255 ? 255 : S.root[c];
      if (untouched && S.tag[i] === 0) {        // reviewer layer: cells not yet picked or relabelled
        if (untouched === 'hide') continue;
        if (untouched === 'faint') root = -1;
      }
      if (S.hidden.has(root)) continue;
      if (S.disagree && agreeAll(i)) continue;
      let list = bins.get(root); if (!list) bins.set(root, list = []);
      list.push(m.ox + px[i] * m.s, m.oy + py[i] * m.s);
    }
  }
  ctx.globalAlpha = (r < 1.5 ? 0.95 : 0.82) * alpha;
  for (const [root, pts] of bins) {
    ctx.globalAlpha = (root === -1 ? 0.35 : (r < 1.5 ? 0.95 : 0.82)) * alpha;
    ctx.fillStyle = root === -1 ? '#aab1ba' : root === 255 ? '#b9bec5' : S.colour[root];
    if (r < 1.6) { const d = Math.max(1, r * 1.8); for (let j = 0; j < pts.length; j += 2) ctx.fillRect(pts[j] - d / 2, pts[j + 1] - d / 2, d, d); }
    else { ctx.beginPath(); for (let j = 0; j < pts.length; j += 2) { ctx.moveTo(pts[j] + r, pts[j + 1]); ctx.arc(pts[j], pts[j + 1], r, 0, 6.2832); } ctx.fill(); }
  }
  ctx.globalAlpha = 1;
  if (S.sel != null) { const [sx, sy] = m.toS(px[S.sel], py[S.sel]); ctx.strokeStyle = '#ff2d55'; ctx.lineWidth = 2; ctx.beginPath(); ctx.arc(sx, sy, Math.max(7, r + 4), 0, 6.2832); ctx.stroke(); }
}

function drawSections(ctx, m) {
  ctx.font = '600 12px ' + getComputedStyle(document.body).fontFamily;
  for (const b of S.secBoxes) {
    const [sx, sy] = m.toS(b.x0, b.y0);
    ctx.fillStyle = b.dev ? '#a8650c' : '#6c7784';
    ctx.fillText(b.name + (b.dev ? ' · dev' : ''), sx, sy - 6);
  }
}

function path(ctx, m, pts, close = true) {
  ctx.beginPath(); pts.forEach((p, j) => { const [x, y] = m.toS(p[0], p[1]); j ? ctx.lineTo(x, y) : ctx.moveTo(x, y); }); if (close) ctx.closePath();
}
const opColour = o => o.op === 'base' ? BASECOL[o.source] : BASECOL.relabel;

function drawShapes(P, ctx, m) {
  if (S.showOps) for (const o of S.ops) {
    path(ctx, m, o.geometry.px); ctx.strokeStyle = opColour(o);
    if (P.mine) { ctx.lineWidth = 2; ctx.stroke(); ctx.globalAlpha = 0.07; ctx.fillStyle = opColour(o); ctx.fill(); ctx.globalAlpha = 1; }
    else { ctx.setLineDash([5, 4]); ctx.lineWidth = 1.3; ctx.stroke(); ctx.setLineDash([]); }
  }
  for (const a of S.annotations) {
    const pts = a.geometry.px; path(ctx, m, pts); ctx.lineWidth = 2.2; ctx.strokeStyle = '#111'; ctx.stroke();
    ctx.lineWidth = 1; ctx.strokeStyle = '#fff'; ctx.stroke();
    const [x, y] = m.toS(Math.min(...pts.map(p => p[0])), Math.min(...pts.map(p => p[1])));
    label(ctx, a.name, x, y - 4, '#111', '#fff');
  }
  S.comments.forEach((c, k) => {
    if (c.geometry.type === 'Polygon') { path(ctx, m, c.geometry.px); ctx.setLineDash([7, 4]); ctx.lineWidth = 2; ctx.strokeStyle = '#f2b300'; ctx.stroke(); ctx.setLineDash([]); }
    const [ax, ay] = anchor(c.geometry.px), [x, y] = m.toS(ax, ay);
    ctx.fillStyle = '#f2b300'; ctx.strokeStyle = '#5a4300'; ctx.lineWidth = 1.5;
    ctx.beginPath(); ctx.moveTo(x, y); ctx.lineTo(x - 7, y - 12); ctx.arc(x, y - 15, 7.6, Math.PI * 0.75, Math.PI * 0.25); ctx.closePath(); ctx.fill(); ctx.stroke();
    ctx.fillStyle = '#3a2b00'; ctx.font = '700 10px ' + getComputedStyle(document.body).fontFamily; ctx.textAlign = 'center'; ctx.fillText(String(k + 1), x, y - 11.5); ctx.textAlign = 'left';
  });
  if (S.region) { path(ctx, m, S.region); ctx.lineWidth = 2.5; ctx.strokeStyle = '#1f6fd1'; ctx.stroke(); ctx.fillStyle = 'rgba(31,111,209,.08)'; ctx.fill(); }
  if (S.draw && S.draw.pts.length) { path(ctx, m, S.draw.pts, false); ctx.lineWidth = 2; ctx.strokeStyle = '#ff2d55'; ctx.stroke(); }
}
const anchor = pts => [pts.reduce((s, p) => s + p[0], 0) / pts.length, pts.reduce((s, p) => s + p[1], 0) / pts.length];

function label(ctx, text, x, y, fg, bg) {
  if (!text) return;
  ctx.font = '600 12px ' + getComputedStyle(document.body).fontFamily;
  const w = ctx.measureText(text).width + 8;
  ctx.fillStyle = bg; ctx.globalAlpha = 0.85; ctx.fillRect(x, y - 14, w, 17); ctx.globalAlpha = 1;
  ctx.fillStyle = fg; ctx.fillText(text, x + 4, y - 1);
}

// ---------------------------------------------------------------- geometry helpers
function inPoly(pt, poly) {
  let inside = false;
  for (let i = 0, j = poly.length - 1; i < poly.length; j = i++) {
    const [xi, yi] = poly[i], [xj, yj] = poly[j];
    if ((yi > pt[1]) !== (yj > pt[1]) && pt[0] < (xj - xi) * (pt[1] - yi) / (yj - yi) + xi) inside = !inside;
  }
  return inside;
}
function nearestCell(ix, iy, rad) {
  const { gw, gh, start, order } = S.index, { px, py } = S.cells;
  let best = -1, bd = rad * rad;
  for (let by = Math.max(0, (iy - rad) / GRID | 0); by <= Math.min(gh - 1, (iy + rad) / GRID | 0); by++)
    for (let bx = Math.max(0, (ix - rad) / GRID | 0); bx <= Math.min(gw - 1, (ix + rad) / GRID | 0); bx++) {
      const b = by * gw + bx;
      for (let j = start[b]; j < start[b + 1]; j++) { const i = order[j], d = (px[i] - ix) ** 2 + (py[i] - iy) ** 2; if (d < bd) { bd = d; best = i; } }
    }
  return best >= 0 ? best : null;
}
function hits(P, sx, sy) {      // everything under a screen point of one panel
  const m = mapping(P), pt = m.toI(sx, sy), h = { pt };
  if (S.region && inPoly(pt, S.region)) h.region = true;
  h.comment = S.comments.findIndex(c => { const [ax, ay] = m.toS(...anchor(c.geometry.px)); return Math.hypot(ax - sx, ay - 15 - sy) < 12 || (c.geometry.type === 'Polygon' && inPoly(pt, c.geometry.px)); });
  h.annotation = S.annotations.findIndex(a => inPoly(pt, a.geometry.px));
  for (let k = S.ops.length - 1; k >= 0; k--) if (inPoly(pt, S.ops[k].geometry.px)) { h.op = S.ops[k]; break; }
  h.cell = nearestCell(pt[0], pt[1], 10 / m.s);
  return h;
}

// ---------------------------------------------------------------- modes, freehand drawing, clicks
const MODE_KIND = { pin: 'comment', area: 'comment', edit: 'region', annot: 'annotation' };
function setMode(mode, oneShot = false) {
  S.mode = mode; S.oneShot = oneShot; S.draw = null;
  document.querySelectorAll('#modes button').forEach(b => b.classList.toggle('on', b.dataset.mode === mode));
  S.panels.forEach(P => P.el.classList.toggle('drawing', mode !== 'pan'));
  redraw();
}

function attachPointer(P) {
  const pos = e => { const r = P.el.getBoundingClientRect(); return [e.clientX - r.left, e.clientY - r.top]; };
  // capture phase on the panel: runs before OpenSeadragon, so a draw gesture never pans the slide
  P.el.addEventListener('pointerdown', e => {
    if (e.button !== 0 || e.target.closest('.ph')) return;
    const kind = S.mode !== 'pan' ? S.mode : (e.shiftKey ? 'edit' : null);
    if (!kind) return;
    e.stopPropagation(); e.preventDefault();
    const [sx, sy] = pos(e), pt = mapping(P).toI(sx, sy);
    if (kind === 'pin') { finishDrawing('pin', P, { type: 'Point', px: [pt] }); return; }
    P.el.setPointerCapture(e.pointerId);
    S.draw = { P, kind, pts: [pt], last: [sx, sy] }; redraw();
  }, true);
  P.el.addEventListener('pointermove', e => {
    if (!S.draw || S.draw.P !== P) return;
    e.stopPropagation();
    const [sx, sy] = pos(e);
    if (Math.hypot(sx - S.draw.last[0], sy - S.draw.last[1]) < 4) return;
    S.draw.last = [sx, sy]; S.draw.pts.push(mapping(P).toI(sx, sy)); redraw();
  }, true);
  P.el.addEventListener('pointerup', e => {
    if (!S.draw || S.draw.P !== P) return;
    e.stopPropagation();
    const { pts, kind } = S.draw; S.draw = null;
    if (pts.length < 3) { redraw(); toast('Trace a closed shape: hold the button and draw around the area.'); return; }
    finishDrawing(kind, P, { type: 'Polygon', px: simplify(pts) });
  }, true);
  P.el.addEventListener('contextmenu', e => {
    if (e.target.closest('.ph')) return;
    e.preventDefault(); e.stopPropagation();
    const [sx, sy] = pos(e); contextMenu(P, hits(P, sx, sy), e.clientX, e.clientY);
  }, true);
}

function simplify(pts) {
  const xs = pts.map(p => p[0]), ys = pts.map(p => p[1]);
  const tol = Math.max(Math.max(...xs) - Math.min(...xs), Math.max(...ys) - Math.min(...ys)) / 400;
  const out = [pts[0]];
  for (const p of pts) { const l = out[out.length - 1]; if (Math.hypot(p[0] - l[0], p[1] - l[1]) > tol) out.push(p); }
  return out.map(p => [Math.round(p[0] * 10) / 10, Math.round(p[1] * 10) / 10]);
}

function finishDrawing(kind, P, geom) {
  if (kind === 'pin' || kind === 'area') commentDialog({ geometry: geom });
  else if (kind === 'annot') annotationDialog({ geometry: geom });
  else setRegion(geom.px, P.k);
  if (S.oneShot) setMode('pan');
  redraw();
}

function panClick(P, pos) {
  const h = hits(P, pos.x, pos.y);
  if (S.region && !h.region) clearRegion();            // click outside the region drops it
  if (h.comment >= 0 && !h.region) { commentDialog(S.comments[h.comment]); return; }
  S.sel = h.cell; redraw();
  if (S.sel == null) { $('#cellBox').hidden = true; return; }
  api('/api/cell/' + S.sel).then(showCell).catch(fail);
}

function clearSelection(withToast = true) {
  clearRegion(); S.sel = null; S.draw = null; $('#cellBox').hidden = true; closeMenu(); redraw();
  if (withToast) toast('Selection cleared');
}

// ---------------------------------------------------------------- cell inspector
function showCell(c) {
  const box = $('#cellBox'); box.hidden = false;
  const lab = name => { if (!name) return '<i>not labelled</i>'; const code = codeOfName(name); return `<span class="sw" style="background:${code != null ? S.colour[code] : '#b9bec5'}"></span> <b>${esc(nameOf(code))}</b>`; };
  box.innerHTML = `<h4><span>Cell ${esc(c.cell_id)}</span><button class="btn small" id="cellClose">×</button></h4>
    <div class="kv">
      <span>Section</span><span>${c.section} ${c.split === 'dev' ? '<span class="tag dev">dev</span>' : '<span class="tag">held-out</span>'}</span>
      <span>${esc(layerName())}</span><span>${lab(c.corrected)} <span class="mono">${esc(c.source)}</span></span>
      <span>Bill_eugene_code</span><span>${lab(c.labels.bill_eugene_code)}</span>
      <span>Vestrum</span><span>${lab(c.labels.vestrum)}</span>
      <span>Claude Code</span><span>${lab(c.labels.claude_code)}</span>
      <span>Slide mm</span><span class="mono">${c.x_mm.toFixed(4)}, ${c.y_mm.toFixed(4)}</span>
    </div>
    <div class="row"><button class="btn small" id="cellRelabel">Relabel this cell</button><button class="btn small" id="cellComment">Comment on this cell</button></div>`;
  $('#cellClose').onclick = () => { box.hidden = true; S.sel = null; redraw(); };
  $('#cellRelabel').onclick = () => relabelDialog({ cells: [c.index] });
  $('#cellComment').onclick = () => commentDialog({ geometry: { type: 'Point', px: [[S.cells.px[c.index], S.cells.py[c.index]]] }, cell_id: c.cell_id });
}

// ---------------------------------------------------------------- region: popover by the region, details in the side pane
function setRegion(px, panelK) {
  S.region = px; S.regionPanel = panelK; S.regionFilter = null; S.regionUsed = false; S.regionInfo = null; S.regionClasses = null;
  showTab('edit'); refreshRegion();
}
function clearRegion() { S.region = null; S.regionInfo = null; S.regionFilter = null; $('#pop').hidden = true; renderEdit(); redraw(); }

async function refreshRegion() {
  if (!S.region) return;
  try { S.regionInfo = await api('/api/region', { body: { polygon: S.region, filter: S.regionFilter } }); }
  catch (e) { fail(e); S.regionInfo = null; }
  // the "limit to" choices always come from the unfiltered region
  if (S.regionInfo && S.regionFilter == null) S.regionClasses = S.regionInfo.composition.corrected;
  renderPopover(); renderEdit();
}

const compCount = (comp, k, c) => (comp[k].find(x => x[0] === c) || [0, 0])[1];
function sameAsNow(comp, k, n) { let a = 0; for (const [c, cnt] of comp.corrected) a += Math.min(cnt, compCount(comp, k, c)); return n ? Math.round(100 * a / n) : 0; }

function renderPopover() {
  const pop = $('#pop'), info = S.regionInfo;
  if (!S.region || !info) { pop.hidden = true; return; }
  const devSecs = Object.keys(info.sections).filter(s => S.manifest.splits.dev.includes(s));
  const filt = S.regionClasses || info.composition.corrected;
  pop.innerHTML = `<div class="ph2"><b>${fmt(info.n)} cells</b> <span>${Object.keys(info.sections).join(', ')}</span>
      <button class="x" data-a="discard" title="${S.regionUsed ? 'Done' : 'Discard region'}">×</button></div>
    ${devSecs.length ? `<div class="warnbox">Dev section ${devSecs.join(', ')}: Vestrum is in-sample here.</div>` : ''}
    <div class="lbl">Use labels from <span class="hint">→ ${esc(layerName())}</span></div>
    <div class="basebtns">${S.manifest.sources.map(s => `<button class="btn base" data-base="${s.key}" style="--bc:${BASECOL[s.key]}">
      <span><i class="dot"></i>${esc(s.name)}</span><span class="mono">${sameAsNow(info.composition, s.key, info.n)}% same as yours</span></button>`).join('')}</div>
    <div class="lbl">Limit to</div>
    <select data-a="filter"><option value="">all cells in the region</option>${filt.map(([c, n]) => `<option value="${c}" ${S.regionFilter === c ? 'selected' : ''}>only cells you have as ${esc(nameOf(c))} (${fmt(n)})</option>`).join('')}</select>
    <div class="row"><select data-a="to">${labelOptions()}</select><button class="btn" data-a="relabel">Relabel ${fmt(info.n)}</button></div>
    <div class="row"><button class="btn small" data-a="comment">Comment on region</button><button class="btn small" data-a="discard">${S.regionUsed ? 'Done' : 'Discard'}</button></div>`;
  pop.hidden = false;
  pop.querySelectorAll('[data-base]').forEach(b => b.onclick = () => applyOp({ op: 'base', source: b.dataset.base }));
  pop.querySelector('[data-a=filter]').onchange = e => { S.regionFilter = e.target.value === '' ? null : +e.target.value; refreshRegion(); };
  pop.querySelector('[data-a=relabel]').onclick = () => relabelPick(+pop.querySelector('[data-a=to]').value);
  pop.querySelector('[data-a=comment]').onclick = () => commentDialog({ geometry: { type: 'Polygon', px: S.region } });
  pop.querySelectorAll('[data-a=discard]').forEach(b => b.onclick = clearRegion);
  placePopover();
}

function placePopover() {
  const pop = $('#pop'); if (pop.hidden || !S.region) return;
  const P = S.panels[S.regionPanel], m = mapping(P); if (!m) return;
  const pr = P.el.getBoundingClientRect(), gr = $('#grid').getBoundingClientRect();
  const xs = S.region.map(p => m.toS(p[0], p[1])[0]), ys = S.region.map(p => m.toS(p[0], p[1])[1]);
  const w = pop.offsetWidth, h = pop.offsetHeight;
  let x = pr.left - gr.left + Math.max(...xs) + 12, y = pr.top - gr.top + Math.min(...ys);
  if (x + w > gr.width - 4) x = pr.left - gr.left + Math.min(...xs) - w - 12;
  x = Math.max(4, Math.min(gr.width - w - 4, x)); y = Math.max(4, Math.min(gr.height - h - 4, y));
  pop.style.left = x + 'px'; pop.style.top = y + 'px';
}

function labelOptions(selected) {
  return S.vocab.filter(v => v.merged_into == null).map(v => `<option value="${v.code}" ${v.code === selected ? 'selected' : ''}>${esc(v.name)}</option>`).join('')
    + '<option value="-1">+ new label…</option>';
}
async function ensureLabel(code) {      // "+ new label…" creates it first
  if (code !== -1) return code;
  const n = prompt('Name of the new label'); if (!n || !n.trim()) return null;
  const r = await api('/api/vocab', { body: { action: { action: 'add', name: n.trim() } } });
  setVocab(r.vocab); renderAll(); return codeOfName(n.trim());
}
async function relabelPick(code) {
  try { const c = await ensureLabel(code); if (c != null) applyOp({ op: 'relabel', to: c }); } catch (e) { fail(e); }
}

function renderEdit() {
  const pane = $('#editPane');
  if (!S.region || !S.regionInfo) {
    pane.innerHTML = `<h3>Build ${esc(layerName())}</h3>
      <ol class="steps">
        <li><b>Draw a region:</b> <kbd>Shift</kbd>+drag on any panel (or right-click → Draw region here, or the Draw region tool).</li>
        <li><b>Pick the labels to start from:</b> in the box by the region, choose the method that looks most accurate.
          Or right-click inside the region on that method's panel → <i>Use this panel's labels here</i>.</li>
        <li><b>Fix on top:</b> relabel the region, or only the cells you have as one class.</li>
      </ol>
      <p class="hint">Every choice lands in <b>${esc(layerName())}</b> (its own panel, next to the slide), whichever panel you drew on. Outlines there show the method you picked:
        ${S.manifest.sources.map(s => `<span class="keychip" style="--bc:${BASECOL[s.key]}">${esc(s.name)}</span>`).join(' ')} <span class="keychip" style="--bc:${BASECOL.relabel}">relabel</span></p>
      <p class="hint">Cells you never touch keep the Bill_eugene_code label (saved as <span class="mono">bill_eugene_code-default</span>).
        A region you draw and do not use disappears when you click elsewhere or press Esc.</p>`;
    return;
  }
  const info = S.regionInfo, comp = info.composition;
  const codes = new Set(); Object.values(comp).forEach(list => list.slice(0, 10).forEach(([c]) => codes.add(c)));
  const rows = [...codes].sort((a, b) => compCount(comp, 'corrected', b) - compCount(comp, 'corrected', a)).map(c => `<tr><td><span class="sw" style="background:${c < 0 ? '#b9bec5' : S.colour[c]}"></span> ${esc(nameOf(c))}</td>
    <td><b>${fmt(compCount(comp, 'corrected', c))}</b></td><td>${fmt(compCount(comp, 'bill_eugene_code', c))}</td><td>${fmt(compCount(comp, 'vestrum', c))}</td><td>${fmt(compCount(comp, 'claude_code', c))}</td></tr>`).join('');
  pane.innerHTML = `<h3>Region · ${fmt(info.n)} cells${S.regionFilter != null ? ` (only ${esc(nameOf(S.regionFilter))})` : ''}</h3>
    <p class="hint">Choose and relabel in the box next to the region. Counts per label:</p>
    <table class="comp"><tr><th>Label</th><th>Yours</th><th>Bill_e.</th><th>Vestrum</th><th>Claude</th></tr>${rows}</table>`;
}

async function applyOp(body) {
  try {
    const op = await api('/api/ops', { body: { ...body, polygon: body.cells ? undefined : S.region, filter: body.cells ? undefined : S.regionFilter } });
    S.ops.push(op); await loadFinal();
    toast(op.op === 'base' ? `${layerName()}: ${sourceName(op.source)} labels copied into ${nCells(op.n)}` : `${layerName()}: ${nCells(op.n)} relabelled to ${op.to_name}`);
    if (!body.cells) { S.regionUsed = true; S.regionFilter = null; await refreshRegion(); }
    else if (S.sel != null) api('/api/cell/' + S.sel).then(showCell).catch(fail);
    renderAll();
  } catch (e) { fail(e); }
}

async function undo(target) {
  try {
    const r = await api('/api/undo', { body: target ? { target } : {} });
    if (!r.undone) { toast('Nothing to undo'); return; }
    S.ops = S.ops.filter(o => o.id !== r.undone.id); await loadFinal();
    toast('Undid: ' + opText(r.undone)); if (S.region) await refreshRegion(); renderAll();
  } catch (e) { fail(e); }
}
const opText = o => o.op === 'base' ? `${sourceName(o.source)} labels · ${nCells(o.n)}` : `relabel → ${o.to_name} · ${nCells(o.n)}`;

// ---------------------------------------------------------------- right-click menu
function contextMenu(P, h, cx, cy) {
  const items = [];
  const add = (label, fn, cls = '') => items.push({ label, fn, cls });
  const sep = title => items.push({ sep: true, title });
  if (h.region) {
    sep(`Region · ${S.regionInfo ? fmt(S.regionInfo.n) + ' cells' : ''}`);
    const own = typeof P.layer === 'number' ? SRC[P.layer] : null;   // a method panel
    if (own) add(`Use this panel's labels here (${sourceName(own)})`, () => applyOp({ op: 'base', source: own }), 'strong');
    for (const k of SRC) if (k !== own) add(`Use ${sourceName(k)} labels here`, () => applyOp({ op: 'base', source: k }));
    add('Relabel region…', () => relabelDialog({ region: true }));
    add('Comment on region', () => commentDialog({ geometry: { type: 'Polygon', px: S.region } }));
    add(S.regionUsed ? 'Done with region' : 'Discard region', clearRegion);
  }
  if (h.comment >= 0) {
    const c = S.comments[h.comment];
    sep(`Comment #${h.comment + 1}`);
    add('Edit comment', () => commentDialog(c));
    add('Delete comment', () => deleteItem('comments', c), 'danger');
  }
  if (h.annotation >= 0) {
    const a = S.annotations[h.annotation];
    sep(`Annotation · ${a.name}`);
    add('Rename annotation', () => annotationDialog(a));
    add('Delete annotation', () => deleteItem('annotations', a), 'danger');
  }
  if (h.op && !h.region) {
    sep(`Edit · ${opText(h.op)}`);
    add('Select this region again', () => { setRegion(h.op.geometry.px, P.k); });
    add('Undo this edit', () => undo(h.op.id), 'danger');
  }
  if (h.cell != null) {
    sep('Cell');
    add('Show its labels', () => { S.sel = h.cell; redraw(); api('/api/cell/' + h.cell).then(showCell).catch(fail); });
    add('Relabel this cell…', () => relabelDialog({ cells: [h.cell] }));
    add('Comment on this cell', () => commentDialog({ geometry: { type: 'Point', px: [[S.cells.px[h.cell], S.cells.py[h.cell]]] } }));
  }
  sep('Here');
  add('Draw region here', () => { setMode('edit', true); toast('Drag to draw the region'); });
  add('Comment here', () => commentDialog({ geometry: { type: 'Point', px: [h.pt] } }));
  add('Annotate here', () => { setMode('annot', true); toast('Drag to draw the annotation'); });
  if (S.region || S.sel != null) add('Clear selection', () => clearSelection());

  const menu = $('#ctx'); menu.innerHTML = '';
  for (const it of items) {
    if (it.sep) { menu.append(el('div', 'msep', esc(it.title))); continue; }
    const b = el('button', 'mi ' + it.cls, esc(it.label));
    b.onclick = () => { closeMenu(); it.fn(); };
    menu.append(b);
  }
  menu.hidden = false;
  const w = menu.offsetWidth, hh = menu.offsetHeight;
  menu.style.left = Math.min(cx, innerWidth - w - 6) + 'px';
  menu.style.top = Math.min(cy, innerHeight - hh - 6) + 'px';
}
function closeMenu() { $('#ctx').hidden = true; }
document.addEventListener('pointerdown', e => { if (!e.target.closest('#ctx')) closeMenu(); }, true);

// ---------------------------------------------------------------- dialogs
function dialog(html, onSubmit) {
  const d = $('#dlg'), f = $('#dlgForm'); f.innerHTML = html;
  f.onsubmit = async e => { if (e.submitter?.value === 'cancel') return; e.preventDefault(); try { if (await onSubmit(new FormData(f)) !== false) d.close(); } catch (err) { fail(err); } };
  d.showModal(); f.querySelector('textarea,input[type=text]')?.focus();
}

function relabelDialog({ region, cells }) {
  const info = S.regionInfo;
  dialog(`<h3>Relabel ${cells ? 'this cell' : `the region (${fmt(info?.n || 0)} cells)`}</h3>
    <p class="hint">Changes ${esc(layerName())} only. The three methods stay as they are.</p>
    ${region && info ? `<label>Limit to</label><select name="filter"><option value="">all cells in the region</option>
      ${(S.regionClasses || info.composition.corrected).map(([c, n]) => `<option value="${c}" ${S.regionFilter === c ? 'selected' : ''}>only cells you have as ${esc(nameOf(c))} (${fmt(n)})</option>`).join('')}</select>` : ''}
    <label>New label</label><select name="to">${labelOptions()}</select>
    <div class="acts"><button class="btn" value="cancel" formnovalidate>Cancel</button><button class="btn primary">Relabel</button></div>`,
  async fd => {
    const to = await ensureLabel(+fd.get('to')); if (to == null) return false;
    if (cells) await applyOp({ op: 'relabel', to, cells });
    else { const f = fd.get('filter'); S.regionFilter = f ? +f : null; await applyOp({ op: 'relabel', to }); }
  });
}

const TARGETS = () => [['bill_eugene_code', 'Bill_eugene_code'], ['vestrum', 'Vestrum'], ['claude_code', 'Claude Code'], ['mine', layerName()]];
function commentDialog(item) {
  const isNew = !item.id;
  dialog(`<h3>${isNew ? 'New comment' : 'Edit comment'} · ${item.geometry.type === 'Point' ? 'point' : 'area'}</h3>
    <label>Category</label><select name="category">${CATEGORIES.map(c => `<option ${item.category === c ? 'selected' : ''}>${c}</option>`).join('')}</select>
    <label>About</label><div class="checks">${TARGETS().map(([k, n]) => `<label><input type="checkbox" name="t" value="${k}" ${(item.targets || []).includes(k) ? 'checked' : ''}> ${esc(n)}</label>`).join('')}</div>
    <label>Comment</label><textarea name="text" placeholder="What is wrong here, and what should it be?">${esc(item.text || '')}</textarea>
    <div class="acts">${isNew ? '' : '<button class="btn danger" value="delete" formnovalidate>Delete</button><span class="grow"></span>'}
      <button class="btn" value="cancel" formnovalidate>Cancel</button><button class="btn primary">Save comment</button></div>`,
  async fd => {
    const body = { ...item, category: fd.get('category'), targets: fd.getAll('t'), text: fd.get('text').trim() };
    if (!body.text) { toast('Write the comment first'); return false; }
    if (!isNew) delete body.geometry;
    const saved = await api('/api/comments', { body: { item: body } });
    const k = S.comments.findIndex(c => c.id === saved.id); k >= 0 ? S.comments[k] = saved : S.comments.push(saved);
    toast('Comment saved'); renderAll();
  });
  const del = $('#dlgForm button[value=delete]');
  if (del) del.onclick = e => { e.preventDefault(); $('#dlg').close(); deleteItem('comments', item); };
}

function annotationDialog(item) {
  const isNew = !item.id, names = [...new Set(S.annotations.map(a => a.name))];
  dialog(`<h3>${isNew ? 'New annotation' : 'Rename annotation'}</h3>
    <label>Name</label><input type="text" name="name" list="annNames" value="${esc(item.name || '')}" placeholder="e.g. tumour nest, ulcer, artifact" required>
    <datalist id="annNames">${names.map(n => `<option value="${esc(n)}">`).join('')}</datalist>
    <label>Note (optional)</label><textarea name="note">${esc(item.note || '')}</textarea>
    <div class="acts"><button class="btn" value="cancel" formnovalidate>Cancel</button><button class="btn primary">Save</button></div>`,
  async fd => {
    const body = { ...item, name: fd.get('name').trim(), note: fd.get('note').trim() };
    if (!isNew) delete body.geometry;
    const saved = await api('/api/annotations', { body: { item: body } });
    const k = S.annotations.findIndex(a => a.id === saved.id); k >= 0 ? S.annotations[k] = saved : S.annotations.push(saved);
    toast('Annotation saved'); renderAll();
  });
}

function deleteItem(kind, item) {
  if (!confirm(`Delete this ${kind === 'comments' ? 'comment' : 'annotation'}?`)) return;
  api(`/api/${kind}/${item.id}`, { method: 'DELETE' }).then(() => { S[kind] = S[kind].filter(x => x.id !== item.id); renderAll(); }).catch(fail);
}

function reviewerDialog(first) {
  dialog(`<h3>${first ? 'Welcome to PathQC' : 'Change reviewer'}</h3>
    <p class="hint">Your name labels every comment and edit; your layer is saved as <span class="mono">&lt;name&gt;_labels</span>. Each reviewer has a separate session.</p>
    <label>Reviewer name</label><input type="text" name="name" value="${esc(S.reviewer || '')}" required>
    <div class="acts">${first ? '' : '<button class="btn" value="cancel" formnovalidate>Cancel</button>'}<button class="btn primary">Start</button></div>`,
  async fd => {
    S.reviewer = fd.get('name').trim(); try { localStorage.setItem('pathqc.reviewer', S.reviewer); } catch (e) {}
    await load();
  });
}

function resetDialog() {
  dialog(`<h3>Reset to initial</h3>
    <div class="warnbox">This puts ${esc(layerName())} back to Bill_eugene_code everywhere and removes <b>all</b> your edits
      (${S.ops.length}), comments (${S.comments.length}), annotations (${S.annotations.length}) and label changes.</div>
    <p class="hint">Nothing is destroyed: the session is moved to <span class="mono">sessions/_archive/</span>, so it can be recovered.</p>
    <label>Type your reviewer name (<b>${esc(S.reviewer)}</b>) to confirm</label><input type="text" name="confirm" autocomplete="off" required>
    <div class="acts"><button class="btn" value="cancel" formnovalidate>Cancel</button><button class="btn primary danger-bg">Reset</button></div>`,
  async fd => {
    const r = await api('/api/reset', { body: { confirm: fd.get('confirm') } });
    toast('Reset done. Previous session archived to ' + r.archived_to, 6000);
    await load();
  });
}

function saveDialog() {
  dialog(`<h3>Save results</h3>
    <p class="hint">Writes a new results folder: <span class="mono">${esc(layerName())}.tsv</span> (every cell, your label and where it came from),
    <span class="mono">base_picks.tsv</span>, comments and annotations as GeoJSON, the edit log and the vocabulary.</p>
    <label><input type="checkbox" name="image" checked> Also write the whole-slide image (<span class="mono">corrected_slide.tif</span>, opens in QuPath)</label>
    <div id="saveProg"></div>
    <div class="acts"><button class="btn" value="cancel" formnovalidate>Close</button><button class="btn primary">Save</button></div>`,
  async fd => {
    const st = await api('/api/save', { body: { image: fd.get('image') === 'on' } });
    const prog = $('#saveProg');
    prog.innerHTML = `<p class="hint">Saved to <span class="mono">${esc(st.dir)}</span></p><div class="bar"><i style="width:0"></i></div>`;
    if (st.image === 'running') {
      const tick = async () => {
        const s = await api('/api/save/status');
        prog.querySelector('i').style.width = (100 * (s.progress || 0)) + '%';
        if (s.image === 'running') setTimeout(tick, 800);
        else prog.insertAdjacentHTML('beforeend', `<p class="hint">Slide image: ${s.image}${s.error ? ' — ' + esc(s.error) : ''}</p>`);
      };
      tick();
    } else prog.querySelector('i').style.width = '100%';
    return false;
  });
}

// ---------------------------------------------------------------- side panes
function showTab(t) {
  document.querySelectorAll('#tabs button').forEach(b => b.classList.toggle('on', b.dataset.tab === t));
  document.querySelectorAll('.pane').forEach(p => p.classList.toggle('on', p.dataset.pane === t));
}

function renderComments() {
  const pane = $('#commentsPane');
  $('#nComments').textContent = S.comments.length || '';
  if (!S.comments.length) { pane.innerHTML = '<p class="hint">No comments yet. Right-click → <b>Comment here</b>, or use <b>Comment point</b> (C) / <b>Comment area</b> (R).</p>'; return; }
  pane.innerHTML = '<div class="list"></div>';
  const T = Object.fromEntries(TARGETS());
  S.comments.forEach((c, k) => {
    const it = el('div', 'item', `<div class="meta"><b>#${k + 1}</b><span class="tag cat">${esc(c.category)}</span>
      ${(c.targets || []).map(t => `<span class="tag">${esc(T[t] || t)}</span>`).join('')}
      <span>${c.geometry.type === 'Point' ? 'point' : 'area'}</span><span>${esc(c.ts?.slice(5, 16).replace('T', ' '))}</span></div>
      <div class="txt">${esc(c.text)}</div><div class="acts"><button class="btn small" data-a="edit">Edit</button><button class="btn small danger" data-a="del">Delete</button></div>`);
    it.onclick = e => { const a = e.target.dataset.a; if (a === 'edit') commentDialog(c); else if (a === 'del') deleteItem('comments', c); else flyTo(c.geometry.px); };
    pane.firstChild.append(it);
  });
}

function renderAnnots() {
  const pane = $('#annotsPane');
  $('#nAnnots').textContent = S.annotations.length || '';
  if (!S.annotations.length) { pane.innerHTML = '<p class="hint">No annotations yet. Right-click → <b>Annotate here</b>, or use <b>Annotate</b> (A), then trace and name a shape.</p>'; return; }
  pane.innerHTML = '<div class="list"></div>';
  S.annotations.forEach(a => {
    const it = el('div', 'item', `<div class="meta"><b>${esc(a.name)}</b><span>${a.geometry.px.length} points</span><span>${esc(a.ts?.slice(5, 16).replace('T', ' '))}</span></div>
      ${a.note ? `<div class="txt">${esc(a.note)}</div>` : ''}<div class="acts"><button class="btn small" data-a="edit">Rename</button><button class="btn small danger" data-a="del">Delete</button></div>`);
    it.onclick = e => { const k = e.target.dataset.a; if (k === 'edit') annotationDialog(a); else if (k === 'del') deleteItem('annotations', a); else flyTo(a.geometry.px); };
    pane.firstChild.append(it);
  });
}

function renderLabels() {
  const pane = $('#labelsPane');
  const counts = new Map(); for (let i = 0; i < S.final.length; i++) { const r = S.root[S.final[i]]; counts.set(r, (counts.get(r) || 0) + 1); }
  const roots = S.vocab.filter(v => v.merged_into == null);
  pane.innerHTML = `<h3>Labels</h3><p class="hint">Click a name to rename it. Merging a label into another recolours and renames it in
    <b>all four panels</b>; the original labels stay in the saved file. Counts are for ${esc(layerName())}.</p>
    <table class="vocab">${S.vocab.map(v => `<tr data-c="${v.code}">
      <td><input type="color" value="${v.color}" ${v.merged_into != null ? 'disabled' : ''}></td>
      <td class="nm" title="Click to rename">${esc(v.name)}${v.added_by ? ' <span class="tag">new</span>' : ''}</td>
      <td><select><option value="">${v.merged_into == null ? 'merge into…' : 'unmerge'}</option>${roots.filter(r => r.code !== v.code).map(r => `<option value="${r.code}" ${v.merged_into === r.code ? 'selected' : ''}>→ ${esc(r.name)}</option>`).join('')}</select></td>
      <td class="n">${v.merged_into == null ? fmt(counts.get(v.code) || 0) : ''}</td></tr>`).join('')}</table>
    <div class="row"><input type="text" id="newLabel" placeholder="New label name" class="inp"><button class="btn" id="addLabel">Add</button></div>`;
  const act = async a => { try { const r = await api('/api/vocab', { body: { action: a } }); setVocab(r.vocab); renderAll(); if (S.region) refreshRegion(); } catch (e) { fail(e); } };
  pane.querySelectorAll('tr[data-c]').forEach(tr => {
    const code = +tr.dataset.c;
    tr.querySelector('input[type=color]').onchange = e => act({ action: 'recolor', code, color: e.target.value });
    tr.querySelector('.nm').onclick = () => { const v = S.byCode[code]; const n = prompt('Rename label', v.name); if (n && n.trim() !== v.name) act({ action: 'rename', code, name: n.trim() }); };
    tr.querySelector('select').onchange = e => act(e.target.value === '' ? { action: 'unmerge', code } : { action: 'merge', code, into: +e.target.value });
  });
  $('#addLabel').onclick = () => { const n = $('#newLabel').value.trim(); if (n) act({ action: 'add', name: n }); };
}

function renderLog() {
  const pane = $('#logPane');
  $('#nOps').textContent = S.ops.length || '';
  const byTag = new Map(); for (const t of S.tag) byTag.set(t, (byTag.get(t) || 0) + 1);
  const tn = { 0: 'Bill_eugene_code (default, untouched)', 1: 'relabelled by you', 2: 'Bill_eugene_code (picked)', 3: 'Vestrum (picked)', 4: 'Claude Code (picked)' };
  pane.innerHTML = `<h3>${esc(layerName())}</h3><table class="comp">${[...byTag].sort((a, b) => a[0] - b[0]).map(([t, n]) => `<tr><td>${tn[t]}</td><td>${fmt(n)}</td></tr>`).join('')}</table>
    <h3>Edits (${S.ops.length})</h3>${S.ops.length ? '' : '<p class="hint">No edits yet.</p>'}<div class="list"></div>
    <hr class="hr"><button class="btn danger" id="resetBtn">Reset to initial…</button>
    <p class="hint">Removes every edit, comment, annotation and label change of ${esc(S.reviewer)}. The session is archived, not deleted.</p>`;
  [...S.ops].reverse().forEach(o => {
    const it = el('div', 'item', `<div class="meta"><span class="keychip" style="--bc:${opColour(o)}">${o.op === 'base' ? 'picked' : 'relabel'}</span><span>${esc(opText(o))}</span>
      ${o.filter_name ? `<span class="tag">only ${esc(o.filter_name)}</span>` : ''}<span>${esc(o.ts?.slice(11, 16))}</span></div>
      <div class="acts"><button class="btn small" data-a="sel">Select region</button><button class="btn small danger" data-a="undo">Undo</button></div>`);
    it.onclick = e => { const a = e.target.dataset.a; if (a === 'undo') undo(o.id); else if (a === 'sel') { flyTo(o.geometry.px); setRegion(o.geometry.px, 0); } else flyTo(o.geometry.px); };
    pane.querySelector('.list').append(it);
  });
  $('#resetBtn').onclick = resetDialog;
}

function renderLegend() {
  const L = $('#legend'); L.innerHTML = '';
  for (const v of S.vocab.filter(v => v.merged_into == null)) {
    const it = el('span', 'it' + (S.hidden.has(v.code) ? ' off' : ''), `<span class="sw" style="background:${v.color}"></span>${esc(v.name)}`);
    it.onclick = () => { S.hidden.has(v.code) ? S.hidden.delete(v.code) : S.hidden.add(v.code); renderLegend(); redraw(); };
    L.append(it);
  }
  const nl = el('span', 'it' + (S.hidden.has(255) ? ' off' : ''), '<span class="sw" style="background:#b9bec5"></span>not labelled');
  nl.onclick = () => { S.hidden.has(255) ? S.hidden.delete(255) : S.hidden.add(255); renderLegend(); redraw(); };
  L.append(nl);
}

function renderAll() { renderEdit(); renderComments(); renderAnnots(); renderLabels(); renderLog(); renderLegend(); if (S.region && S.regionInfo) renderPopover(); redraw(); }

// ---------------------------------------------------------------- wiring
document.querySelectorAll('#modes button').forEach(b => b.onclick = () => setMode(b.dataset.mode));
document.querySelectorAll('#tabs button').forEach(b => b.onclick = () => showTab(b.dataset.tab));
$('#disagree').onchange = e => { S.disagree = e.target.checked; redraw(); };
$('#showOps').onchange = e => { S.showOps = e.target.checked; redraw(); };
$('#dotSize').oninput = e => { S.dot = +e.target.value; redraw(); };
$('#wash').oninput = e => { S.wash = +e.target.value; redraw(); };
$('#secJump').onchange = e => { if (e.target.value === '') { S.panels[0].viewer.viewport.goHome(); return; } const b = S.secBoxes[+e.target.value]; fitImageRect(b.x0, b.y0, b.x1, b.y1); };
$('#undoBtn').onclick = () => undo();
$('#clearBtn').onclick = () => clearSelection();
$('#saveBtn').onclick = saveDialog;
$('#reviewerBtn').onclick = () => reviewerDialog(false);
window.addEventListener('keydown', e => {
  if (/INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName) || $('#dlg').open) return;
  const k = e.key.toLowerCase();
  if ((e.ctrlKey || e.metaKey) && k === 'z') { e.preventDefault(); undo(); return; }
  if (e.ctrlKey || e.metaKey || e.altKey) return;
  const modes = { v: 'pan', c: 'pin', r: 'area', e: 'edit', a: 'annot' };
  if (modes[k]) setMode(modes[k]);
  else if (k === 'd') { const c = $('#disagree'); c.checked = !c.checked; c.onchange({ target: c }); }
  else if (k === 'escape') { closeMenu(); if (S.draw) { S.draw = null; redraw(); } else if (S.mode !== 'pan') setMode('pan'); else clearSelection(false); }
});

let saved = null; try { saved = localStorage.getItem('pathqc.reviewer'); } catch (e) {}
const urlReviewer = new URLSearchParams(location.search).get('reviewer');
if (urlReviewer || saved) { S.reviewer = urlReviewer || saved; load().catch(fail); } else reviewerDialog(true);
})();
