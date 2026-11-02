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
  panels: [], mode: 'pan', oneShot: false, draw: null, hover: null, brushPx: 18,
  // selection: a set of cell indices (mask + list) and the shapes that made it (image px, logged in mm)
  mask: null, selList: new Int32Array(0), selGeoms: [], selPanel: 0, selInfo: null, selBox: null, selUsed: false,
  ref: 'corr', ticked: new Set(), target: null, recent: [],
  hidden: new Set(), disagree: false, showOps: true, dot: 1, wash: 0.55, layerAlpha: 0.9, untouched: 'faint',
  sel: null, syncing: false, secBoxes: [], secTouched: [], secView: 0,
  // assisted selection: pack attributes, tool settings, walker seeds, review queue, efficiency log
  assist: null, gap: 30, jump: 0, compShift: [0, 0], novaeRes: null, comp: null, dom: {}, walk: null, queue: null,
  selTool: null, selTools: [], selT0: 0, gestT0: 0, suggested: null, busy: false,
};
const layerName = () => `${S.reviewer.replace(/[^A-Za-z0-9_.-]+/g, '_')}_labels`;
const sourceName = k => S.manifest.sources.find(s => s.key === k)?.name ?? k;
const shortName = k => sourceName(k).split(/[_ ]/)[0];
const REFS = () => [['corr', 'yours'], ...SRC.map(k => [k, shortName(k)])];
const refArray = r => r === 'corr' ? S.final : S.cells.src[SRC.indexOf(r)];

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
  if (!S.assist) loadAssist().catch(fail);
  S.queue = null;
  S.panels.find(P => P.mine).titleEl.textContent = layerName();
  deselect();
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
  S.mask = new Uint8Array(n);
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
  S.secTouched = S.secBoxes.map(() => 0);
  for (let i = 0; i < n; i++) if (S.tag[i]) S.secTouched[S.cells.sec[i]]++;
  S.cardSec = null;          // force the start card to re-render
}

// the section nearest the centre of the view (the one the start card, Restart and Clear act on)
function sectionInView() {
  const P = S.panels[0], m = P && mapping(P); if (!m || !S.secBoxes.length) return 0;
  const [cx, cy] = m.toI(P.el.clientWidth / 2, P.el.clientHeight / 2);
  let best = 0, bd = Infinity;
  S.secBoxes.forEach((b, k) => { const dx = Math.max(b.x0 - cx, 0, cx - b.x1), dy = Math.max(b.y0 - cy, 0, cy - b.y1), d = dx * dx + dy * dy - (dx || dy ? 0 : 1); if (d < bd) { bd = d; best = k; } });
  return best;
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
      const hb = el('span', 'hbtns', `<button class="btn small" id="restartBtn" title="Pick the labels this section (or all) starts from again">Restart from…</button>
        <button class="btn small danger" id="layerClearBtn" title="Empty ${esc(layerName())} for this section (undoable)">Clear</button>`);
      ph.append(hb);
    }
    ph.append(el('span', 's', esc(spec.sub)));
    p.append(osdDiv, cv, ph); grid.append(p);
    if (spec.mine) { const card = el('div', 'startcard'); card.id = 'startCard'; card.hidden = true; p.append(card); }
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
  new ResizeObserver(() => redraw()).observe(grid);
  $('#restartBtn').onclick = e => { const r = e.target.getBoundingClientRect(); showMenu(restartItems(), r.left, r.bottom + 2); };
  $('#layerClearBtn').onclick = e => { const r = e.target.getBoundingClientRect(); showMenu(clearItems(), r.left, r.bottom + 2); };
}

// ---------------------------------------------------------------- start card, Restart from…, Clear (whole sections)
function renderStartCard() {
  const card = $('#startCard'); if (!card || !S.final) return;
  const k = S.secView = sectionInView(), b = S.secBoxes[k];
  const show = !S.secTouched[k];
  card.hidden = !show;
  if (!show || S.cardSec === k) return;
  S.cardSec = k;
  card.innerHTML = `<div class="sc-h">Start <b>${esc(layerName())}</b> for <b>${esc(b.name)}</b> — pick the labels to start from:</div>
    <div class="sc-b">${S.manifest.sources.map(s => `<button class="btn base" data-start="${s.key}" style="--bc:${BASECOL[s.key]}"><span><i class="dot"></i>${esc(s.name)}</span></button>`).join('')}</div>
    ${b.dev ? `<div class="warnbox">${esc(b.name)} is a dev section: Vestrum is in-sample here.</div>` : ''}
    <label class="sc-all" title="Sections you have already worked on keep their work (Restart from… overwrites)"><input type="checkbox" id="startAll"> apply to all sections not started yet</label>`;
  card.querySelectorAll('[data-start]').forEach(x => x.onclick = () => { card.hidden = true; sectionOp('base', x.dataset.start, $('#startAll').checked ? 'untouched' : false); });
}

// all: true = every section, 'untouched' = sections with no picks / relabels yet, false = the section in view
function sectionOp(op, source, all) {
  const secs = all === 'untouched' ? S.secBoxes.filter((b, k) => !S.secTouched[k] || k === S.secView).map(b => b.name)
    : all ? S.manifest.sections : [S.secBoxes[S.secView].name];
  return applyOp({ op, source, sections: secs, tool: 'section' }, true);
}
function restartItems() {
  const name = S.secBoxes[S.secView = sectionInView()].name, items = [{ sep: true, title: `Restart ${name} from` }];
  for (const s of S.manifest.sources) items.push({ label: `${s.name} labels`, fn: () => sectionOp('base', s.key, false), cls: 'strong' });
  items.push({ sep: true, title: 'All sections from' });
  for (const s of S.manifest.sources) items.push({ label: `${s.name} labels (all sections)`, fn: () => restartAll(s.key) });
  return items;
}
function restartAll(source) {      // overwrites every section: say how many already have work first
  const worked = S.secBoxes.filter((b, k) => S.secTouched[k]).map(b => b.name), n = S.secBoxes.length;
  if (!worked.length) return sectionOp('base', source, true);
  dialog(`<h3>Restart all ${n} sections from ${esc(sourceName(source))}?</h3>
    <p class="hint"><b>${worked.length} of ${n} sections</b> already have work in ${esc(layerName())}: ${esc(worked.join(', '))}.
      Every pick and relabel there is replaced by ${esc(sourceName(source))} labels. Comments and annotations stay; Undo (Ctrl+Z) brings the work back.</p>
    <div class="acts"><button class="btn" value="cancel" formnovalidate>Cancel</button><button class="btn danger" id="restartAllOk">Overwrite ${worked.length} worked section${worked.length > 1 ? 's' : ''}</button></div>`,
  async () => { await sectionOp('base', source, true); });
}
function clearItems() {
  const name = S.secBoxes[S.secView = sectionInView()].name;
  return [{ sep: true, title: `Clear ${layerName()}` },
    { label: `Clear ${name} (this section)`, fn: () => sectionOp('clear', null, false), cls: 'danger' },
    { label: 'Clear all sections', fn: () => sectionOp('clear', null, true), cls: 'danger' },
    { sep: true, title: 'Comments and annotations stay · Undo reverts' }];
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
let raf = 0, full = true;
// redraw(true): only the drawn shape / brush cursor moved, so the cached cell layer is reused
function redraw(overlay = false) {
  if (!overlay) full = true;
  if (!raf) raf = requestAnimationFrame(() => { raf = 0; const f = full; full = false; S.panels.forEach(P => drawPanel(P, f)); placePopover(); renderStartCard(); });
}

function layerArray(P) { return P.layer === 'corr' ? S.final : S.cells.src[P.layer]; }

function agreeAll(i) {
  const r = S.root, a = S.cells.src[0][i], b = S.cells.src[1][i], c = S.cells.src[2][i];
  return a !== 255 && b !== 255 && c !== 255 && r[a] === r[b] && r[b] === r[c];
}

function drawPanel(P, full = true) {
  const dpr = window.devicePixelRatio || 1, W = P.cv.clientWidth, H = P.cv.clientHeight, cw = Math.round(W * dpr), ch = Math.round(H * dpr);
  if (P.cv.width !== cw || P.cv.height !== ch) { P.cv.width = cw; P.cv.height = ch; full = true; }
  const ctx = P.ctx; ctx.setTransform(dpr, 0, 0, dpr, 0, 0); ctx.clearRect(0, 0, W, H);
  const m = mapping(P); if (!m || !S.cells) return;
  const alpha = P.mine ? S.layerAlpha : 1;
  if (P.layer != null && alpha > 0) {          // the bare slide panel draws no cells
    const C = P.cache || (P.cache = document.createElement('canvas'));
    if (full || C.width !== cw || C.height !== ch) {
      if (S.wash > 0) { ctx.fillStyle = `rgba(250,250,252,${S.wash * alpha})`; ctx.fillRect(0, 0, W, H); }
      drawCells(ctx, m, layerArray(P), W, H, alpha, P.mine ? S.untouched : null);
      if (S.selList.length) {                  // dim the rest, redraw the selected cells in full colour
        ctx.fillStyle = 'rgba(250,250,252,.66)'; ctx.fillRect(0, 0, W, H);
        drawCells(ctx, m, layerArray(P), W, H, Math.max(alpha, 0.9), P.mine ? S.untouched : null, S.selList);
      }
      if (C.width !== cw || C.height !== ch) { C.width = cw; C.height = ch; }   // keep the cell layer for overlay-only frames
      const cx = C.getContext('2d'); cx.clearRect(0, 0, cw, ch); cx.drawImage(P.cv, 0, 0);
    } else ctx.drawImage(C, 0, 0, W, H);
  }
  drawSections(ctx, m);
  drawShapes(P, ctx, m);
  drawSelection(P, ctx, m, W, H);
}

function drawSelection(P, ctx, m, W, H) {
  const { px, py } = S.cells, r = Math.min(9, Math.max(1.6, NUCLEUS_UM / (S.image.um_per_px || 1) * m.s)) * S.dot + 1.2;
  const ring = (list, colour) => {
    if (!list.length) return;
    ctx.beginPath();
    for (const i of list) { const x = m.ox + px[i] * m.s, y = m.oy + py[i] * m.s; if (x < -9 || y < -9 || x > W + 9 || y > H + 9) continue; ctx.moveTo(x + r, y); ctx.arc(x, y, r, 0, 6.2832); }
    ctx.lineWidth = r > 3 ? 1.6 : 1; ctx.strokeStyle = colour; ctx.stroke();
  };
  if (P.layer == null || r >= 4) ring(S.selList, P.layer == null ? '#1f6fd1' : 'rgba(31,111,209,.7)');
  const d = S.draw;
  if (d && d.kind === 'brush') ring(d.cells, d.sel === 'sub' ? '#e5484d' : '#1f6fd1');
  if (S.walk) for (const [list, col] of [[S.walk.inside, '#1e9e57'], [S.walk.outside, '#e5484d']]) for (const i of list) {
    const [x, y] = m.toS(px[i], py[i]); ctx.beginPath(); ctx.arc(x, y, 6, 0, 6.2832); ctx.fillStyle = col; ctx.fill(); ctx.lineWidth = 2; ctx.strokeStyle = '#fff'; ctx.stroke();
  }
  for (const g of S.selGeoms) {
    ctx.strokeStyle = g.mode === 'sub' ? '#e5484d' : '#1f6fd1';
    if (g.type === 'Brush') { ctx.save(); ctx.globalAlpha = 0.13; ctx.lineCap = ctx.lineJoin = 'round'; ctx.lineWidth = 2 * g.r_px * m.s; path(ctx, m, g.px, false); if (g.px.length === 1) ctx.lineTo(...m.toS(g.px[0][0] + 0.01, g.px[0][1])); ctx.stroke(); ctx.restore(); continue; }
    if (g.type !== 'Polygon') continue;
    path(ctx, m, g.px); ctx.lineWidth = 2; if (g.mode === 'sub') ctx.setLineDash([6, 4]); ctx.stroke(); ctx.setLineDash([]);
    if (g.mode !== 'sub') { ctx.fillStyle = 'rgba(31,111,209,.06)'; ctx.fill(); }
  }
  if (S.hover && S.hover.P === P && S.mode === 'brush') { ctx.beginPath(); ctx.arc(S.hover.sx, S.hover.sy, S.brushPx, 0, 6.2832); ctx.lineWidth = 1.5; ctx.strokeStyle = '#1f6fd1'; ctx.setLineDash([3, 3]); ctx.stroke(); ctx.setLineDash([]); }
  if (S.hover && S.mode === 'brush' && S.hover.P !== P) { const [x, y] = m.toS(...S.hover.pt); ctx.beginPath(); ctx.arc(x, y, S.brushPx, 0, 6.2832); ctx.lineWidth = 1; ctx.strokeStyle = 'rgba(31,111,209,.6)'; ctx.stroke(); }
}

function drawCells(ctx, m, arr, W, H, alpha, untouched, only) {
  const { px, py } = S.cells, { gw, gh, start, order } = S.index;
  const [ix0, iy0] = m.toI(0, 0), [ix1, iy1] = m.toI(W, H);
  const bx0 = Math.max(0, ix0 / GRID | 0), bx1 = Math.min(gw - 1, ix1 / GRID | 0);
  const by0 = Math.max(0, iy0 / GRID | 0), by1 = Math.min(gh - 1, iy1 / GRID | 0);
  const r = Math.min(9, Math.max(1.1, NUCLEUS_UM / (S.image.um_per_px || 1) * m.s)) * S.dot;
  const bins = new Map();
  const visit = only ? f => { for (const i of only) if (px[i] >= ix0 && px[i] <= ix1 && py[i] >= iy0 && py[i] <= iy1) f(i); }
    : f => { for (let by = by0; by <= by1; by++) for (let bx = bx0; bx <= bx1; bx++) { const b = by * gw + bx; for (let j = start[b]; j < start[b + 1]; j++) f(order[j]); } };
  visit(i => {
    const c = arr[i];
    let root = c === 255 ? 255 : S.root[c];
    if (untouched && S.tag[i] === 0) {        // reviewer layer: cells not yet picked or relabelled
      if (untouched === 'hide') return;
      if (untouched === 'faint') root = -1;
    }
    if (S.hidden.has(root)) return;
    if (S.disagree && agreeAll(i)) return;
    let list = bins.get(root); if (!list) bins.set(root, list = []);
    list.push(m.ox + px[i] * m.s, m.oy + py[i] * m.s);
  });
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
const opColour = o => o.op === 'base' ? BASECOL[o.source] : o.op === 'clear' ? '#6c7784' : BASECOL.relabel;

function drawShapes(P, ctx, m) {
  if (S.showOps) for (const o of S.ops) {
    if (o.scope) continue;              // whole-section picks and clears have no outline
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
  const d = S.draw;
  if (d && d.kind !== 'brush') {
    const pts = d.kind === 'rect' ? rectPts(d.pts[0], d.b || d.pts[0]) : d.kind === 'poly' && d.cursor ? [...d.pts, d.cursor] : d.pts;
    path(ctx, m, pts, d.kind === 'rect'); ctx.lineWidth = 2; ctx.strokeStyle = d.sel === 'sub' ? '#e5484d' : '#ff2d55'; ctx.stroke();
    if (d.kind === 'poly') { ctx.fillStyle = '#ff2d55'; for (const p of d.pts) { const [x, y] = m.toS(p[0], p[1]); ctx.fillRect(x - 3, y - 3, 6, 6); } }
  }
}
const rectPts = (a, b) => [[a[0], a[1]], [b[0], a[1]], [b[0], b[1]], [a[0], b[1]]];
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
function cellsInPoly(poly) {
  const { gw, gh, start, order } = S.index, { px, py } = S.cells, out = [];
  const xs = poly.map(p => p[0]), ys = poly.map(p => p[1]);
  const x0 = Math.min(...xs), x1 = Math.max(...xs), y0 = Math.min(...ys), y1 = Math.max(...ys);
  for (let by = Math.max(0, y0 / GRID | 0); by <= Math.min(gh - 1, y1 / GRID | 0); by++)
    for (let bx = Math.max(0, x0 / GRID | 0); bx <= Math.min(gw - 1, x1 / GRID | 0); bx++) {
      const b = by * gw + bx;
      for (let j = start[b]; j < start[b + 1]; j++) { const i = order[j], x = px[i], y = py[i]; if (x >= x0 && x <= x1 && y >= y0 && y <= y1 && inPoly([x, y], poly)) out.push(i); }
    }
  return out;
}
function brushAt(d, pt) {        // add the cells under a brush dab to the stroke being drawn
  const { gw, gh, start, order } = S.index, { px, py } = S.cells, r = d.r;
  for (let by = Math.max(0, (pt[1] - r) / GRID | 0); by <= Math.min(gh - 1, (pt[1] + r) / GRID | 0); by++)
    for (let bx = Math.max(0, (pt[0] - r) / GRID | 0); bx <= Math.min(gw - 1, (pt[0] + r) / GRID | 0); bx++) {
      const b = by * gw + bx;
      for (let j = start[b]; j < start[b + 1]; j++) { const i = order[j]; if (!d.set.has(i) && (px[i] - pt[0]) ** 2 + (py[i] - pt[1]) ** 2 <= r * r) { d.set.add(i); d.cells.push(i); } }
    }
}
function hull(list) {            // convex hull of cells (image px), for comments on a selection
  const pts = Array.from(list, i => [S.cells.px[i], S.cells.py[i]]).sort((a, b) => a[0] - b[0] || a[1] - b[1]);
  if (pts.length < 3) { const [x, y] = pts[0]; return [[x - 8, y - 8], [x + 8, y - 8], [x + 8, y + 8], [x - 8, y + 8]]; }
  const cr = (o, a, b) => (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0]), lo = [], up = [];
  for (const p of pts) { while (lo.length > 1 && cr(lo[lo.length - 2], lo[lo.length - 1], p) <= 0) lo.pop(); lo.push(p); }
  for (const p of pts.reverse()) { while (up.length > 1 && cr(up[up.length - 2], up[up.length - 1], p) <= 0) up.pop(); up.push(p); }
  return lo.slice(0, -1).concat(up.slice(0, -1)).map(p => [Math.round(p[0] * 10) / 10, Math.round(p[1] * 10) / 10]);
}
function hits(P, sx, sy) {      // everything under a screen point of one panel
  const m = mapping(P), pt = m.toI(sx, sy), h = { pt };
  h.cell = nearestCell(pt[0], pt[1], 10 / m.s);
  h.inSel = S.selList.length > 0 && ((h.cell != null && S.mask[h.cell] === 1) || S.selGeoms.some(g => g.mode !== 'sub' && g.type === 'Polygon' && inPoly(pt, g.px)));
  h.comment = S.comments.findIndex(c => { const [ax, ay] = m.toS(...anchor(c.geometry.px)); return Math.hypot(ax - sx, ay - 15 - sy) < 12 || (c.geometry.type === 'Polygon' && inPoly(pt, c.geometry.px)); });
  h.annotation = S.annotations.findIndex(a => inPoly(pt, a.geometry.px));
  for (let k = S.ops.length - 1; k >= 0; k--) if (!S.ops[k].scope && inPoly(pt, S.ops[k].geometry.px)) { h.op = S.ops[k]; break; }
  return h;
}

// ---------------------------------------------------------------- tools, drawing, clicks
const SEL_TOOLS = ['lasso', 'poly', 'rect', 'brush', 'cell'];
const ASSIST_TOOLS = { wand: 'wand', comp: 'compartment', novae: 'novae' };
function setMode(mode, oneShot = false) {
  if (mode === 'walker' && !S.selList.length) { toast('Refine border: select a region first (any tool), then press G and click inside / Alt+click outside.', 5000); return; }
  if (mode === 'walker' && S.mode !== 'walker') S.walk = { base: S.selList.slice(), tools: [...S.selTools], t0: S.selT0, inside: [], outside: [], P: S.panels[S.selPanel] };
  if (mode !== 'walker') S.walk = null;
  S.mode = mode; S.oneShot = oneShot; S.draw = null; if (mode !== 'brush') S.hover = null;
  document.querySelectorAll('#modes button, #modes2 button, #modes3 button').forEach(b => b.classList.toggle('on', b.dataset.mode === mode));
  renderAssistBar(); if (S.selInfo && !$('#pop').hidden) renderPopover();
  S.panels.forEach(P => P.el.classList.toggle('drawing', mode !== 'pan'));
  redraw();
}
function setBrush(px) { S.brushPx = Math.max(4, Math.min(150, Math.round(px))); $('#brushSize').textContent = S.brushPx + ' px'; redraw(); }

function attachPointer(P) {
  const pos = e => { const r = P.el.getBoundingClientRect(); return [e.clientX - r.left, e.clientY - r.top]; };
  const skip = e => e.target.closest('.ph') || e.target.closest('.startcard');
  // capture phase on the panel: runs before OpenSeadragon, so a draw gesture never pans the slide
  P.el.addEventListener('pointerdown', e => {
    if (e.button !== 0 || skip(e)) return;
    if (/INPUT|SELECT|TEXTAREA|BUTTON/.test(document.activeElement?.tagName)) document.activeElement.blur();   // keys go to the tools again
    const tool = S.mode !== 'pan' ? S.mode : (e.shiftKey || e.altKey ? 'lasso' : null);
    if (!tool) return;
    e.stopPropagation(); e.preventDefault();
    const [sx, sy] = pos(e), m = mapping(P), pt = m.toI(sx, sy);
    const sel = e.altKey ? 'sub' : e.shiftKey ? 'add' : 'replace';
    if (!S.draw) S.gestT0 = performance.now();
    if (tool === 'pin') { finishDrawing('pin', P, { type: 'Point', px: [pt] }); return; }
    if (tool === 'cell') {
      const c = nearestCell(pt[0], pt[1], 10 / m.s);
      if (c == null) { if (sel === 'replace') deselect(); return; }
      applySel(sel, [c], { type: 'Point', px: [[S.cells.px[c], S.cells.py[c]]] }, P, 'cell'); return;
    }
    if (ASSIST_TOOLS[tool]) { assistClick(P, ASSIST_TOOLS[tool], nearestCell(pt[0], pt[1], 10 / m.s), (e.ctrlKey || e.metaKey) && S.selList.length ? 'and' : sel); return; }
    if (tool === 'walker') { walkerClick(P, nearestCell(pt[0], pt[1], 10 / m.s), e.altKey !== !!S.walk?.out); return; }
    if (tool === 'poly') {
      const d = S.draw, t = performance.now();
      if (d && d.kind === 'poly' && d.P === P) {
        if (t - d.down.t < 350 && Math.hypot(sx - d.down.x, sy - d.down.y) < 8) { finishPoly(); return; }
        d.pts.push(pt); d.down = { t, x: sx, y: sy }; redraw(); return;
      }
      S.draw = { P, kind: 'poly', sel, pts: [pt], down: { t, x: sx, y: sy } }; redraw(); return;
    }
    P.el.setPointerCapture(e.pointerId);
    S.draw = { P, kind: tool, sel, pts: [pt], last: [sx, sy] };
    if (tool === 'brush') { Object.assign(S.draw, { r: S.brushPx / m.s, cells: [], set: new Set() }); brushAt(S.draw, pt); }
    redraw();
  }, true);
  P.el.addEventListener('pointermove', e => {
    const [sx, sy] = pos(e);
    if (S.mode === 'brush') { S.hover = { P, sx, sy, pt: mapping(P).toI(sx, sy) }; redraw(true); }
    const d = S.draw; if (!d || d.P !== P) return;
    e.stopPropagation();
    const pt = mapping(P).toI(sx, sy);
    if (d.kind === 'poly') { d.cursor = pt; redraw(true); return; }
    if (d.kind === 'rect') { d.b = pt; redraw(true); return; }
    if (Math.hypot(sx - d.last[0], sy - d.last[1]) < 4) return;
    if (d.kind === 'brush') {              // dab along the segment so a fast stroke leaves no gaps
      const a = d.pts[d.pts.length - 1], steps = Math.ceil(Math.hypot(pt[0] - a[0], pt[1] - a[1]) / (d.r / 2));
      for (let s = 1; s <= steps; s++) brushAt(d, [a[0] + (pt[0] - a[0]) * s / steps, a[1] + (pt[1] - a[1]) * s / steps]);
    }
    d.last = [sx, sy]; d.pts.push(pt); redraw(true);
  }, true);
  P.el.addEventListener('pointerup', e => {
    const d = S.draw; if (!d || d.P !== P || d.kind === 'poly') return;
    e.stopPropagation(); S.draw = null;
    if (d.kind === 'brush') { applySel(d.sel, d.cells, { type: 'Brush', px: simplify(d.pts), r_px: Math.round(d.r * 10) / 10 }, P, 'brush'); if (S.oneShot) setMode('pan'); return; }
    if (d.kind === 'rect') {
      const m = mapping(P);
      if (!d.b || Math.abs(d.b[0] - d.pts[0][0]) * m.s < 4 || Math.abs(d.b[1] - d.pts[0][1]) * m.s < 4) { redraw(); toast('Drag to draw a rectangle.'); return; }
      finishDrawing('rect', P, { type: 'Polygon', px: simplify(rectPts(d.pts[0], d.b)) }, d.sel); return;
    }
    if (d.pts.length < 3) { redraw(); toast('Trace a closed shape: hold the button and draw around the area.'); return; }
    finishDrawing(d.kind, P, { type: 'Polygon', px: simplify(d.pts) }, d.sel);
  }, true);
  P.el.addEventListener('pointerleave', () => { if (S.hover?.P === P) { S.hover = null; redraw(true); } });
  P.el.addEventListener('dblclick', e => { if (S.mode !== 'pan') { e.stopPropagation(); e.preventDefault(); } }, true);
  P.el.addEventListener('contextmenu', e => {
    if (e.target.closest('.ph')) return;
    if (e.ctrlKey && ASSIST_TOOLS[S.mode]) { e.preventDefault(); return; }   // macOS: Ctrl+click is the "inside the selection" modifier here
    e.preventDefault(); e.stopPropagation();
    const [sx, sy] = pos(e); contextMenu(P, hits(P, sx, sy), e.clientX, e.clientY);
  }, true);
}

function finishPoly() {
  const d = S.draw; S.draw = null;
  if (!d || d.pts.length < 3) { redraw(); toast('A polygon needs at least three corners.'); return; }
  finishDrawing('poly', d.P, { type: 'Polygon', px: simplify(d.pts) }, d.sel);
}

function simplify(pts) {
  const xs = pts.map(p => p[0]), ys = pts.map(p => p[1]);
  const tol = Math.max(Math.max(...xs) - Math.min(...xs), Math.max(...ys) - Math.min(...ys)) / 400;
  const out = [pts[0]];
  for (const p of pts) { const l = out[out.length - 1]; if (Math.hypot(p[0] - l[0], p[1] - l[1]) > tol) out.push(p); }
  return out.map(p => [Math.round(p[0] * 10) / 10, Math.round(p[1] * 10) / 10]);
}

function finishDrawing(kind, P, geom, sel = 'replace') {
  if (kind === 'pin' || kind === 'area') commentDialog({ geometry: geom });
  else if (kind === 'annot') annotationDialog({ geometry: geom });
  else selectPolygon(geom.px, P, sel, kind);
  if (S.oneShot) setMode('pan');
  redraw();
}

function panClick(P, pos) {
  const h = hits(P, pos.x, pos.y);
  if (S.selList.length && !h.inSel) deselect();        // click outside the selection drops it
  if (h.comment >= 0 && !h.inSel) { commentDialog(S.comments[h.comment]); return; }
  S.sel = h.cell; redraw();
  if (S.sel == null) { $('#cellBox').hidden = true; return; }
  api('/api/cell/' + S.sel).then(showCell).catch(fail);
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

// ---------------------------------------------------------------- selection: a set of cells, Shift adds, Alt subtracts
function selectPolygon(px, P, sel = 'replace', tool = 'lasso') {
  const cells = cellsInPoly(px);
  if (!cells.length && sel !== 'sub') toast('No cells inside that shape.');
  applySel(sel, cells, { type: 'Polygon', px }, P, tool);
}
// sel: 'replace' | 'add' (Shift) | 'sub' (Alt) | 'and' (keep only these cells); tool feeds the efficiency log
function applySel(sel, cells, geom, P, tool = 'other') {
  if (sel === 'replace') { S.mask.fill(0); S.selGeoms = []; S.selUsed = false; S.selTool = tool; S.selTools = [tool]; S.selT0 = S.gestT0 || performance.now(); S.suggested = null; }
  else if (!S.selTools.includes(tool)) S.selTools.push(tool);
  if (sel === 'and') { const keep = new Uint8Array(S.mask.length); for (const i of cells) keep[i] = 1; for (const i of S.selList) if (!keep[i]) S.mask[i] = 0; }
  else { const v = sel === 'sub' ? 0 : 1; for (const i of cells) S.mask[i] = v; }
  if (geom) S.selGeoms.push({ ...geom, mode: sel === 'sub' ? 'sub' : 'add' });
  let n = 0; for (let i = 0; i < S.mask.length; i++) n += S.mask[i];
  if (!n) { deselect(); return; }
  const list = new Int32Array(n); let j = 0, x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
  const { px, py } = S.cells;
  for (let i = 0; i < S.mask.length; i++) if (S.mask[i]) { list[j++] = i; if (px[i] < x0) x0 = px[i]; if (px[i] > x1) x1 = px[i]; if (py[i] < y0) y0 = py[i]; if (py[i] > y1) y1 = py[i]; }
  S.selList = list; S.selBox = [x0, y0, x1, y1]; if (P) S.selPanel = P.k;
  selInfo(); resetTicks(); showTab('edit'); renderPopover(); renderEdit(); redraw();
}
function deselect() {
  if (S.walk) setMode('pan');
  if (S.mask) S.mask.fill(0);
  S.selList = new Int32Array(0); S.selGeoms = []; S.selInfo = null; S.selUsed = false; S.sel = null; S.draw = null;
  $('#cellBox').hidden = true; $('#pop').hidden = true; closeMenu(); renderEdit(); redraw();
}
const rootOf = c => c === 255 ? 255 : S.root[c];
function selInfo() {
  const comp = {}, secs = new Map();
  for (const [r] of REFS()) {
    const a = refArray(r), c = new Map();
    for (const i of S.selList) { const v = rootOf(a[i]); c.set(v, (c.get(v) || 0) + 1); }
    comp[r] = [...c].sort((x, y) => y[1] - x[1]);
  }
  for (const i of S.selList) { const s = S.secBoxes[S.cells.sec[i]].name; secs.set(s, (secs.get(s) || 0) + 1); }
  let same = {}; for (const k of SRC) { const a = refArray(k); let n = 0; for (const i of S.selList) if (a[i] !== 255 && S.root[a[i]] === S.root[S.final[i]]) n++; same[k] = Math.round(100 * n / S.selList.length); }
  S.selInfo = { n: S.selList.length, comp, sections: [...secs.keys()], same };
}
function resetTicks() { const top = S.selInfo.comp[S.ref][0]; S.ticked = new Set(top ? [top[0]] : []); }
function tickedCells() {
  const a = refArray(S.ref), out = [];
  for (const i of S.selList) if (S.ticked.has(rootOf(a[i]))) out.push(i);
  return out;
}
const selGeomsOut = () => S.selGeoms.map(g => ({ mode: g.mode, type: g.type, px: g.px, r_px: g.r_px, tool: g.tool }));
const effLog = () => ({ tool: S.selTool || 'other', tools: S.selTools.length ? S.selTools : [S.selTool || 'other'], sel_ms: Math.round(performance.now() - (S.selT0 || performance.now())) });
const selOutline = () => S.selGeoms.length === 1 && S.selGeoms[0].type === 'Polygon' ? S.selGeoms[0].px : hull(S.selList);
function quickList() {      // labels on the number keys: recent targets first, then the vocabulary order
  const roots = S.vocab.filter(v => v.merged_into == null).map(v => v.code);
  return [...new Set([...S.recent.filter(c => roots.includes(c)), ...roots])].slice(0, 9);
}

function renderPopover() {
  const pop = $('#pop'), info = S.selInfo;
  if (!info || !S.selList.length) { pop.hidden = true; return; }
  const devSecs = info.sections.filter(s => S.manifest.splits.dev.includes(s));
  const dom = k => { const t = info.comp[k][0]; return `${shortName(k)} ${Math.round(100 * t[1] / info.n)}% ${esc(nameOf(t[0]))}`; };
  const classes = info.comp[S.ref], nTick = tickedCells().length, quick = quickList();
  pop.innerHTML = `<div class="ph2"><b>${nCells(info.n)}</b> <span>${info.sections.join(', ')}</span>
      <button class="x" data-a="discard" title="Deselect (Esc)">×</button></div>
    <div class="compline" id="selComp">${fmt(info.n)} cells: ${SRC.map(dom).join(' · ')}</div>
    ${guessHtml()}
    ${devSecs.length ? `<div class="warnbox">Dev section ${devSecs.join(', ')}: Vestrum is in-sample here.</div>` : ''}
    <div class="lbl">Use labels from <span class="hint">→ ${esc(layerName())}</span></div>
    <div class="basebtns">${S.manifest.sources.map(s => `<button class="btn base" data-base="${s.key}" style="--bc:${BASECOL[s.key]}">
      <span><i class="dot"></i>${esc(s.name)}</span><span class="mono">${info.same[s.key]}% same as yours</span></button>`).join('')}</div>
    <div class="lbl">Relabel only the ticked classes, as labelled by <select data-a="ref" class="inl">${REFS().map(([k, n]) => `<option value="${k}" ${S.ref === k ? 'selected' : ''}>${esc(n)}</option>`).join('')}</select></div>
    <div class="classes">${classes.map(([c, n]) => `<label><input type="checkbox" data-c="${c}" ${S.ticked.has(c) ? 'checked' : ''}><span class="sw" style="background:${c === 255 ? '#b9bec5' : S.colour[c]}"></span>${esc(nameOf(c))} <span class="mono">${fmt(n)}</span></label>`).join('')}</div>
    <div class="row"><select data-a="to"><option value="">new label…</option>${labelOptions(S.target)}</select><button class="btn primary" data-a="relabel" ${nTick ? '' : 'disabled'}>Relabel ${fmt(nTick)}</button></div>
    <div class="quick">${quick.map((c, k) => `<button class="qk ${S.target === c ? 'on' : ''}" data-q="${c}" title="Press ${k + 1}"><kbd>${k + 1}</kbd>${esc(nameOf(c))}</button>`).join('')}</div>
    <div class="row"><button class="btn small" data-a="comment">Comment here</button><span class="hint" style="margin:0">Enter relabels · Esc deselects · ${S.walk ? '<b>Refine: click = inside seed, Alt+click = outside seed</b>' : 'Shift adds · Alt subtracts'}</span></div>`;
  pop.hidden = false;
  pop.querySelectorAll('[data-base]').forEach(b => b.onclick = () => baseSel(b.dataset.base));
  pop.querySelector('[data-a=ref]').onchange = e => { S.ref = e.target.value; resetTicks(); renderPopover(); };
  pop.querySelectorAll('[data-c]').forEach(b => b.onchange = e => { const c = +b.dataset.c; e.target.checked ? S.ticked.add(c) : S.ticked.delete(c); e.target.blur(); const n = tickedCells().length, r = pop.querySelector('[data-a=relabel]'); r.textContent = 'Relabel ' + fmt(n); r.disabled = !n; });
  pop.querySelector('[data-a=to]').onchange = e => { S.target = e.target.value === '' ? null : +e.target.value; e.target.blur(); renderPopover(); };
  pop.querySelectorAll('[data-q]').forEach(b => b.onclick = () => { S.target = +b.dataset.q; renderPopover(); });
  pop.querySelector('[data-a=relabel]').onclick = relabelSel;
  pop.querySelector('[data-a=comment]').onclick = () => commentDialog({ geometry: { type: 'Polygon', px: selOutline() } });
  pop.querySelector('[data-a=discard]').onclick = deselect;
  pop.querySelector('[data-a=suggest]')?.addEventListener('click', suggest);
  pop.querySelector('[data-a=snap]')?.addEventListener('click', snapCompartment);
  placePopover();
}

function placePopover() {
  const pop = $('#pop'); if (pop.hidden || !S.selBox) return;
  const P = S.panels[S.selPanel], m = mapping(P); if (!m) return;
  const pr = P.el.getBoundingClientRect(), gr = $('#grid').getBoundingClientRect();
  const [ax, ay] = m.toS(S.selBox[0], S.selBox[1]), [bx] = m.toS(S.selBox[2], S.selBox[3]);
  const w = pop.offsetWidth, h = pop.offsetHeight;
  let x = pr.left - gr.left + bx + 12, y = pr.top - gr.top + ay;
  if (x + w > gr.width - 4) x = pr.left - gr.left + ax - w - 12;
  x = Math.max(4, Math.min(gr.width - w - 4, x)); y = Math.max(4, Math.min(gr.height - h - 4, y));
  pop.style.left = x + 'px'; pop.style.top = y + 'px';
}

function labelOptions(selected) {
  return S.vocab.filter(v => v.merged_into == null).map(v => `<option value="${v.code}" ${v.code === selected ? 'selected' : ''}>${esc(v.name)}</option>`).join('')
    + `<option value="-1" ${selected === -1 ? 'selected' : ''}>+ new label…</option>`;
}
async function ensureLabel(code) {      // "+ new label…" creates it first
  if (code !== -1) return code;
  const n = prompt('Name of the new label'); if (!n || !n.trim()) return null;
  const r = await api('/api/vocab', { body: { action: { action: 'add', name: n.trim() } } });
  setVocab(r.vocab); renderAll(); return codeOfName(n.trim());
}

function baseSel(source) { return applyOp({ op: 'base', source, cells: Array.from(S.selList), geoms: selGeomsOut(), ...effLog() }); }
async function relabelSel() {
  if (!S.selList.length) return;
  if (S.target == null) { toast('Choose the new label first (number keys 1–9 or the list).'); return; }
  const cells = tickedCells();
  if (!cells.length) { toast('Tick at least one class to relabel.'); return; }
  try {
    const to = await ensureLabel(S.target); if (to == null) return;
    S.target = to; S.recent = [to, ...S.recent.filter(c => c !== to)].slice(0, 4);
    const eff = effLog(); if (S.suggested === to) eff.tools = [...eff.tools, 'suggest'];
    await applyOp({ op: 'relabel', to, cells, geoms: selGeomsOut(), filter_ref: S.ref, filter_classes: [...S.ticked], ...eff });
  } catch (e) { fail(e); }
}

function renderEdit() {
  const pane = $('#editPane');
  if (!S.selInfo) {
    pane.innerHTML = `<h3>Build ${esc(layerName())}</h3>
      <ol class="steps">
        <li><b>Start a section:</b> on the ${esc(layerName())} panel, pick the method to start from (the start card), or <i>Restart from…</i> later.</li>
        <li><b>Select cells:</b> Lasso <kbd>L</kbd>, Polygon <kbd>P</kbd> (click corners, double-click or Enter closes), Rectangle <kbd>R</kbd>,
          Brush <kbd>B</kbd> (<kbd>[</kbd> <kbd>]</kbd> size), Cell <kbd>S</kbd>. <kbd>Shift</kbd> adds, <kbd>Alt</kbd> subtracts. In Pan <kbd>V</kbd>, <kbd>Shift</kbd>+drag lassos.</li>
        <li><b>Fix:</b> in the box by the selection, use another method's labels for those cells, or relabel only the ticked classes.
          <kbd>1</kbd>–<kbd>9</kbd> choose the new label, <kbd>Enter</kbd> applies, <kbd>Esc</kbd> deselects.</li>
        <li><b>Assist (optional):</b> Wand <kbd>W</kbd> (same label, connected), Compartment <kbd>K</kbd>, Novae <kbd>N</kbd>, Refine border <kbd>G</kbd>;
          the <b>Review</b> tab lists disagreement hotspots (<kbd>J</kbd> next, <kbd>Shift</kbd>+<kbd>J</kbd> previous). They only select — you still confirm.</li>
      </ol>
      <p class="hint">Every choice lands in <b>${esc(layerName())}</b>, whichever panel you drew on. Outlines there show the method you picked:
        ${S.manifest.sources.map(s => `<span class="keychip" style="--bc:${BASECOL[s.key]}">${esc(s.name)}</span>`).join(' ')} <span class="keychip" style="--bc:${BASECOL.relabel}">relabel</span></p>
      <p class="hint">Cells you never touch keep the Bill_eugene_code label (saved as <span class="mono">bill_eugene_code-default</span>).
        <b>Clear</b> on the ${esc(layerName())} panel empties a section again (undoable).</p>`;
    return;
  }
  const info = S.selInfo, comp = info.comp, cnt = (k, c) => (comp[k].find(x => x[0] === c) || [0, 0])[1];
  const codes = new Set(); Object.values(comp).forEach(list => list.slice(0, 10).forEach(([c]) => codes.add(c)));
  const rows = [...codes].sort((a, b) => cnt('corr', b) - cnt('corr', a)).map(c => `<tr><td><span class="sw" style="background:${c === 255 ? '#b9bec5' : S.colour[c]}"></span> ${esc(nameOf(c))}</td>
    <td><b>${fmt(cnt('corr', c))}</b></td>${SRC.map(k => `<td>${fmt(cnt(k, c))}</td>`).join('')}</tr>`).join('');
  pane.innerHTML = `<h3>Selection · ${nCells(info.n)}</h3>
    <p class="hint">Choose and relabel in the box next to the selection. Counts per label:</p>
    <table class="comp"><tr><th>Label</th><th>Yours</th>${SRC.map(k => `<th>${esc(shortName(k))}</th>`).join('')}</tr>${rows}</table>`;
}

async function applyOp(body, scoped = false) {
  if (applyOp.busy) return;                  // a double click sends one op
  applyOp.busy = true;
  try {
    const op = await api('/api/ops', { body });
    S.ops.push(op); await loadFinal(); if (S.queue) loadQueue(S.queue.kind);
    const where = op.scope === 'all' ? 'all sections' : (op.sections || []).join(', ');
    toast(op.op === 'clear' ? `${layerName()} cleared for ${where} — Undo (Ctrl+Z) brings it back`
      : op.op === 'base' ? `${layerName()}: ${sourceName(op.source)} labels for ${scoped ? where : nCells(op.n)}`
      : `${layerName()}: ${nCells(op.n)} relabelled to ${op.to_name}`);
    if (S.selList.length && !scoped) { S.selUsed = true; selInfo(); resetTicks(); }
    else if (S.selList.length) selInfo();
    if (S.sel != null) api('/api/cell/' + S.sel).then(showCell).catch(fail);
    renderAll();
    return op;
  } catch (e) { fail(e); } finally { applyOp.busy = false; }
}

async function undo(target) {
  try {
    const r = await api('/api/undo', { body: target ? { target } : {} });
    if (!r.undone) { toast('Nothing to undo'); return; }
    S.ops = S.ops.filter(o => o.id !== r.undone.id); await loadFinal(); if (S.queue) loadQueue(S.queue.kind);
    toast('Undid: ' + opText(r.undone)); if (S.selList.length) selInfo(); renderAll();
  } catch (e) { fail(e); }
}
const opText = o => {
  const where = o.scope === 'all' ? 'all sections' : (o.sections || []).join(', ');
  if (o.op === 'clear') return `clear · ${where}`;
  if (o.op === 'base') return `${sourceName(o.source)} labels · ${o.scope ? where : nCells(o.n)}`;
  return `relabel → ${o.to_name} · ${nCells(o.n)}`;
};

// ---------------------------------------------------------------- assisted selection (opt-in; every result is only a selection)
async function loadAssist() {
  const info = await api('/api/assist/info');
  S.assist = info; S.gap = info.default_gap;
  const res = Object.keys(info.domains);
  S.novaeRes = res.includes('novae10') ? 'novae10' : res[0] || null;
  await Promise.all([info.stains ? loadComp() : null, S.novaeRes ? loadDomains(S.novaeRes) : null]);
  buildAssistBar(); if (S.selInfo) renderPopover();
}
async function loadComp() { S.comp = new Uint8Array(await api(`/api/assist/compartments.bin?panck=${S.compShift[0]}&cd45=${S.compShift[1]}`)); }
async function loadDomains(res) { if (!S.dom[res]) S.dom[res] = new Uint16Array(await api('/api/assist/domains.bin?res=' + res)); }

function buildAssistBar() {
  const A = S.assist, bar = $('#abOpts'), seg = $('#modes3');
  seg.hidden = false; $('#assistBar').hidden = false;
  seg.querySelector('[data-mode=comp]').hidden = !A.stains;
  seg.querySelector('[data-mode=novae]').hidden = !S.novaeRes;
  bar.innerHTML = `<span class="ab-t" id="abTitle"></span>
    <label class="ctl" data-for="wand comp novae walker" title="Longest cell-to-cell step (µm) the region may cross">max gap <input type="range" id="abGap" min="${A.gap_range[0]}" max="${A.gap_range[1]}" step="5" value="${S.gap}"> <b class="mono" id="abGapV">${S.gap} µm</b></label>
    <label class="ctl" data-for="wand comp novae" title="Cross up to this many cells of another class">jump <select id="abJump"><option value="0">0 cells</option><option value="1">1 cell</option><option value="2">2 cells</option></select></label>
    ${A.stains ? `<label class="ctl" data-for="comp" title="Epithelial: PanCK above the threshold (Otsu per section on log intensity)">PanCK <input type="range" id="abP" min="-1.5" max="1.5" step="0.05" value="0"> <b class="mono" id="abPV"></b></label>
    <label class="ctl" data-for="comp" title="Immune: CD45 above the threshold (and PanCK below)">CD45 <input type="range" id="abC" min="-1.5" max="1.5" step="0.05" value="0"> <b class="mono" id="abCV"></b></label>` : ''}
    ${S.novaeRes ? `<label class="ctl" data-for="novae">resolution <select id="abRes">${Object.keys(A.domains).map(k => `<option value="${k}" ${k === S.novaeRes ? 'selected' : ''}>${esc(A.domains[k].title)}</option>`).join('')}</select></label>` : ''}
    <span class="hint" data-for="wand comp novae">click a cell · Shift adds · Alt subtracts · Ctrl/⌘ stays inside the selection</span>
    <span class="hint" data-for="walker" id="abWalk"></span>`;
  $('#abGap').oninput = e => { S.gap = +e.target.value; $('#abGapV').textContent = S.gap + ' µm'; };
  $('#abJump').onchange = e => { S.jump = +e.target.value; e.target.blur(); };
  if (A.stains) {
    const upd = () => { S.compShift = [+$('#abP').value, +$('#abC').value]; renderAssistBar(); clearTimeout(upd.h); upd.h = setTimeout(() => loadComp().then(() => { if (S.selInfo) renderPopover(); }).catch(fail), 150); };
    $('#abP').oninput = upd; $('#abC').oninput = upd;
  }
  if (S.novaeRes) $('#abRes').onchange = e => { S.novaeRes = e.target.value; e.target.blur(); loadDomains(S.novaeRes).then(() => { if (S.selInfo) renderPopover(); }).catch(fail); };
  renderAssistBar();
}
function renderAssistBar() {
  const bar = $('#abOpts'); if (!bar || !S.assist) return;
  bar.querySelectorAll('[data-for]').forEach(x => x.hidden = !x.dataset.for.split(' ').includes(S.mode));
  $('#abTitle').textContent = { wand: 'same label, connected:', comp: 'epithelium or immune region:', novae: 'same Novae domain, connected:', walker: 'random walker on the selection:' }[S.mode]
    || 'optional helpers — each one only makes a selection; you confirm in the box';
  if (S.assist.stains) {
    const t = S.assist.thresholds[S.secBoxes[sectionInView()].name], k = v => v >= 1000 ? (v / 1000).toFixed(1) + 'k' : Math.round(v);
    $('#abPV').textContent = '≥ ' + k(Math.expm1(t.panck_log + S.compShift[0]));
    $('#abCV').textContent = '≥ ' + k(Math.expm1(t.cd45_log + S.compShift[1]));
  }
  if (S.walk) {      // Alt here places an outside seed (it does not subtract); the toggle does the same without Alt
    const o = S.walk.out;
    $('#abWalk').innerHTML = `next click places <span class="seg mini" id="abSide"><button data-side="in" class="${o ? '' : 'on'}" style="color:#1e9e57">● inside</button><button data-side="out" class="${o ? 'on' : ''}" style="color:#e5484d">● outside</button></span>
      seed (Alt+click: ${o ? 'inside' : 'outside'}) · seeds <b style="color:#1e9e57">${S.walk.inside.length} in</b> / <b style="color:#e5484d">${S.walk.outside.length} out</b>
      <button class="btn small" id="abWalkClear">Clear seeds</button>`;
    $('#abSide').querySelectorAll('button').forEach(b => b.onclick = () => { S.walk.out = b.dataset.side === 'out'; renderAssistBar(); });
  }
  $('#abWalkClear')?.addEventListener('click', () => { const w = S.walk; w.inside = []; w.outside = []; S.gestT0 = w.t0; applySel('replace', Array.from(w.base), null, w.P, w.tools[0] || 'other'); S.selTools = [...w.tools]; S.walk = w; renderAssistBar(); });
}

const panelLayer = P => P.layer == null ? 'corr' : P.layer;
async function assistClick(P, kind, cell, sel) {
  if (cell == null) { if (sel === 'replace') deselect(); return; }
  const body = { kind, seed: cell, layer: panelLayer(P), gap: S.gap, jump: S.jump, res: S.novaeRes, panck: S.compShift[0], cd45: S.compShift[1] };
  if (sel === 'and') body.within = Array.from(S.selList);
  S.busy = true;
  try {
    const r = await api('/api/assist/flood', { body });
    const tool = { wand: 'wand', compartment: 'compartment', novae: 'novae' }[kind];
    applySel(sel, r.cells, { type: 'Assist', tool, px: [[S.cells.px[cell], S.cells.py[cell]]] }, P, tool);
    const where = kind === 'wand' ? ` (${P.layer == null || P.layer === 'corr' ? 'yours' : shortName(SRC[P.layer])})` : '';
    toast(`${tool}: ${nCells(r.cells.length)} · ${r.label}${where} · ${r.ms} ms`);
  } catch (e) { fail(e); } finally { S.busy = false; }
}

async function walkerClick(P, cell, outside) {
  const w = S.walk; if (!w || cell == null) return;
  (outside ? w.outside : w.inside).push(cell);
  renderAssistBar(); redraw();
  if (!w.inside.length || !w.outside.length) { toast(outside ? 'Outside seed placed. Now place an inside seed.' : 'Inside seed placed. Now Alt+click (or switch the toggle to “outside” and click) a cell outside the region.', 4000); return; }
  S.busy = true;
  try {
    const r = await api('/api/assist/walker', { body: { selection: Array.from(w.base), inside: w.inside, outside: w.outside, layer: panelLayer(w.P), gap: S.gap, panck: S.compShift[0], cd45: S.compShift[1] } });
    const seeds = [...w.inside, ...w.outside].map(i => [S.cells.px[i], S.cells.py[i]]);
    applySel('replace', r.cells, { type: 'Assist', tool: 'walker', px: seeds }, w.P, 'walker');
    S.walk = w; S.selT0 = w.t0; S.selTools = [...w.tools.filter(t => t !== 'walker'), 'walker'];
    renderAssistBar(); redraw();
    toast(`Refined: ${nCells(r.cells.length)} (from ${fmt(w.base.length)}) · ${r.ms} ms`);
  } catch (e) { fail(e); } finally { S.busy = false; }
}

function majority(arr, list) {
  const c = new Map(); for (const i of list) c.set(arr[i], (c.get(arr[i]) || 0) + 1);
  let best = null, bn = -1; for (const [k, n] of c) if (n > bn) { best = k; bn = n; }
  return [best, Math.round(100 * bn / Math.max(1, list.length))];
}
function guess() {      // "What is this?": compartment, Novae domain, and how the methods label it
  const info = S.selInfo; if (!info) return null;
  const top = k => info.comp[k][0] || [255, 0], pc = n => Math.round(100 * n / info.n);
  const parts = [];
  if (S.comp) { const [c, p] = majority(S.comp, S.selList); parts.push(`${{ epithelial: 'Epithelial', immune: 'Immune-rich', stromal: 'Stromal' }[S.assist.compartments[c]]} ${p}%`); }
  const dom = S.novaeRes && S.dom[S.novaeRes];
  if (dom) { const [d, p] = majority(dom, S.selList); parts.push(`Novae ${esc(S.assist.domains[S.novaeRes].names[d])} ${p}%`); }
  const [v, c, b, y] = [top('vestrum'), top('claude_code'), top('bill_eugene_code'), top('corr')];
  const nm = t => `${esc(nameOf(t[0]))} ${pc(t[1])}%`;
  if (v[0] === c[0] && c[0] === b[0]) parts.push(`all agree: ${esc(nameOf(v[0]))} (V ${pc(v[1])}% · C ${pc(c[1])}% · Bill ${pc(b[1])}%)`);
  else if (v[0] === c[0] && v[0] !== 255) parts.push(`agents agree: ${esc(nameOf(v[0]))} (V ${pc(v[1])}% · C ${pc(c[1])}%) vs Bill ${nm(b)}`);
  else if (v[0] === b[0] || c[0] === b[0]) { const [lone, ln, other] = v[0] === b[0] ? [c, 'Claude', 'Vestrum'] : [v, 'Vestrum', 'Claude']; parts.push(`only ${ln} differs: ${nm(lone)} vs Bill + ${other} ${nm(b)}`); }
  else parts.push(`agents split: Vestrum ${nm(v)} / Claude ${nm(c)} / Bill ${nm(b)}`);
  if (y[0] !== (v[0] === c[0] ? v[0] : b[0])) parts.push(`yours ${nm(y)}`);
  // suggestion: the agents' shared majority when it differs from yours, else the commonest label over the three methods
  let target = null, why = '';
  if (v[0] === c[0] && v[0] !== 255 && v[0] !== y[0]) { target = v[0]; why = `both agents call it ${nameOf(v[0])}`; }
  else {
    const pool = new Map(); for (const k of SRC) for (const [code, n] of info.comp[k]) if (code !== 255) pool.set(code, (pool.get(code) || 0) + n);
    const best = [...pool].sort((a, b2) => b2[1] - a[1])[0];
    if (best && best[0] !== y[0]) { target = best[0]; why = `${nameOf(best[0])} is the majority over the three methods`; }
  }
  return { text: parts.join(' · '), target, why, yours: y[0] };
}
function guessHtml() {
  const g = guess(); if (!g) return '';
  return `<div class="guess" id="selGuess"><b>What is this?</b> ${g.text}</div>
    <div class="row guessbtns">${g.target != null ? `<button class="btn small" data-a="suggest" title="${esc(g.why)} — pre-sets the new label and the ticks; you still press Relabel">Suggest: ${esc(nameOf(g.target))}</button>` : '<span class="hint" style="margin:0">no change suggested</span>'}
    ${S.comp ? '<button class="btn small" data-a="snap" title="Keep only the cells in the compartment most of the selection is in">Snap to compartment</button>' : ''}</div>`;
}
function suggest() {
  const g = guess(); if (!g || g.target == null) return;
  S.ref = 'corr'; S.target = g.target; S.suggested = g.target;
  S.ticked = new Set(g.yours !== g.target ? [g.yours] : []);
  renderPopover(); toast(`Suggested ${nameOf(g.target)}: ${g.why}. Press Relabel (Enter) to apply.`, 4500);
}
function snapCompartment() {
  if (!S.comp || !S.selList.length) return;
  const [c] = majority(S.comp, S.selList), keep = Array.from(S.selList).filter(i => S.comp[i] === c);
  applySel('and', keep, null, null, 'snap');
  toast(`Snapped to ${S.assist.compartments[c]} cells: ${nCells(keep.length)}`);
}

// ---------------------------------------------------------------- review queue (side tab): disagreement hotspots
async function loadQueue(kind) {
  try {
    const r = await api('/api/assist/queue?kind=' + kind);
    const cur = S.queue && S.queue.kind === kind ? S.queue.cur : -1;
    S.queue = { ...r, cur };
    renderReview();
  } catch (e) { fail(e); }
}
function confusionText(c) {      // an agent "agrees" when under 30% of Bill's main class are relabelled by it
  const b = nameOf(c.bill_eugene_code), off = k => c[k] !== c.bill_eugene_code && c[k + '_pct'] >= 30;
  const v = off('vestrum') && nameOf(c.vestrum), cc = off('claude_code') && nameOf(c.claude_code);
  if (v && v === cc) return `Bill ${b} → agents ${v} (V ${c.vestrum_pct}% · C ${c.claude_code_pct}%)`;
  const part = (n, name, pct) => n ? `${name} ${n} ${pct}%` : `${name} mostly agrees`;
  return `Bill ${b} → ${[part(v, 'Vestrum', c.vestrum_pct), part(cc, 'Claude', c.claude_code_pct)].sort((x, y) => x.includes('agrees') - y.includes('agrees')).join(' · ')}`;
}
function renderReview() {
  const pane = $('#reviewPane'); if (!pane) return;
  const Q = S.queue;
  $('#nReview').textContent = Q ? `${Q.n_done}/${Q.items.length}` : '';
  if (!Q) { pane.innerHTML = '<p class="hint">Loading disagreement hotspots…</p>'; return; }
  pane.innerHTML = `<h3>Review queue</h3>
    <div class="seg mini" id="qKind"><button data-k="any" class="${Q.kind === 'any' ? 'on' : ''}">Any agent ≠ Bill</button><button data-k="both" class="${Q.kind === 'both' ? 'on' : ''}">Both agents agree, Bill differs</button></div>
    <p class="hint" style="margin-top:8px">${Q.kind === 'both' ? 'Connected patches where both agents agree against Bill (1-cell gaps allowed)' : 'One confusion (Bill → agent label) in one Novae domain per item, adjacent cells only'};
      20–1,500 cells (bigger patches are cut into pieces). Ranked by expected fixes: a cell counts 1 when both agents differ from Bill, ⅓ when one does. An item is done when one edit covers half of it.</p>
    <div class="row"><b id="qProg">${fmt(Q.n_done)} / ${fmt(Q.items.length)} done</b><span class="grow"></span>
      <button class="btn small" id="qPrev" title="Previous item (Shift+J)">‹ Prev <kbd>⇧J</kbd></button><button class="btn small primary" id="qNext" title="Next item not done yet (J)">Next › <kbd>J</kbd></button></div>
    <div class="bar"><i style="width:${Q.items.length ? 100 * Q.n_done / Q.items.length : 0}%"></i></div>
    <div class="list" style="margin-top:8px"></div>`;
  pane.querySelectorAll('#qKind button').forEach(b => b.onclick = () => loadQueue(b.dataset.k));
  $('#qPrev').onclick = () => queueStep(-1); $('#qNext').onclick = () => queueStep(1);
  const list = pane.querySelector('.list');
  Q.items.slice(0, 300).forEach((it, k) => {
    const row = el('div', 'item' + (k === Q.cur ? ' cur' : '') + (it.done ? ' done' : ''), `<div class="meta"><b>#${k + 1}</b><span>${esc(it.section)}</span><span>${nCells(it.n)}</span><span title="expected fixes">≈ ${fmt(Math.round(it.score ?? it.n))}</span>${it.domain ? `<span>${esc(it.domain)}</span>` : ''}${it.done ? '<span class="tag ok">done</span>' : ''}</div>
      <div class="txt">${esc(confusionText(it.confusion))}</div>`);
    row.onclick = () => queueGo(k);
    list.append(row);
  });
  if (!Q.items.length) list.append(el('p', 'hint', 'No hotspots in this list.'));
}
async function queueGo(k) {
  const Q = S.queue; if (!Q || !Q.items[k]) return;
  const it = Q.items[k]; Q.cur = k;
  try {
    const r = await api(`/api/assist/queue/${Q.kind}/${it.id}`);
    const [x0, y0, x1, y1] = it.bbox_px, pad = Math.max(60, 0.15 * Math.max(x1 - x0, y1 - y0));
    fitImageRect(x0 - pad, y0 - pad, x1 + pad, y1 + pad);
    S.gestT0 = performance.now();
    applySel('replace', r.cells, { type: 'Assist', tool: 'queue', px: [[(x0 + x1) / 2, (y0 + y1) / 2]] }, S.panels[1], 'queue');
    showTab('review'); renderReview();
  } catch (e) { fail(e); }
}
function queueStep(dir) {
  const Q = S.queue;
  if (!Q) { showTab('review'); return; }
  if (!Q.items.length) { toast('No hotspots in this list.'); return; }
  const n = Q.items.length;
  if (dir < 0) { queueGo(Q.cur <= 0 ? n - 1 : Q.cur - 1); return; }
  for (let s = 1; s <= n; s++) { const k = (Q.cur + s + n) % n; if (!Q.items[k].done) { queueGo(k); return; } }
  toast('Every item in this list is done.');
}

// ---------------------------------------------------------------- right-click and header menus
function contextMenu(P, h, cx, cy) {
  const items = [];
  const add = (label, fn, cls = '') => items.push({ label, fn, cls });
  const sep = title => items.push({ sep: true, title });
  if (h.inSel) {
    sep(`Selection · ${nCells(S.selList.length)}`);
    const own = typeof P.layer === 'number' ? SRC[P.layer] : null;   // a method panel
    if (own) add(`Use this panel's labels here (${sourceName(own)})`, () => baseSel(own), 'strong');
    for (const k of SRC) if (k !== own) add(`Use ${sourceName(k)} labels here`, () => baseSel(k));
    add('Comment on the selection', () => commentDialog({ geometry: { type: 'Polygon', px: selOutline() } }));
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
  if (h.op && !h.inSel) {
    sep(`Edit · ${opText(h.op)}`);
    add('Select this region again', () => { S.gestT0 = performance.now(); selectPolygon(h.op.geometry.px, P, 'replace', 'reselect'); });
    add('Undo this edit', () => undo(h.op.id), 'danger');
  }
  if (h.cell != null) {
    sep('Cell');
    add('Show its labels', () => { S.sel = h.cell; redraw(); api('/api/cell/' + h.cell).then(showCell).catch(fail); });
    add('Select this cell', () => { S.gestT0 = performance.now(); applySel('replace', [h.cell], { type: 'Point', px: [[S.cells.px[h.cell], S.cells.py[h.cell]]] }, P, 'cell'); });
    add('Relabel this cell…', () => relabelDialog({ cells: [h.cell] }));
    add('Comment on this cell', () => commentDialog({ geometry: { type: 'Point', px: [[S.cells.px[h.cell], S.cells.py[h.cell]]] } }));
  }
  sep('Here');
  add('Lasso a region here', () => { setMode('lasso', true); toast('Drag to lasso the cells'); });
  add('Comment here', () => commentDialog({ geometry: { type: 'Point', px: [h.pt] } }));
  add('Annotate here', () => { setMode('annot', true); toast('Drag to draw the annotation'); });
  showMenu(items, cx, cy);
}
function showMenu(items, cx, cy) {
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

function relabelDialog({ cells }) {
  dialog(`<h3>Relabel this cell</h3>
    <p class="hint">Changes ${esc(layerName())} only. The three methods stay as they are.</p>
    <label>New label</label><select name="to">${labelOptions()}</select>
    <div class="acts"><button class="btn" value="cancel" formnovalidate>Cancel</button><button class="btn primary">Relabel</button></div>`,
  async fd => {
    const to = await ensureLabel(+fd.get('to')); if (to == null) return false;
    await applyOp({ op: 'relabel', to, cells, tool: 'cell-dialog' });
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
  if (t === 'review' && !S.queue) loadQueue('any');
  document.querySelectorAll('#tabs button').forEach(b => b.classList.toggle('on', b.dataset.tab === t));
  document.querySelectorAll('.pane').forEach(p => p.classList.toggle('on', p.dataset.pane === t));
}

function renderComments() {
  const pane = $('#commentsPane');
  $('#nComments').textContent = S.comments.length || '';
  if (!S.comments.length) { pane.innerHTML = '<p class="hint">No comments yet. Right-click → <b>Comment here</b>, or use <b>Comment</b> (C) / <b>Area</b> (M).</p>'; return; }
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
  const act = async a => { try { const r = await api('/api/vocab', { body: { action: a } }); setVocab(r.vocab); if (S.selList.length) selInfo(); renderAll(); } catch (e) { fail(e); } };
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
    const it = el('div', 'item', `<div class="meta"><span class="keychip" style="--bc:${opColour(o)}">${o.op === 'base' ? (o.scope ? 'start' : 'picked') : o.op}</span><span>${esc(opText(o))}</span>
      ${o.filter_name ? `<span class="tag">only ${esc(o.filter_name)}</span>` : ''}${o.filter_names ? `<span class="tag">only ${esc(o.filter_names.join(', '))} (${esc(o.filter_ref === 'corr' ? 'yours' : shortName(o.filter_ref))})</span>` : ''}${o.tool && o.tool !== 'section' ? `<span class="tag">${esc(o.tool)}</span>` : ''}<span>${esc(o.ts?.slice(11, 16))}</span></div>
      <div class="acts">${o.scope ? '' : '<button class="btn small" data-a="sel">Select region</button>'}<button class="btn small danger" data-a="undo">Undo</button></div>`);
    it.onclick = e => { const a = e.target.dataset.a; if (a === 'undo') undo(o.id); else if (a === 'sel') { flyTo(o.geometry.px); S.gestT0 = performance.now(); selectPolygon(o.geometry.px, S.panels[1], 'replace', 'reselect'); } else if (!o.scope) flyTo(o.geometry.px); };
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

function renderAll() { renderEdit(); renderComments(); renderAnnots(); renderLabels(); renderLog(); renderLegend(); renderReview(); if (S.selInfo) renderPopover(); redraw(); }

// ---------------------------------------------------------------- wiring
document.querySelectorAll('#modes button, #modes2 button, #modes3 button').forEach(b => b.onclick = () => setMode(b.dataset.mode));
document.querySelectorAll('#tabs button').forEach(b => b.onclick = () => showTab(b.dataset.tab));
$('#disagree').onchange = e => { S.disagree = e.target.checked; redraw(); };
$('#showOps').onchange = e => { S.showOps = e.target.checked; redraw(); };
$('#dotSize').oninput = e => { S.dot = +e.target.value; redraw(); };
$('#wash').oninput = e => { S.wash = +e.target.value; redraw(); };
$('#secJump').onchange = e => { e.target.blur(); if (e.target.value === '') { S.panels[0].viewer.viewport.goHome(); return; } const b = S.secBoxes[+e.target.value]; fitImageRect(b.x0, b.y0, b.x1, b.y1); };
$('#undoBtn').onclick = () => undo();
$('#saveBtn').onclick = saveDialog;
$('#reviewerBtn').onclick = () => reviewerDialog(false);
// holding Shift / Alt (to add / subtract) fades the popover so it never blocks the next stroke
const ghost = e => $('#pop').classList.toggle('ghost', e.shiftKey || e.altKey);
window.addEventListener('keydown', ghost); window.addEventListener('keyup', ghost); window.addEventListener('blur', () => $('#pop').classList.remove('ghost'));
window.addEventListener('keydown', e => {
  const a = document.activeElement;
  if ((a.tagName === 'INPUT' && !/checkbox|range|radio/.test(a.type)) || /TEXTAREA|SELECT/.test(a.tagName) || $('#dlg').open) return;
  const k = e.key.toLowerCase();
  if ((e.ctrlKey || e.metaKey) && k === 'z') { e.preventDefault(); undo(); return; }
  if (e.ctrlKey || e.metaKey || e.altKey) return;
  const modes = { v: 'pan', l: 'lasso', e: 'lasso', p: 'poly', r: 'rect', b: 'brush', s: 'cell', c: 'pin', m: 'area', a: 'annot', w: 'wand', g: 'walker' };
  if (S.assist?.stains) modes.k = 'comp';
  if (S.novaeRes) modes.n = 'novae';
  if (modes[k]) setMode(modes[k]);
  else if (k === 'j') queueStep(e.shiftKey ? -1 : 1);
  else if (k === 'd') { const c = $('#disagree'); c.checked = !c.checked; c.onchange({ target: c }); }
  else if (k === '[' || k === ']') setBrush(S.brushPx * (k === '[' ? 0.8 : 1.25));
  else if (/^[1-9]$/.test(k) && S.selInfo) { const c = quickList()[+k - 1]; if (c != null) { S.target = c; renderPopover(); } }
  else if (k === 'enter') {
    if (S.draw?.kind === 'poly') { e.preventDefault(); finishPoly(); }
    else if (S.selInfo) { e.preventDefault(); relabelSel(); }
  }
  else if (k === 'escape') {
    if (!$('#ctx').hidden) closeMenu();
    else if (S.draw) { S.draw = null; redraw(); }
    else if (S.selList.length || S.sel != null) deselect();
    else if (S.mode !== 'pan') setMode('pan');
  }
});

// read-only handle for the UI tests: state, and image px -> page coordinates of one panel
window.PathQC = { S, sectionInView, toClient: (k, x, y) => { const P = S.panels[k], m = mapping(P), r = P.el.getBoundingClientRect(), [sx, sy] = m.toS(x, y); return [r.left + sx, r.top + sy]; } };

let saved = null; try { saved = localStorage.getItem('pathqc.reviewer'); } catch (e) {}
const urlReviewer = new URLSearchParams(location.search).get('reviewer');
if (urlReviewer || saved) { S.reviewer = urlReviewer || saved; load().catch(fail); } else reviewerDialog(true);
})();
