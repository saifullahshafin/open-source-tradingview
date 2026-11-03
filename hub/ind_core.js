// Chart Hub indicators, part 1: state, chart messaging, add/remove, re-apply on engine load.
// Loaded by hub.html before its inline script; uses hub globals ($, st, openPanel) only at call time.
let INDS = []; try { INDS = JSON.parse(localStorage.getItem("chartHubIndicators") || "[]"); } catch (_) {}   // on the chart (any origin)
let FAVS = []; try { FAVS = JSON.parse(localStorage.getItem("chartHubFavs") || "[]"); } catch (_) {}         // starred rows
let MINE = []; try { MINE = JSON.parse(localStorage.getItem("chartHubScripts") || "[]"); } catch (_) {}      // own Pine scripts
const keep = (k, v) => { try { localStorage.setItem(k, JSON.stringify(v)); } catch (_) {} };
const saveInds = () => keep("chartHubIndicators", INDS);
INDS.forEach((x) => { if (x.err && /Unsupported Pine Script version|version 4 not supported/i.test(x.err)) { x.err = null; x.on = true; } });  // old v4 failures: the converter handles them now
let liveIds = {}, busy = {}, reapply = false;
const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
function frameWin() { return $("frame").contentWindow; }

// request/reply with the engine page (lab.js answers {evt:"reply", rid})
let _rid = 0; const _waits = {};
function ask(cmd, extra = {}, ms = 8000) {
  return new Promise((done) => {
    const w = frameWin(); if (!w) return done({ ok: false, error: "no chart" });
    const id = ++_rid; _waits[id] = done;
    setTimeout(() => { if (_waits[id]) { delete _waits[id]; done({ ok: false, error: "chart did not answer" }); } }, ms);
    w.postMessage({ cmd, rid: id, ...extra }, location.origin);
  });
}
function onReply(m) { const f = _waits[m.rid]; if (f) { delete _waits[m.rid]; f(m); } }

// Source the engine runs: v1-v4 converted to v5, plus engine fixes. Original source is kept untouched.
function prepared(ind) {
  if (!window.PineConvert) return ind.source;
  const c = PineConvert.convert(ind.source);
  ind.convFrom = c.changed && !c.compatOnly ? c.from : null; ind.notes = c.warnings || [];
  return c.source;
}
async function pineAdd(ind) {
  busy[ind.key] = true; indChanged();
  if (ind.kind === "native") { const r = await ask("native:add", { type: ind.type, inputs: ind.inputs }); onPineResult({ key: ind.key, ...r }); return; }
  const w = frameWin(); if (!w) return;
  w.postMessage({ cmd: "pine:add", key: ind.key, source: prepared(ind), inputs: ind.inputs, props: ind.props }, location.origin);
}
function pineRemove(ind) {
  const id = liveIds[ind.key];
  if (id) { const w = frameWin(); if (w) w.postMessage({ cmd: "pine:remove", id }, location.origin); }
  delete liveIds[ind.key];
}
function onPineResult(m) {
  const ind = INDS.find((x) => x.key === m.key); delete busy[m.key]; if (!ind) return;
  if (m.ok) { liveIds[ind.key] = m.id; ind.err = null; ind.pending = !!m.pending; } else { ind.err = m.error || "failed"; ind.on = false; }
  saveInds(); indChanged();
  if (window.onEditorResult) onEditorResult(ind, m);
}
// Removed from Vela's own legend (trash icon): forget it here too, so it is not re-added on reload.
function onLegendRemoved(id) {
  const key = Object.keys(liveIds).find((k) => liveIds[k] === id); if (!key) return;
  delete liveIds[key]; INDS = INDS.filter((x) => x.key !== key); saveInds(); indChanged();
}
function onReady(m) {
  const key = Object.keys(liveIds).find((k) => liveIds[k] === m.id), ind = key && INDS.find((x) => x.key === key);
  if (ind && ind.pending) { ind.pending = false; saveInds(); indChanged(); }
}
function onInputs(m) {
  const key = Object.keys(liveIds).find((k) => liveIds[k] === m.id), ind = key && INDS.find((x) => x.key === key); if (!ind) return;
  ind.inputs = m.inputs; ind.props = m.props; saveInds();
}
function reapplyAll() { liveIds = {}; INDS.filter((x) => x.on).forEach(pineAdd); }
function removeInd(ind) { pineRemove(ind); INDS = INDS.filter((y) => y !== ind); saveInds(); indChanged(); }
function toggleInd(ind, on) { ind.on = on; ind.err = null; if (on) pineAdd(ind); else pineRemove(ind); saveInds(); indChanged(); }
// One hook every view listens to (dialog list, Pine editor status).
function indChanged() {
  if (typeof rowResult === "function") INDS.forEach(rowResult);
  if (typeof renderIndList === "function" && $("indDlg") && !$("indDlg").hidden) renderIndDialog();
  if (typeof editorStatus === "function") editorStatus();
}
// Kept for the shell's old call sites.
function renderIndicators() { openIndDlg(); }
