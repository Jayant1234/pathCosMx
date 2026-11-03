/* PathQC start screen: reviewer, slide pack, optional H&E + registration (server/shell.py). */
(() => {
'use strict';
const $ = s => document.querySelector(s);
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const fmt = n => Number(n).toLocaleString();
const T = { st: null, heImg: null, siImg: null, cells: null, pairs: [], pending: null, poll: 0 };

async function api(path, body) {
  const r = await fetch(path, body === undefined ? {} : { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  let j = null; try { j = await r.json(); } catch (e) {}
  if (!r.ok) throw new Error(j?.error || r.statusText);
  return j;
}
function toast(msg, ms = 3800) { const t = $('#toast'); t.textContent = msg; t.hidden = false; clearTimeout(toast.h); toast.h = setTimeout(() => t.hidden = true, ms); }
const fail = e => { console.error(e); toast('Error: ' + e.message, 7000); };
const kv = rows => rows.filter(r => r[1] != null && r[1] !== '').map(([k, v]) => `<span>${esc(k)}</span><span>${v}</span>`).join('');
const go = url => { try { localStorage.setItem('pathqc.reviewer', $('#reviewer').value.trim()); } catch (e) {} location.href = url; };

// ---------------------------------------------------------------- setup step
function render(st) {
  T.st = st;
  $('#settingsFile').textContent = 'settings: ' + st.settings_file;
  if (!$('#reviewer').value) { let saved = ''; try { saved = localStorage.getItem('pathqc.reviewer') || ''; } catch (e) {} $('#reviewer').value = st.reviewer || saved; }
  const p = st.pack;
  $('#packInfo').innerHTML = kv([['Pack', `<b>${esc(p.name)}</b>${p.demo ? ' <span class="tag dev">synthetic demo</span>' : ''}`], ['Dataset', esc(p.dataset)],
    ['Cells', fmt(p.n_cells)], ['Sections', esc(p.sections.join(', '))], ['Folder', `<span class="mono">${esc(p.path)}</span>`],
    ['Slide image now', st.image === 'he' ? '<b>H&amp;E</b> · ' + esc(st.registration?.summary || '') : 'stand-in (drawn from cell positions)']]);
  const prob = $('#heProblem');
  prob.hidden = !st.problem;
  if (st.problem) prob.innerHTML = `${esc(st.problem)} <b>Locate the file again</b>, or continue with the stand-in.`;
  const h = st.he, s = st.saved_he;
  if (h) $('#heInfo').innerHTML = kv([['File', `<b>${esc(h.name)}</b>`], ['Folder', `<span class="mono">${esc(h.path)}</span>`],
    ['Size', `${fmt(h.width)} × ${fmt(h.height)} px · ${(h.fingerprint.size / 1e6).toFixed(1)} MB`], ['Reader', `${esc(h.vendor)} (${esc(h.reader)})`],
    ['Resolution', h.mpp ? `${h.mpp.toFixed(3)} µm/px` : 'not in the file (taken from the registration)'],
    ['Registered', h.registered ? '<span class="st-ok">yes, for this pack</span> (registering again replaces it)' : 'not yet']]);
  else if (s) $('#heInfo').innerHTML = kv([['Saved', `<b>${esc(s.name)}</b>`], ['Path', `<span class="mono">${esc(s.path)}</span>`]]);
  else $('#heInfo').innerHTML = '<span class="hint">None. The viewer works with the stand-in; add the H&E any time from “Change slide / H&E”.</span>';
  $('#heFormats').textContent = 'SVS, NDPI, MRXS, SCN, pyramidal TIFF… (' + st.exts.join(' ') + ')';
  $('#heBtn').disabled = !h;
  const sb = $('#savedHeBtn');
  sb.hidden = true;
  const useHe = () => saveReviewer().then(ok => ok && api('/api/setup/use_he', {}).then(r => go(r.redirect))).catch(fail);
  if (h && h.registered) { sb.hidden = false; sb.textContent = 'Open with its saved registration'; sb.onclick = useHe; }
  else if (st.ready) { sb.hidden = false; sb.textContent = `Open the viewer (${st.image === 'he' ? 'H&E' : 'stand-in'})`; sb.onclick = () => saveReviewer().then(ok => ok && go('/?reviewer=' + encodeURIComponent(ok))); }
  else if (s && !st.problem && st.chosen === 'standin') { sb.hidden = false; sb.textContent = 'Use the registered H&E'; sb.onclick = useHe; }
}

async function saveReviewer() {
  const n = $('#reviewer').value.trim();
  if (!n) { toast('Enter your name first'); $('#reviewer').focus(); return null; }
  await api('/api/setup/reviewer', { name: n });
  return n;
}

async function pick(kind) {
  const r = await api('/api/setup/pick', { kind });
  if (!r.available) {
    toast('No file dialog on this machine: paste the full path instead.');
    if (kind === 'folder') { $('#packPasteRow').hidden = false; $('#packPath').focus(); } else $('#hePath').focus();
    return null;
  }
  return r.path;
}

async function openHe(path) {
  if (!path) return;
  $('#heOpen').disabled = true; $('#heOpen').textContent = 'Opening…';
  try { render(await api('/api/setup/he', { path })); toast('H&E opened'); }
  catch (e) { fail(e); }
  finally { $('#heOpen').disabled = false; $('#heOpen').textContent = 'Open'; }
}
async function openPack(path) {
  if (!path) return;
  try { render(await api('/api/setup/pack', { path })); toast('Pack connected'); } catch (e) { fail(e); }
}

$('#pickHe').onclick = () => pick('file').then(p => { if (p) { $('#hePath').value = p; openHe(p); } }).catch(fail);
$('#heOpen').onclick = () => openHe($('#hePath').value.trim());
$('#hePath').onkeydown = e => { if (e.key === 'Enter') openHe(e.target.value.trim()); };
$('#pickPack').onclick = () => pick('folder').then(p => { if (p) openPack(p); }).catch(fail);
$('#packOpen').onclick = () => openPack($('#packPath').value.trim());
$('#standinBtn').onclick = async () => { try { if (!await saveReviewer()) return; go((await api('/api/setup/standin', {})).redirect); } catch (e) { fail(e); } };
$('#heBtn').onclick = async () => { try { if (!await saveReviewer()) return; await showReg(); } catch (e) { fail(e); } };

// ---------------------------------------------------------------- registration step
const loadImg = src => new Promise((ok, no) => { const i = new Image(); i.onload = () => ok(i); i.onerror = () => no(new Error('could not load ' + src)); i.src = src; });

async function showReg() {
  $('#stepSetup').hidden = true; $('#stepReg').hidden = false;
  $('#regHeName').textContent = T.st.he.name;
  const bust = '&v=' + T.st.he.fingerprint.id;
  [T.heImg, T.siImg] = await Promise.all([loadImg('/api/setup/he/thumb.jpg?size=1024' + bust), loadImg('/api/setup/standin/thumb.jpg?size=1024&p=' + encodeURIComponent(T.st.pack.path))]);
  if (!T.cells) T.cells = new Float32Array(await (await fetch('/api/setup/cells_mm.bin?n=40000')).arrayBuffer());
  drawAnchors();
  if (T.st.candidate) showCheck(T.st.candidate);
}
$('#regBack').onclick = () => { $('#stepReg').hidden = true; $('#stepSetup').hidden = false; };
document.querySelectorAll('#regTabs button').forEach(b => b.onclick = () => {
  document.querySelectorAll('#regTabs button').forEach(x => x.classList.toggle('on', x === b));
  document.querySelectorAll('.st-pane').forEach(p => p.classList.toggle('on', p.dataset.pane === b.dataset.tab));
  drawAnchors();
});

$('#regRun').onclick = async () => {
  try {
    $('#regRun').disabled = true;
    await api('/api/setup/register/auto', {});
    clearInterval(T.poll);
    T.poll = setInterval(async () => {
      const st = await api('/api/setup/state'); const j = st.job;
      $('#regProg i').style.width = (100 * (j.progress || 0)) + '%';
      $('#regMsg').textContent = j.state === 'failed' ? 'Failed: ' + j.message : (j.message || '');
      if (j.state !== 'running') {
        clearInterval(T.poll); $('#regRun').disabled = false; T.st = st;
        if (j.state === 'done' && st.candidate) { $('#regMsg').textContent = 'Done in ' + st.candidate.seconds + ' s'; showCheck(st.candidate); }
      }
    }, 500);
  } catch (e) { $('#regRun').disabled = false; fail(e); }
};

function acceptLabel() { return $('#acceptBtn').classList.contains('primary') ? 'Accept and open the viewer' : 'Accept anyway and open the viewer'; }

function showCheck(c) {
  $('#checkCard').hidden = false;
  $('#regResult').innerHTML = kv([['Method', esc(c.method)], ['Orientation', esc(c.orientation) + ' (relative to the stand-in)'],
    ['H&E resolution', `${c.um_per_px.toFixed(3)} µm/px`], ['Fit score', c.score != null ? `r = ${c.score.toFixed(3)}` : ''],
    ['Anchor residuals', c.residual_um ? c.residual_um.map(v => v.toFixed(1)).join(', ') + ` µm (mean ${c.mean_residual_um.toFixed(1)})` : ''],
    ['Note', esc(c.refine_note || '')]]);
  const w = $('#regWarn'), unsure = c.status === 'uncertain' || c.confident === false, b = $('#acceptBtn');
  w.hidden = !unsure;
  w.textContent = unsure ? `Not sure this is right${c.reason ? ' (' + c.reason + ')' : ''}. Check the overlay; use anchor points if the cells do not sit on the tissue.` : '';
  b.disabled = false; b.classList.toggle('primary', !unsure);       // uncertain: accepting is possible, not suggested
  b.textContent = acceptLabel();
  T.cand = c; drawOverlay();
}

function drawOverlay() {
  const c = T.cand, img = T.heImg; if (!c || !img) return;
  const cv = $('#overlay'), ctx = cv.getContext('2d');
  cv.width = img.naturalWidth; cv.height = img.naturalHeight;
  ctx.drawImage(img, 0, 0);
  const A = c.mm_to_px, sx = img.naturalWidth / T.st.he.width, sy = img.naturalHeight / T.st.he.height, P = T.cells;
  ctx.globalAlpha = +$('#opacity').value; ctx.fillStyle = '#00b85c';
  for (let i = 0; i < P.length; i += 2) {
    const x = A[0][0] * P[i] + A[0][1] * P[i + 1] + A[0][2], y = A[1][0] * P[i] + A[1][1] * P[i + 1] + A[1][2];
    ctx.fillRect(x * sx - 0.75, y * sy - 0.75, 1.5, 1.5);
  }
  ctx.globalAlpha = 1;
}
$('#opacity').oninput = drawOverlay;

// anchors: click the H&E, then the same place on the stand-in
function canvasPt(cv, img, e) { const r = cv.getBoundingClientRect(); return [(e.clientX - r.left) * img.naturalWidth / r.width, (e.clientY - r.top) * img.naturalHeight / r.height]; }
function drawAnchors() {
  for (const [id, img, key] of [['#heCanvas', T.heImg, 'he'], ['#siCanvas', T.siImg, 'si']]) {
    if (!img) continue;
    const cv = $(id), ctx = cv.getContext('2d'); cv.width = img.naturalWidth; cv.height = img.naturalHeight;
    ctx.drawImage(img, 0, 0);
    const pts = T.pairs.map(p => p[key]).concat(T.pending && key === 'he' ? [T.pending.he] : []);
    pts.forEach((p, k) => {
      ctx.fillStyle = '#ff2d55'; ctx.strokeStyle = '#fff'; ctx.lineWidth = 2;
      ctx.beginPath(); ctx.arc(p[0], p[1], 6, 0, 6.2832); ctx.fill(); ctx.stroke();
      ctx.fillStyle = '#111'; ctx.font = '700 13px sans-serif'; ctx.fillText(String(k + 1), p[0] + 8, p[1] - 6);
    });
  }
  $('#pairList').textContent = T.pairs.length ? `${T.pairs.length} pair${T.pairs.length > 1 ? 's' : ''}` + (T.pending ? ' · now click the same place on the stand-in' : '') : (T.pending ? 'Now click the same place on the stand-in' : 'No points yet.');
  $('#anchorFit').disabled = T.pairs.length < 3;
}
$('#heCanvas').onclick = e => { if (!T.heImg) return; T.pending = { he: canvasPt($('#heCanvas'), T.heImg, e) }; drawAnchors(); };
$('#siCanvas').onclick = e => {
  if (!T.siImg) return;
  if (!T.pending) { toast('Click the H&E first, then the same place here'); return; }
  T.pairs.push({ he: T.pending.he, si: canvasPt($('#siCanvas'), T.siImg, e) }); T.pending = null; drawAnchors();
};
$('#anchorUndo').onclick = () => { if (T.pending) T.pending = null; else T.pairs.pop(); drawAnchors(); };
$('#anchorClear').onclick = () => { T.pairs = []; T.pending = null; drawAnchors(); };
$('#anchorFit').onclick = async () => {
  const st = T.st, hx = st.he.width / T.heImg.naturalWidth, hy = st.he.height / T.heImg.naturalHeight;
  const sx = st.standin.width / T.siImg.naturalWidth, sy = st.standin.height / T.siImg.naturalHeight;
  const pairs = T.pairs.map(p => ({ he: [p.he[0] * hx, p.he[1] * hy], standin: [p.si[0] * sx, p.si[1] * sy] }));
  $('#anchorFit').disabled = true; $('#anchorFit').textContent = 'Fitting…';
  try { const r = await api('/api/setup/register/anchors', { pairs, refine: $('#anchorRefine').checked }); T.st = r; showCheck(r.candidate); }
  catch (e) { fail(e); }
  finally { $('#anchorFit').textContent = 'Fit from points'; drawAnchors(); }
};

$('#acceptBtn').onclick = async () => {
  $('#acceptBtn').disabled = true; $('#acceptBtn').textContent = 'Saving…';
  try { go((await api('/api/setup/register/accept', {})).redirect); }
  catch (e) { fail(e); $('#acceptBtn').disabled = false; $('#acceptBtn').textContent = acceptLabel(); }
};

api('/api/setup/state').then(render).catch(fail);
})();
