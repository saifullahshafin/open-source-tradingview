// TradeX indicators, part 1: state, chart messaging, add/remove, re-apply on engine load.
// Loaded by hub.html before its inline script; uses hub globals ($, st, openPanel) only at call time.
let INDS = []; try { INDS = JSON.parse(localStorage.getItem("chartHubIndicators") || "[]"); } catch (_) {}   // on the chart (any origin)
let FAVS = []; try { FAVS = JSON.parse(localStorage.getItem("chartHubFavs") || "[]"); } catch (_) {}         // starred rows
let MINE = []; try { MINE = JSON.parse(localStorage.getItem("chartHubScripts") || "[]"); } catch (_) {}      // own Pine scripts
const keep = (k, v) => { try { localStorage.setItem(k, JSON.stringify(v)); } catch (_) {} };
const saveInds = () => { keep("chartHubIndicators", INDS); if (window.layoutsDirty) window.layoutsDirty(); };  // layouts.js autosave
// Order-flow views moved to the chart-type menu (Volume footprint, Session volume profile), 2026-11-12. The first version
// switched two indicators on by itself (Delta pane, SVP indicator); take those auto-added ones off once. Both stay
// available in Indicators > Vela native for anyone who wants them.
try {
  if (!localStorage.getItem("chartHubOrderflowV2")) {
    INDS = INDS.filter((x) => !/^native:orderflow_(delta|profile):seed$/.test(x.key || ""));
    localStorage.setItem("chartHubOrderflowV2", "1"); saveInds();
  }
} catch (_) {}
// Before 2026-11-15 a visit to the Lightweight or KLineChart engine switched every Pine indicator off with this error. Turn them back on.
INDS.forEach((x) => { if (x.err && /run on Vela for now|Vela only/i.test(x.err)) { x.err = null; x.on = true; } });
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
  // TradingView-style "Style" settings (colour, thickness, show) for every plot line: pine_style.js, 2026-11-13
  return window.PineStyle ? PineStyle.addStyleInputs(c.source) : c.source;
}
// Pine and Vela-native indicators draw only on the Vela engine. On another engine an indicator stays on and waits
// (auto = reload after an engine switch); a user who adds one there is taken to Vela, where it draws.
async function pineAdd(ind, auto) {
  if (typeof st !== "undefined" && st.engine !== "vela") {
    if (auto) { ind.wait = true; indChanged(); return; }
    ind.wait = false; ind.on = true; saveInds();
    st.engine = "vela"; save(); paint(); loadEngine();   // the fresh Vela page re-adds every indicator that is on
    return;
  }
  ind.wait = false;
  // Multi-pane: remember which cell owns this indicator (active cell when first added; restored cell on reapply).
  if (ind.cell == null && !ind.cellId) {
    const a = await ask("cell:active", {}, 2000);
    if (a && a.ok) { ind.cell = a.index | 0; if (a.id) ind.cellId = a.id; }
  }
  busy[ind.key] = true; indChanged();
  let r = { ok: false, error: "chart did not answer" };
  for (let attempt = 0; attempt < 25; attempt++) {
    r = ind.kind === "native"
      ? await ask("native:add", { type: ind.type, inputs: ind.inputs, cell: ind.cell, cellId: ind.cellId })
      : await ask("pine:add", { key: ind.key, source: prepared(ind), inputs: ind.inputs, props: ind.props, cell: ind.cell, cellId: ind.cellId }, 20000);
    if (r && r.retry) { await new Promise((x) => setTimeout(x, 80)); continue; }
    break;
  }
  onPineResult({ key: ind.key, ...r });
}
function pineRemove(ind) {
  const id = liveIds[ind.key];
  if (id) { const w = frameWin(); if (w) w.postMessage({ cmd: "pine:remove", id }, location.origin); }
  delete liveIds[ind.key];
}
function onPineResult(m) {
  const ind = INDS.find((x) => x.key === m.key); delete busy[m.key]; if (!ind) return;
  if (m.ok) {
    liveIds[ind.key] = m.id; ind.err = null; ind.pending = !!m.pending;
    // Do not overwrite a known cell with a fallback; only fill missing fields.
    if (ind.cell == null && m.cell != null) ind.cell = m.cell;
    if (!ind.cellId && m.cellId) ind.cellId = m.cellId;
  } else { ind.err = m.error || "failed"; ind.on = false; }
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
async function reapplyAll() {
  liveIds = {};
  const ons = INDS.filter((x) => x.on);
  // Wait until multi-pane cells exist, otherwise every indicator falls onto the only ready cell.
  const need = ons.reduce((n, x) => Math.max(n, (Number.isInteger(+x.cell) ? (+x.cell + 1) : 1)), 1);
  const t0 = Date.now();
  while (Date.now() - t0 < 4000) {
    const r = await ask("cells:count", {}, 800);
    if (r && r.ok && (r.count || 0) >= need) break;
    await new Promise((x) => setTimeout(x, 50));
  }
  for (const x of ons) await pineAdd(x, true);
}
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
