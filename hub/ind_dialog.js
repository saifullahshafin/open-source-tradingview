// TradeX indicators, part 2: the "Indicators, metrics, and strategies" dialog.
// Left: Personal (Favorites, My scripts, On chart) / Built-in (TradingView technicals, TradeX library, Vela native) /
// Community (Editors' picks, Top, Trending). Right: chips + list. Clicking a row adds it to the chart.
// TradingView lists + open-source Pine code come from /api/tv/* (desktop: private part; web: a small proxy on our own
// address). Where no such route answers, those sections simply stay hidden. Your own Pine (v1-v6): the Pine Editor.
const SECTIONS = [
  ["PERSONAL", [["fav", "Favorites"], ["mine", "My scripts"], ["chart", "On chart"]]],
  ["BUILT-IN", [["lib", "TradeX library"], ["vela", "Vela native"]]],
];
let dlg = { sec: "lib", chip: "1", q: "", search: null, rowMsg: {}, moved: false };
// TradingView lists: shown only when /api/tv answers (checked once per dialog open until it works).
let TV = false, tvBusy = null;
function tvProbe() {
  if (TV) return Promise.resolve(true);
  if (tvBusy) return tvBusy;
  tvBusy = (async () => {
    try {
      const j = await (await fetch("/api/tv/list?kind=standard&type=1")).json();
      if (!j || !Array.isArray(j.results) || !j.results.length) return false;
      CAT["tv:1"] = j.results.map(tvRow); CAT["tv:2"] = CAT["tv:1"];
      TV = true;
      SECTIONS[1][1].unshift(["tv", "TradingView technicals"]);
      SECTIONS.push(["COMMUNITY", [["editors", "Editors' picks"], ["top", "Top"], ["trending", "Trending"]]]);
      if (!dlg.moved && dlg.sec === "lib") dlg.sec = "tv";
      return true;
    } catch (_) { return false; } finally { tvBusy = null; }
  })();
  return tvBusy;
}
const CAT = {};  // loaded lists, by "sec:chip"

function openIndDlg() {
  let d = $("indDlg");
  if (!d) {
    d = document.createElement("div"); d.id = "indDlg"; d.hidden = true;
    d.innerHTML = `<div class="dlg" role="dialog" aria-label="Indicators, metrics, and strategies">
      <div class="dlg-h"><span>Indicators, metrics, and strategies</span><button class="dlg-x" title="Close">&times;</button></div>
      <div class="dlg-s"><input placeholder="Search" spellcheck="false" autocomplete="off"></div>
      <div class="dlg-b"><nav class="dlg-nav"></nav><div class="dlg-main"><div class="dlg-chips"></div><div class="dlg-list"></div></div></div></div>`;
    document.body.appendChild(d);
    d.onclick = (e) => { if (e.target === d) closeIndDlg(); };
    d.querySelector(".dlg-x").onclick = closeIndDlg;
    const inp = d.querySelector(".dlg-s input");
    inp.oninput = () => { dlg.q = inp.value.trim(); startSearch(dlg.q); };
    inp.onkeydown = (e) => { if (e.key === "Enter" && dlg.q) startSearch(dlg.q, true); if (e.key === "Escape") closeIndDlg(); };
  }
  d.hidden = false; renderIndDialog(); setTimeout(() => d.querySelector(".dlg-s input").focus(), 0);
  if (!TV) tvProbe().then((ok) => { if (ok && !$("indDlg").hidden) { $("indDlg").querySelector(".dlg-s input").placeholder = "Search everything: TradingView, TradeX, Vela, your scripts (or paste a TradingView link)"; renderIndDialog(); } });
}
function closeIndDlg() { const d = $("indDlg"); if (d) d.hidden = true; }

function renderIndDialog() {
  const nav = $("indDlg").querySelector(".dlg-nav"); nav.innerHTML = "";
  SECTIONS.forEach(([title, items]) => {
    nav.insertAdjacentHTML("beforeend", `<div class="dlg-gh">${title}</div>`);
    items.forEach(([id, label]) => {
      const b = document.createElement("button"); b.className = "dlg-ni" + (dlg.sec === id && !dlg.search ? " on" : "");
      b.textContent = label + (id === "chart" ? ` (${INDS.length})` : id === "fav" && FAVS.length ? ` (${FAVS.length})` : id === "mine" && MINE.length ? ` (${MINE.length})` : "");
      b.onclick = () => { dlg.sec = id; dlg.moved = true; dlg.search = null; dlg.q = ""; $("indDlg").querySelector(".dlg-s input").value = ""; renderIndDialog(); };
      nav.appendChild(b);
    });
  });
  const pe = document.createElement("button"); pe.className = "dlg-ni dlg-pine"; pe.textContent = "Open Pine Editor";
  pe.onclick = () => { closeIndDlg(); openPanelByName("pine"); }; nav.appendChild(pe);
  renderIndList();
}

// ---- data per section, normalised to rows {src, id, name, sub, likes, open, kind, type} ----
async function loadCat(sec, chip) {
  const k = sec + ":" + chip; if (CAT[k]) return CAT[k];
  let rows = [];
  if (sec === "lib") {   // TradeX's own Pine scripts (pine_library.js)
    rows = (window.TX_PINE_LIB || []).map((x) => ({ src: "lib", id: "lib:" + x.id, libId: x.id, name: x.name, sub: x.sub, open: true, kind: "indicator" }));
  } else if (sec === "vela") {
    // The chart may still be loading when the dialog opens: Vela then answers with an empty list. Ask again a few
    // times, and never keep an empty answer (it used to stay empty for the whole visit, 2026-11-25).
    let r = await ask("native:list");
    for (let i = 0; i < 8 && st.engine === "vela" && r.ok && !(r.list || []).length; i++) { await new Promise((d) => setTimeout(d, 1000)); r = await ask("native:list"); }
    if (r.ok && !(r.list || []).length) r = { ok: false, list: [], error: "The chart is still loading. Close and open this panel again in a moment." };
    rows = (r.list || []).map((n) => ({ src: "vela", id: "vela:" + n.type, type: n.type, name: n.title, sub: "Vela built-in, computed natively", open: n.supported, kind: "indicator", beta: n.beta, present: n.present, multi: n.multi }));
    if (!r.ok) rows.err = st.engine === "vela" ? (r.error || "Vela did not answer") : "Vela native indicators need the Vela engine.";
  } else if (["tv", "editors", "top", "trending"].includes(sec)) {
    try {
      const j = await (await fetch(`/api/tv/list?kind=${sec === "tv" ? "standard" : sec}&type=${chip}`)).json();
      if (j.error || !Array.isArray(j.results)) { rows = []; rows.err = "TradingView list did not load: " + (j.error || "no answer") + ". Try again in a moment."; }
      else rows = j.results.map(tvRow);
    } catch (e) { rows = []; rows.err = "TradingView list did not load. Check your connection and try again."; }
  } else {
    rows.err = "Unknown list.";
  }
  if (!rows.err) CAT[k] = rows; return rows;
}

// TradingView sends some names HTML-encoded ("Elder&#039;s"); decode once so the list shows the real text.
const unent = (t) => { const x = document.createElement("textarea"); x.innerHTML = t == null ? "" : String(t); return x.value; };
const tvRow = (r) => ({ src: "tv", id: r.id, name: unent(r.name), sub: r.builtin ? (r.short || "TradingView built-in") : (r.author || ""), author: r.builtin ? "TradingView" : (r.author || ""), likes: r.likes, open: r.open, kind: r.kind, builtin: r.builtin, tag: r.builtin ? "TradingView" : "Community" });

// ONE search for everything (his ask 2026-11-25): your scripts, TradeX library, Vela native, every TradingView list
// already loaded, plus TradingView's own search (all public scripts) as you type. A pasted TradingView script link
// (tradingview.com/script/...) finds that exact script, also ones TradingView's search leaves out.
let searchTimer = 0;
function startSearch(q, now) {
  clearTimeout(searchTimer);
  if (!q) { dlg.search = null; renderIndDialog(); return; }
  if (!dlg.search || dlg.search.q !== q) dlg.search = { q, rows: null, err: "" };
  renderIndDialog();
  if (st.engine === "vela" && !CAT["vela:1"]) loadCat("vela", "1").then(() => { if (dlg.search) renderIndList(); });
  searchTimer = setTimeout(() => remoteSearch(q), now ? 0 : 450);
}
async function remoteSearch(q) {
  const link = /tradingview\.com\/script\//i.test(q);
  if (!TV && !link) await tvProbe();
  if (!dlg.search || dlg.search.q !== q) return;
  if (!TV) { dlg.search.rows = []; return renderIndList(); }
  try {
    const j = await (await fetch(link ? "/api/tv/link?url=" + encodeURIComponent(q) : "/api/tv/search?q=" + encodeURIComponent(q))).json();
    if (!dlg.search || dlg.search.q !== q) return;
    if (j.error) throw new Error(j.error);
    dlg.search.rows = j.results.map(tvRow);
  } catch (e) { if (!dlg.search || dlg.search.q !== q) return; dlg.search.rows = []; dlg.search.err = String(e.message || e); }
  renderIndList();
}
function localPool() {
  const out = MINE.map((m) => ({ src: "mine", id: "mine:" + m.id, mineId: m.id, name: m.name, sub: "your Pine" + (m.pine ? " v" + m.pine : ""), author: "you", open: true, kind: "indicator", tag: "My script" }));
  (window.TX_PINE_LIB || []).forEach((x) => out.push({ src: "lib", id: "lib:" + x.id, libId: x.id, name: x.name, sub: x.sub, author: "TradeX", open: true, kind: "indicator", tag: "TradeX" }));
  for (const k of ["tv:1", "vela:1"].concat(Object.keys(CAT))) (CAT[k] || []).forEach((r) => out.push(Object.assign({ tag: r.src === "vela" ? "Vela" : undefined, author: r.src === "vela" ? "Vela" : undefined }, r)));
  return out;
}
const fmtK = (n) => !n ? "" : n >= 1e6 ? (n / 1e6).toFixed(1).replace(/\.0$/, "") + "M" : n >= 1e3 ? (n / 1e3).toFixed(1).replace(/\.0$/, "") + "K" : String(n);

// ---- right side: chips + list ----
const isFav = (r) => FAVS.some((f) => f.id === r.id);
function toggleFav(r) { if (isFav(r)) FAVS = FAVS.filter((f) => f.id !== r.id); else FAVS.push({ src: r.src, id: r.id, type: r.type, name: r.name, sub: r.sub, open: r.open, kind: r.kind, likes: r.likes }); keep("chartHubFavs", FAVS); renderIndDialog(); }

async function renderIndList() {
  const D = $("indDlg"); if (!D || D.hidden) return;
  const chips = D.querySelector(".dlg-chips"), L = D.querySelector(".dlg-list"), sec = dlg.search ? "search" : dlg.sec;
  const chipSecs = ["tv", "editors", "top", "trending"];
  chips.innerHTML = "";
  if (chipSecs.includes(sec)) [["1", "Indicators"], ["2", "Strategies"]].forEach(([c, n]) => {
    const b = document.createElement("button"); b.textContent = n; b.className = dlg.chip === c ? "on" : "";
    b.onclick = () => { dlg.chip = c; renderIndList(); }; chips.appendChild(b);
  });
  if (sec === "chart") return renderOnChart(L);
  let rows;
  if (sec === "search") {
    const link = /tradingview\.com\/script\//i.test(dlg.search.q), words = dlg.search.q.toLowerCase().split(/\s+/).filter(Boolean), seen = new Set();
    const hit = (r) => words.every((w) => (r.name + " " + (r.sub || "") + " " + (r.author || "")).toLowerCase().includes(w));
    rows = [];
    if (!link) for (const r of localPool()) if (!seen.has(r.id) && hit(r)) { seen.add(r.id); rows.push(r); }
    for (const r of (dlg.search.rows || []).slice().sort((a, b) => (b.likes || 0) - (a.likes || 0))) if (!seen.has(r.id)) { seen.add(r.id); rows.push(r); }
  }
  else if (sec === "fav") rows = FAVS;
  else if (sec === "mine") rows = MINE.map((m) => ({ src: "mine", id: "mine:" + m.id, mineId: m.id, name: m.name, sub: "your Pine" + (m.pine ? " v" + m.pine : ""), open: true, kind: "indicator" }));
  else { L.innerHTML = '<div class="dlg-empty">Loading...</div>'; rows = await loadCat(sec, sec === "vela" ? "1" : dlg.chip); if ((dlg.search ? "search" : dlg.sec) !== sec) return; }
  if (rows.err) { L.innerHTML = `<div class="dlg-empty">${esc(rows.err)}</div>`; return; }
  if (sec === "tv") rows = rows.filter((r) => (dlg.chip === "2") === (r.kind === "strategy"));
  if (dlg.q && sec !== "search") { const q = dlg.q.toLowerCase(); rows = rows.filter((r) => (r.name + " " + (r.sub || "")).toLowerCase().includes(q)); }
  L.innerHTML = `<div class="dlg-colh"><span>SCRIPT NAME</span><span>AUTHOR</span><span>BOOSTS</span></div>`;
  if (sec === "search" && !dlg.search.rows) L.insertAdjacentHTML("beforeend", `<div class="dlg-empty">Searching TradingView too...</div>`);
  else if (sec === "search" && dlg.search.err) L.insertAdjacentHTML("beforeend", `<div class="dlg-empty">TradingView search: ${esc(dlg.search.err)}</div>`);
  if (!rows.length && !(sec === "search" && !dlg.search.rows)) L.insertAdjacentHTML("beforeend", `<div class="dlg-empty">${sec === "fav" ? "No favourites yet. Star any script to keep it here." : sec === "mine" ? "No scripts yet. Write or import one in the Pine Editor." : dlg.q ? `Nothing here matches "${esc(dlg.q)}".` : "Nothing in this list."}</div>`);
  rows.forEach((r) => {
    const row = document.createElement("div"); row.className = "dlg-row" + (r.open === false ? " locked" : ""); const msg = dlg.rowMsg[r.id];
    row.innerHTML = `<button class="star${isFav(r) ? " on" : ""}" title="Favourite"><svg width="14" height="14" viewBox="0 0 24 24"><path d="M12 3l2.7 5.6 6.1.9-4.4 4.3 1 6.1L12 17l-5.4 2.9 1-6.1L3.2 9.5l6.1-.9z"/></svg></button>
      <div class="nm">${esc(r.name)}${sec === "search" && r.tag ? `<span class="badge">${esc(r.tag)}</span>` : ""}${r.beta ? '<span class="badge">beta</span>' : ""}${r.open === false ? `<span class="badge">${r.src === "vela" ? "not for this symbol" : "protected"}</span>` : ""}</div>
      <div class="sub" title="${esc(r.sub || "")}">${esc(r.author || r.sub || "")}</div><div class="cnt" title="${r.likes ? Number(r.likes).toLocaleString() + " boosts" : ""}">${fmtK(r.likes)}</div>
      ${msg ? `<div class="msg ${msg.bad ? "bad" : ""}">${esc(msg.text)}</div>` : ""}`;
    row.querySelector(".star").onclick = (e) => { e.stopPropagation(); toggleFav(r); };
    row.onclick = () => addRow(r);
    L.appendChild(row);
  });
  if (["editors", "top", "trending", "tv", "search"].includes(sec)) L.insertAdjacentHTML("beforeend", '<div class="dlg-empty">Protected and invite-only scripts keep their code on TradingView, so they cannot run here.' + (sec !== "search" ? " Type in the search box to search everything at once." : " Not found? Paste the script's TradingView link (tradingview.com/script/...).") + '</div>');
}

// Click a row: add one instance (clicking again adds another).
async function addRow(r) {
  const flash = (text, bad) => { dlg.rowMsg[r.id] = { text, bad }; renderIndList(); };
  if (r.open === false) return flash(r.src === "vela" ? "This Vela indicator needs data this symbol does not have." : "Protected script: TradingView keeps its code private, so it cannot run here.", true);
  if (r.src !== "vela" && r.kind === "library") return flash("Libraries are building blocks for other scripts, not indicators.", true);
  if (r.src === "vela" && st.engine !== "vela") return flash("Switch the engine to Vela to use Vela native indicators.", true);
  if (r.src === "vela") {  // single-instance natives (Volume, VPVR): say so instead of a fake "Added"
    const live = ((await ask("native:list")).list || []).find((n) => n.type === r.type);
    if (live && live.present && !live.multi) return flash("Already on the chart (one per chart). Use its gear on the chart to change it.", true);
  }
  const stamp = Date.now().toString(36);
  let ind;
  if (r.src === "vela") ind = { key: `native:${r.type}:${stamp}`, kind: "native", type: r.type, name: r.name, origin: "Vela", on: true };
  else if (r.src === "lib") { const x = (window.TX_PINE_LIB || []).find((y) => y.id === r.libId); if (!x) return; ind = { key: `lib:${x.id}:${stamp}`, libId: x.id, name: x.name, origin: "TradeX library", source: x.source, on: true }; }
  else if (r.src === "mine") { const m = MINE.find((x) => x.id === r.mineId); if (!m) return; ind = { key: `own:${m.id}:${stamp}`, mineId: m.id, name: m.name, origin: "own", source: m.source, on: true }; }
  else if (r.src === "tv") {
    flash("Fetching the code from TradingView...");
    try {
      const j = await (await fetch("/api/tv/source?id=" + encodeURIComponent(r.id))).json(); if (j.error) throw new Error(j.protected ? "Protected script: TradingView keeps its code private, so it cannot run here." : j.error);
      ind = { key: `tv:${r.id}:${stamp}`, tvId: r.id, name: r.name, author: r.sub, origin: r.builtin ? "TradingView built-in" : "TradingView", source: j.source, pine: j.pine, license: j.license, on: true };
    } catch (e) { return flash(String(e.message || e), true); }
  }
  else return flash("This script is not available here.", true);
  ind.rowId = r.id; INDS.push(ind); saveInds(); flash("Adding to the chart...");
  if (st.engine !== "vela") flash("Pine draws on the Vela engine: switching to Vela...");
  pineAdd(ind);  // on another engine this switches to Vela, which then draws every indicator that is on
}
// Results land here (via indChanged) and show on the row that added them.
function rowResult(ind) {
  if (!ind.rowId) return;
  if (busy[ind.key]) return;
  const conv = ind.convFrom ? ` (converted from Pine v${ind.convFrom})` : "";
  dlg.rowMsg[ind.rowId] = ind.err ? { text: "Could not draw: " + ind.err, bad: true } : ind.wait ? { text: "On, but it draws on the Vela engine only: switch the engine to Vela to see it." } : liveIds[ind.key] ? (ind.pending ? { text: "Placed on the chart, but it has not drawn yet: waiting for chart data (is the feed up?)", bad: true } : { text: "Added to the chart" + conv }) : dlg.rowMsg[ind.rowId];
}

function renderOnChart(L) {
  L.innerHTML = `<div class="dlg-colh">ON THIS CHART</div>`;
  if (!INDS.length) { L.insertAdjacentHTML("beforeend", '<div class="dlg-empty">Nothing on the chart yet. Pick one from Vela native, or write your own in the Pine Editor.</div>'); return; }
  INDS.forEach((ind) => {
    const row = document.createElement("div"); row.className = "dlg-row chart";
    const status = busy[ind.key] ? "loading..." : ind.on && liveIds[ind.key] ? (ind.pending ? "waiting for chart data" : "on chart") : ind.on ? (st.engine === "vela" ? "waiting for the chart" : "needs the Vela engine") : "hidden";
    const conv = ind.convFrom ? ` · converted from Pine v${ind.convFrom}` : ind.pine ? ` · Pine v${ind.pine}` : "";
    row.innerHTML = `<input type="checkbox" ${ind.on ? "checked" : ""} title="Show on chart">
      <div class="nm">${esc(ind.name)}</div><div class="sub">${esc(ind.origin)}${conv} · ${esc(status)}${ind.license ? " · " + esc(ind.license) : ""}</div>
      ${ind.err ? `<div class="msg bad">${esc(ind.err)}</div>` : ""}${ind.notes && ind.notes.length ? `<div class="msg">Converter: ${esc(ind.notes.join("; "))}</div>` : ""}
      <div class="acts">${ind.source ? '<button class="btn" data-a="edit">Edit a copy</button>' : ""}<button class="btn" data-a="rm">Remove</button></div>`;
    row.querySelector("input").onclick = (e) => { e.stopPropagation(); toggleInd(ind, e.target.checked); };
    row.querySelector('[data-a="rm"]').onclick = (e) => { e.stopPropagation(); removeInd(ind); };
    const ed = row.querySelector('[data-a="edit"]'); if (ed) ed.onclick = (e) => { e.stopPropagation(); closeIndDlg(); editorOpenSource(ind.name + " (copy)", ind.source); };
    L.appendChild(row);
  });
  L.insertAdjacentHTML("beforeend", '<div class="dlg-empty">Settings (inputs, colours, visibility): click the indicator name on the chart legend, then the gear.</div>');
}

// + Compare (2026-11-24, TradingView's "Compare or add symbol"): draws another symbol on the current chart as a line,
// rebased so it starts at the chart's own price on the first candle both have (shows which one moved more).
// It is a small built-in Pine overlay (request.security), so it lives in the indicator list like any indicator:
// remove it there or from the chart legend; layouts save it. Pine draws on Vela; on another engine it switches to Vela.
const CMP_COLORS = ["#2962ff", "#e040fb", "#00bcd4", "#ff9800", "#4caf50", "#f44336"];
function compareSource(key, ticker, color) {
  return `//@version=5
indicator("Compare ${key}", overlay=true)
other = request.security("lab:${ticker}", timeframe.period, close)
var float ratio = na
if na(ratio) and not na(other) and other != 0
    ratio := close / other
plot(ratio * other, "${key}", color=color.new(${color}, 0), linewidth=2)`;
}
window.addCompare = function (pick) {
  if (!pick || !pick.ticker) return;
  if (pick.ticker === st.ticker) return;  // the chart's own symbol
  const n = INDS.filter((x) => x.origin === "compare").length;
  const color = CMP_COLORS[n % CMP_COLORS.length];
  const ind = { key: `cmp:${pick.ticker}:${Date.now().toString(36)}`, name: `Compare ${pick.key}`, origin: "compare",
                source: compareSource(pick.key, pick.ticker, color), on: true };
  INDS.push(ind); saveInds(); pineAdd(ind);
  return ind.key;
};
// Agents / console: TradeXCompare("ETHUSD") compares with that instrument at the chart's broker (or its first broker).
window.TradeXCompare = function (key) {
  const i = (DATA.instruments || []).find((x) => x.key === String(key).toUpperCase()); if (!i) return null;
  const b = i.brokers[st.broker] ? st.broker : Object.keys(i.brokers)[0];
  return window.addCompare({ key: i.key, broker: b, ticker: i.brokers[b][0].ticker });
};
// The + button itself is wired in symbol_search.js (loaded after the top bar).
