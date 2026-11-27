// TradeX: saved LAYOUTS and INDICATOR TEMPLATES (TradingView style), 2026-11-23.
// Layout = everything on screen: instrument, broker, timeframe, engine, grid, theme + candle colours, indicators with
// their settings, drawings (per symbol), Vela chart settings. Autosaves into the current layout.
// Indicator template = only an indicator set; applying one ADDS it to the chart.
// Store: server.py /api/layouts (layouts_store.py, user_data/layouts.json). Loaded after hub.html's inline script.
(function () {
  const LS_ID = "chartHubLayoutId", LS_AUTO = "chartHubLayoutAutosave";
  const h = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const lsGet = (k, d) => { try { const v = localStorage.getItem(k); return v == null ? d : v; } catch (_) { return d; } };
  const lsSet = (k, v) => { try { localStorage.setItem(k, v); } catch (_) {} };
  let curId = lsGet(LS_ID, ""), curName = "", auto = lsGet(LS_AUTO, "1") === "1";
  let lastChartDoc = null, saveTimer = null, saving = false, dirtyAgain = false, state = "", list = { layouts: [], templates: [] };
  let bootApplied = false, ready = false, confirmDel = null, editing = null;   // ready: the layout is back on screen (no autosave before, or an empty chart would overwrite it)   // editing: "rename" | "saveas" | "template" | null

  const api = async (path, body) => {
    const r = await fetch("/api/layouts" + path, body ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) } : {});
    const j = await r.json(); if (!r.ok) throw new Error(j.error || r.status); return j;
  };
  const cleanInds = () => INDS.map((x) => { const y = { ...x }; delete y.err; delete y.pending; delete y.wait; delete y.notes; delete y.convFrom; return y; });
  const themeNow = () => { try { return JSON.parse(lsGet("chartHubTheme", "{}")); } catch (_) { return {}; } };
  const summary = () => `${st.key} ${(TFS.find((t) => t[0] === st.tf) || [0, st.tf])[1]} · ${INDS.length} indicator${INDS.length === 1 ? "" : "s"}`;

  async function buildDoc() {
    if (st.engine === "vela") { const r = await ask("doc:get", {}, 6000); if (r.ok) lastChartDoc = r.doc; }
    return { v: 1, hub: { key: st.key, broker: st.broker, ticker: st.ticker, tf: st.tf, engine: st.engine },
             grid: $("layout").value || "", theme: themeNow(), inds: cleanInds(), chart: lastChartDoc };
  }
  async function saveNow(asNew, name) {
    if (saving) { dirtyAgain = true; return; }
    saving = true; setState("Saving...");
    try {
      const doc = await buildDoc();
      const r = await api("/save", { kind: "layouts", id: asNew ? "" : curId, name: name || curName || "My layout", doc, summary: summary() });
      curId = r.id; curName = name || curName || "My layout"; lsSet(LS_ID, curId); setState("Saved");
      refreshList();
    } catch (e) { setState("Not saved: " + e.message); }
    saving = false;
    if (dirtyAgain) { dirtyAgain = false; schedule(); }
  }
  function schedule() {
    if (!auto) { setState("Unsaved changes"); return; }
    clearTimeout(saveTimer);
    // Never save while a chart is loading or before the layout was put back: the empty chart would overwrite it.
    saveTimer = setTimeout(() => { if (!ready || (typeof pending !== "undefined" && pending)) return schedule(); saveNow(false); }, 2500);
  }
  window.layoutsDirty = schedule;
  window.layoutsReady = () => ready;   // alerts_dialog.js: no drawing checks while a layout is being put back     // hub + indicator code call this on any change

  // Engine (re)loaded: put the layout's chart document back BEFORE the hub re-adds the indicators.
  // First load or an explicit layout load = whole document; later reloads (Restart chart, engine switch) = drawings only.
  window.layoutsBeforeReapply = async function () {
    ready = false;
    try {
      if (st.engine !== "vela" || !curId) return;   // finally{} still marks ready
      const full = !bootApplied; bootApplied = true;
      let chart = lastChartDoc, doc;
      if (full || !chart) {
        const d = await api("?kind=layouts&id=" + encodeURIComponent(curId));
        curName = d.name; doc = d.doc || {}; chart = doc.chart;
        if (full && doc.grid) $("layout").value = doc.grid;
        // Full boot after Save + refresh: restore per-cell indicator list from the layout (includes `cell` / `cellId`).
        if (full && Array.isArray(doc.inds)) {
          lsSet("chartHubIndicators", JSON.stringify(doc.inds));
          try { INDS.splice(0, INDS.length, ...doc.inds); } catch (_) {}
        }
      }
      if (!chart) return;
      const r = await ask("doc:apply", { doc: full ? chart : { drawBook: chart.drawBook } }, 8000);
      if (!r.ok) console.warn("layout apply", r.error);
      paintButton();
    } catch (e) { console.warn("layout restore", e); }
    finally { setTimeout(() => { ready = true; }, 1500); }   // let the restore's own change events pass first
  };

  // Load another layout: write its parts where the hub reads them at start, then start fresh.
  async function loadLayout(id) {
    try {
      if (curId && auto) { clearTimeout(saveTimer); await saveNow(false); }
      const d = await api("?kind=layouts&id=" + encodeURIComponent(id)), doc = d.doc || {};
      if (doc.hub) lsSet("chartHub", JSON.stringify(doc.hub));
      if (doc.theme) lsSet("chartHubTheme", JSON.stringify(doc.theme));
      lsSet("chartHubIndicators", JSON.stringify(doc.inds || []));
      lsSet(LS_ID, id); location.reload();
    } catch (e) { setState("Could not load: " + e.message); }
  }
  async function newLayout() {
    if (curId && auto) { clearTimeout(saveTimer); await saveNow(false); }
    lsSet("chartHubIndicators", "[]");
    const r = await api("/save", { kind: "layouts", name: "New layout", doc: { v: 1, hub: { ...st }, grid: "", theme: themeNow(), inds: [], chart: null }, summary: `${st.key} · 0 indicators` });
    lsSet(LS_ID, r.id); location.reload();
  }
  async function delItem(kind, id) {
    await api("/delete", { kind, id });
    if (kind === "layouts" && id === curId) { curId = ""; curName = ""; lsSet(LS_ID, ""); }
    confirmDel = null; await refreshList(); render();
  }
  async function saveTemplate(name) {
    const inds = cleanInds();
    if (!inds.length) { setState("No indicators on the chart to save"); return; }
    await api("/save", { kind: "templates", name, doc: { v: 1, inds }, summary: inds.map((x) => x.name || x.type).join(", ").slice(0, 110) });
    await refreshList(); setState(`Template "${name}" saved`);
  }
  async function applyTemplate(id) {
    const d = await api("?kind=templates&id=" + encodeURIComponent(id)), stamp = Date.now().toString(36);
    (d.doc && d.doc.inds || []).forEach((x, n) => {
      const ind = { ...x, key: String(x.key || "tpl:x:0").split(":").slice(0, -1).join(":") + ":" + stamp + n, on: true };
      INDS.push(ind); pineAdd(ind);
    });
    saveInds(); indChanged(); setState(`Template "${d.name}" added`); schedule();
  }

  // ---------- UI: top-bar button + menu ----------
  const css = document.createElement("style");
  css.textContent = `#lyBtn{display:flex;align-items:center;gap:6px;max-width:190px} #lyBtn .nm{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  #lyBtn .st{font-size:10px;color:var(--faint,var(--dim))}
  #lyMenu{position:fixed;z-index:60;width:320px;max-height:min(560px,80vh);overflow:auto;background:var(--raised);border:1px solid var(--line);border-radius:6px;box-shadow:0 10px 30px rgba(0,0,0,.4);font-size:12px;color:var(--text)}
  #lyMenu .hd{padding:10px 12px 8px;border-bottom:1px solid var(--grid)} #lyMenu .hd b{font-size:13px} #lyMenu .hd .sub{color:var(--dim);margin-top:2px}
  #lyMenu .acts{display:flex;flex-wrap:wrap;gap:6px;padding:8px 12px;border-bottom:1px solid var(--grid)}
  #lyMenu button.a{background:var(--bg);color:var(--text);border:1px solid var(--line);border-radius:4px;padding:4px 9px;font:inherit;cursor:pointer}
  #lyMenu button.a:hover{border-color:var(--accent)} #lyMenu button.p{background:var(--accent);border-color:var(--accent);color:var(--bg)}
  #lyMenu .sec{padding:8px 12px 4px;color:var(--dim);font-size:10.5px;letter-spacing:.06em;text-transform:uppercase;display:flex;justify-content:space-between;align-items:center}
  #lyMenu .row{display:flex;align-items:center;gap:8px;padding:6px 12px;cursor:pointer} #lyMenu .row:hover{background:var(--bg)}
  #lyMenu .row.cur{box-shadow:inset 2px 0 0 var(--accent)} #lyMenu .row .m{flex:1;min-width:0} #lyMenu .row .n{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  #lyMenu .row .d{color:var(--dim);font-size:10.5px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  #lyMenu .x{background:none;border:0;color:var(--dim);cursor:pointer;padding:2px 4px;font:inherit;border-radius:3px} #lyMenu .x:hover{color:var(--bad);background:var(--bg)}
  #lyMenu .x.sure{color:var(--bad)} #lyMenu .ed{display:flex;gap:6px;padding:8px 12px;border-bottom:1px solid var(--grid)}
  #lyMenu .ed input{flex:1;background:var(--bg);color:var(--text);border:1px solid var(--line);border-radius:4px;padding:5px 7px;font:inherit}
  #lyMenu .tg{display:flex;align-items:center;gap:6px;cursor:pointer;color:var(--text);text-transform:none;letter-spacing:0;font-size:11.5px}
  #lyMenu .empty{padding:6px 12px 10px;color:var(--dim)}`;
  document.head.appendChild(css);
  const btnEl = document.createElement("button"); btnEl.id = "lyBtn"; btnEl.title = "Layouts: save and switch whole chart setups; indicator templates";
  btnEl.innerHTML = `<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/></svg><span class="nm"></span><span class="st"></span>`;
  $("layout").insertAdjacentElement("afterend", btnEl);
  const menu = document.createElement("div"); menu.id = "lyMenu"; menu.hidden = true; document.body.appendChild(menu);

  function setState(s) { state = s; paintButton(); if (!menu.hidden) { const e = menu.querySelector(".hd .sub"); if (e) e.textContent = s || "All changes saved"; } }
  function paintButton() { btnEl.querySelector(".nm").textContent = curName || "Layouts"; btnEl.querySelector(".st").textContent = state === "Saving..." ? "saving" : (state.startsWith("Not") ? "!" : ""); }
  async function refreshList() { try { list = await api(""); const c = list.layouts.find((x) => x.id === curId); if (c) curName = c.name; paintButton(); if (!menu.hidden) render(); return true; } catch (e) { setState("Not loaded: " + e.message); return false; } }
  window.layoutsRefresh = refreshList;
  const when = (t) => { const d = new Date(t * 1000); return d.toISOString().slice(0, 16).replace("T", " ") + " UTC"; };

  function render() {
    const rows = (kind, items) => items.map((x) => `<div class="row ${kind === "layouts" && x.id === curId ? "cur" : ""}" data-k="${kind}" data-id="${h(x.id)}">
      <div class="m"><div class="n">${h(x.name)}</div><div class="d">${h(x.summary || "")} · ${when(x.saved_at)}</div></div>
      <button class="x ${confirmDel === kind + x.id ? "sure" : ""}" data-del="1" title="Delete">${confirmDel === kind + x.id ? "Delete?" : "&#x2715;"}</button></div>`).join("");
    const ed = editing ? `<div class="ed"><input id="lyName" maxlength="60" placeholder="${editing === "template" ? "Template name" : "Layout name"}" value="${editing === "rename" ? h(curName) : ""}">
      <button class="a p" id="lyOk">${editing === "rename" ? "Rename" : "Save"}</button><button class="a" id="lyCancel">Cancel</button></div>` : "";
    menu.innerHTML = `<div class="hd"><b>${h(curName || "No layout yet")}</b><div class="sub">${h(state || (curId ? "All changes saved" : "Changes are kept once you save a layout"))}</div></div>
      <div class="acts"><button class="a p" id="lySave">Save</button><button class="a" id="lySaveAs">Save as new</button>${curId ? '<button class="a" id="lyRename">Rename</button>' : ""}<button class="a" id="lyNew">New blank</button></div>
      ${ed}
      <div class="sec"><span>Layouts</span><label class="tg"><input type="checkbox" id="lyAuto" ${auto ? "checked" : ""}> Autosave</label></div>
      ${list.layouts.length ? rows("layouts", list.layouts) : '<div class="empty">No saved layouts yet.</div>'}
      <div class="sec"><span>Indicator templates</span><button class="a" id="lyTpl">Save current indicators</button></div>
      ${list.templates.length ? rows("templates", list.templates) : '<div class="empty">Save the indicators on this chart as a set, then add the set to any chart in one click.</div>'}`;
    const on = (id, fn) => { const e = menu.querySelector("#" + id); if (e) e.onclick = fn; };
    on("lySave", () => (curId ? saveNow(false) : (editing = "saveas", render())));
    on("lySaveAs", () => { editing = "saveas"; render(); });
    on("lyRename", () => { editing = "rename"; render(); });
    on("lyTpl", () => { editing = "template"; render(); });
    on("lyNew", newLayout);
    on("lyCancel", () => { editing = null; render(); });
    const ok = async () => {
      const name = (menu.querySelector("#lyName").value || "").trim(); if (!name) return;
      const mode = editing; editing = null;
      if (mode === "template") await saveTemplate(name); else await saveNow(mode === "saveas", name);
      render();
    };
    on("lyOk", ok);
    const inp = menu.querySelector("#lyName"); if (inp) { inp.focus(); inp.onkeydown = (e) => { if (e.key === "Enter") ok(); if (e.key === "Escape") { editing = null; render(); } }; }
    const a = menu.querySelector("#lyAuto"); if (a) a.onchange = () => { auto = a.checked; lsSet(LS_AUTO, auto ? "1" : "0"); if (auto) schedule(); };
    menu.querySelectorAll(".row").forEach((r) => (r.onclick = async (e) => {
      const kind = r.dataset.k, id = r.dataset.id;
      if (e.target.closest("[data-del]")) { e.stopPropagation(); if (confirmDel === kind + id) await delItem(kind, id); else { confirmDel = kind + id; render(); } return; }
      if (kind === "layouts") { if (id !== curId) loadLayout(id); } else { await applyTemplate(id); render(); }
    }));
  }
  function place() { const b = btnEl.getBoundingClientRect(); menu.style.top = b.bottom + 6 + "px"; menu.style.left = Math.max(8, Math.min(b.left, innerWidth - 328)) + "px"; }
  btnEl.onclick = async (e) => { e.stopPropagation(); if (!menu.hidden) { menu.hidden = true; return; } editing = null; confirmDel = null; await refreshList(); render(); place(); menu.hidden = false; };
  document.addEventListener("mousedown", (e) => { if (!menu.hidden && !menu.contains(e.target) && e.target !== btnEl && !btnEl.contains(e.target)) menu.hidden = true; });
  window.addEventListener("resize", () => { if (!menu.hidden) place(); });
  // Ctrl+S saves the layout (TradingView shortcut) when the Pine editor is not focused.
  document.addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s" && !(document.activeElement && document.activeElement.closest && document.activeElement.closest("#dock"))) {
      e.preventDefault(); if (curId) saveNow(false); else { editing = "saveas"; render(); place(); menu.hidden = false; }
    }
  });
  // Every chart change inside Vela (drawings, settings, symbol) marks the layout dirty.
  window.addEventListener("message", (e) => { if (e.origin === location.origin && e.data && (e.data.evt === "doc:dirty" || e.data.evt === "market")) schedule(); });
  // Start-up: no current layout but the store has some -> open the most recent one (never make a duplicate).
  // Truly empty store (first visit) -> create "My layout" from what is on screen, so drawings and indicators survive a
  // reload out of the box. If the list could not be read, change nothing (no pointer cleared, no layout made).
  const openLatest = () => {
    // one automatic open per tab per minute, so a store that keeps losing the pointer can never cause a reload loop
    let t = 0; try { t = +sessionStorage.getItem("chartHubAutoOpen") || 0; sessionStorage.setItem("chartHubAutoOpen", String(Date.now())); } catch (_) {}
    if (Date.now() - t < 60000) return false;
    loadLayout(list.layouts[0].id); return true;
  };
  (async () => {
    const ok = await refreshList();
    if (!ok) { paintButton(); return; }
    if (curId && !list.layouts.find((x) => x.id === curId)) { curId = ""; lsSet(LS_ID, ""); }
    if (!curId && list.layouts.length && openLatest()) return;
    if (!curId) { bootApplied = true; ready = true; setTimeout(async () => {
      if (curId || !(await refreshList())) return;          // read the list again right before creating
      if (list.layouts.length) { openLatest(); return; }
      saveNow(false, "My layout");
    }, 6000); }
    paintButton();
  })();
})();
