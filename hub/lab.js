// Shared by every chart page: same feed, same lag meter, so the comparison is fair.
// Inside the TradeX wrapper (?embed=1) the shell owns symbol, broker, timeframe, layout, theme and the status bar;
// the page shows only its chart and its own drawing tools.
window.LabEmbedded = new URLSearchParams(location.search).get("embed") === "1" && window.parent !== window;
// TradingView habit: start typing a letter on the chart and the hub's Symbol Search opens with it.
// Not while typing in a field (also inside shadow roots), not with Ctrl/Alt/Meta, not while a Vela dialog is open.
// Window + capture phase: Vela stops key events on the chart canvas before they bubble to document.
if (window.LabEmbedded) window.addEventListener("keydown", (e) => {
  if (e.ctrlKey || e.altKey || e.metaKey || e.isComposing || !/^[a-zA-Z]$/.test(e.key)) return;
  let a = document.activeElement; while (a && a.shadowRoot && a.shadowRoot.activeElement) a = a.shadowRoot.activeElement;
  if (a && (/INPUT|TEXTAREA|SELECT/.test(a.tagName) || a.isContentEditable)) return;
  if ([...document.querySelectorAll("dialog[open], [role=dialog]")].some((x) => x.checkVisibility ? x.checkVisibility() : x.offsetParent)) return;
  e.preventDefault(); window.parent.postMessage({ evt: "symsearch", key: e.key }, location.origin);
}, true);
if (LabEmbedded) {
  document.documentElement.classList.add("embed");
  const st = document.createElement("style");
  st.textContent = [
    "html.embed #top { display: none !important }",                 // Vela / KLineChart title bars
    "html.embed #top + #chart { top: 0 !important; bottom: 0 !important }",
    "html.embed #title, html.embed #sym, html.embed #tfs, html.embed #tfs + .sep, html.embed #auto { display: none !important }", // Lightweight: shell owns these
    "html.embed body { background: var(--bg) !important; color: var(--text) !important; font-family: var(--font) !important }",
    "html.embed #bar { background: var(--panel) !important; border-bottom: 1px solid var(--line) !important; padding: 4px 8px !important }",
    "html.embed #bar button, html.embed #bar select { background: var(--bg) !important; color: var(--text) !important; border: 1px solid var(--line) !important; border-radius: 3px }",
    "html.embed #bar button.on { border-color: var(--accent) !important; color: var(--accent) !important }",
    "html.embed #bar .sep { background: var(--line) !important }",
    "html.embed #bar input[type=checkbox] { accent-color: var(--accent) }",
  ].join(" ");
  document.head.appendChild(st);
}
window.LabFeed = (() => {
  // Leave a 20 px bar at the bottom for the status strip, so it never covers the chart (standalone pages only).
  const chartBox = document.getElementById("chart");
  if (chartBox && !LabEmbedded) chartBox.style.bottom = "20px";
  // 2026-11-24: the data port comes from the server (/api/symbols ws_port), so a second copy on other ports
  // (the merged / installed TradeX) never talks to this one.
  const wsUrl = () => `ws://${location.hostname || "localhost"}:${window.LabWSPort || 8765}`;
  const stats = { updates: 0, delays: [], exch: [], srcLag: [], lastAt: 0, firstBarAt: 0, errors: [], status: "" };
  window.labStats = stats;

  async function history(symbol, tf, toMs, limit = 1000, source) {
    source = source || window.srcOf(symbol);
    const u = `/api/bars?symbol=${encodeURIComponent(symbol)}&tf=${tf}&limit=${limit}&source=${source}` + (toMs ? `&to=${Math.floor(toMs)}` : "");
    const say = (extra) => { if (window.LabEmbedded) setTimeout(() => window.parent.postMessage({ evt: "loaded", symbol, tf, ...extra }, location.origin), 120); };
    try {
      const r = await fetch(u);
      const j = await r.json();
      if (!Array.isArray(j)) throw new Error(j.error || "history failed");
      say({ bars: j.length });  // TradeX lifts its "Loading" cover only after the new ticker's candles arrived
      return j; // time in epoch seconds
    } catch (e) { say({ error: String(e.message || e) }); throw e; }
  }

  // Live candle updates. onBar(bar) gets {time(sec),open,high,low,close,volume}.
  function live(symbol, tf, onBar, source) {
    source = source || window.srcOf(symbol);
    const ws = new WebSocket(wsUrl());
    ws.onopen = () => ws.send(JSON.stringify({ source, symbol, tf }));
    ws.onmessage = (e) => {
      const m = JSON.parse(e.data);
      if (m.type === "error") { stats.errors.push(m.msg); hud(`Feed error: ${m.msg}`); return; }
      if (m.type === "status") { stats.status = m.msg; return; }
      if (m.lag_ms != null) { stats.srcLag.push(m.lag_ms); if (stats.srcLag.length > 500) stats.srcLag.shift(); }
      if (m.type !== "bar") return;
      stats.status = "";  // data flows again: drop any old reconnect message
      const now = Date.now();
      stats.updates++; stats.lastAt = now;
      if (!stats.firstBarAt) stats.firstBarAt = now;
      stats.delays.push(now - m.sent);
      if (m.trade_ms) stats.exch.push(now - m.trade_ms);
      if (stats.delays.length > 500) { stats.delays.shift(); stats.exch.shift(); }
      onBar(m.bar);
    };
    ws.onerror = () => hud("Feed offline: start server.py");
    return () => { try { ws.close(); } catch (_) {} };
  }

  const med = (a) => { if (!a.length) return 0; const s = [...a].sort((x, y) => x - y); return s[Math.floor(s.length / 2)]; };
  let el;
  function hud(text) {
    if (LabEmbedded) { if (text) window.parent.postMessage({ evt: "hud", text }, location.origin); return; }
    if (!el) {
      el = document.createElement("div");
      el.id = "lab-hud";
      // One-line status strip in its own 20 px bar under the chart (the chart is shrunk to make room).
      el.style.cssText = "position:fixed;left:0;right:0;bottom:0;height:20px;box-sizing:border-box;z-index:99999;font:11px/19px Consolas,monospace;background:#0b1117;color:#7d8c99;padding:0 10px;border-top:1px solid #26333f;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;pointer-events:none";
      document.body.appendChild(el);
    }
    if (text) el.textContent = text;
  }
  let lastCount = 0;
  setInterval(() => {
    const ups = stats.updates - lastCount; lastCount = stats.updates;
    const age = stats.lastAt ? Date.now() - stats.lastAt : -1;
    hud(`updates/s ${ups}   |   last update ${age < 0 ? "-" : age + " ms ago"}   |   source->server ${stats.srcLag.length ? Math.round(med(stats.srcLag)) + " ms" : "-"}   |   server->screen ${Math.round(med(stats.delays))} ms` + (stats.status ? `   |   ${stats.status}` : ""));
  }, 1000);

  return { history, live, stats };
})();

// Markets available without a login (Binance public feed). Forex and broker gold arrive with MT5.
// The list comes from the server: every forex pair and commodity at your MT5 broker (when logged in),
// plus crypto from Binance. Loaded synchronously so every page can build its menus at once.
window.LabSymbols = (() => {
  try {
    const x = new XMLHttpRequest(); x.open("GET", "/api/symbols", false); x.send();
    const j = JSON.parse(x.responseText); window.LabMT5Status = j.mt5; window.LabARMStatus = j.arm; if (j.ws_port) window.LabWSPort = j.ws_port;
    if (j.arm !== "ok" && j.arm !== "off") console.warn("Remote MT5 unavailable:", j.arm);   // "off" = not set up: fine
    if (j.mt5 !== "ok" && j.mt5 !== "off") console.warn("Local MT5 unavailable:", j.mt5);
    return j.symbols;
  } catch (e) {
    return [{ ticker: "BTCUSD", name: "Bitcoin", source: "binance", group: "crypto" }];
  }
})();
window.srcOf = (t) => { const s = LabSymbols.find((x) => x.ticker === t); return s ? s.source : "binance"; };
window.LabDefaultSymbol = (LabSymbols.find((s) => s.source === "arm" && /_XAUUSD$/.test(s.ticker)) || LabSymbols.find((s) => s.ticker === "frxXAUUSD") || LabSymbols.find((s) => /^XAUUSD/.test(s.ticker) && s.source === "mt5") || LabSymbols.find((s) => s.ticker === "BTCUSD")).ticker;

// TradeX: the shell opens a page as hub-frame with ?symbol=&tf=&embed=1, sends {cmd:"set"} to switch,
// and the page reports user changes back with LabNotify (only when embedded).
window.LabParams = new URLSearchParams(location.search);
if (LabParams.get("symbol") && LabSymbols.some((s) => s.ticker === LabParams.get("symbol"))) window.LabDefaultSymbol = LabParams.get("symbol");
window.LabDefaultTF = ["M1", "M5", "M15", "M30", "H1", "H4", "D1"].includes(LabParams.get("tf")) ? LabParams.get("tf") : "M1";
window.LabNotify = (symbol, tf) => { if (LabEmbedded) window.parent.postMessage({ evt: "market", symbol, tf }, location.origin); };
window.addEventListener("message", (e) => {
  if (e.origin !== location.origin || !e.data || !window.labChart) return;
  if (e.data.cmd === "set" && labChart.setMarket) labChart.setMarket(e.data.symbol, e.data.tf);
  if (e.data.cmd === "layout" && labChart.setLayout) labChart.setLayout(e.data.layout);
  // Indicators dialog: Vela natives catalog / add, and the live list of what is on the chart.
  const answer = (r) => window.parent.postMessage({ evt: "reply", rid: e.data.rid, ...r }, location.origin);
  if (e.data.cmd === "pine:add") {
    const reply = (r) => {
      if (e.data.rid != null) answer({ key: e.data.key, ...r });
      else window.parent.postMessage({ evt: "pine", key: e.data.key, ...r }, location.origin);
    };
    if (!labChart.addPine) reply({ ok: false, error: "Pine indicators run on Vela for now (the other engines get them in step 4)." });
    else labChart.addPine(e.data.source, e.data.inputs, e.data.props, e.data.cell, e.data.cellId).then(reply);
  }
  if (e.data.cmd === "pine:remove" && labChart.removePine) labChart.removePine(e.data.id);
  if (e.data.cmd === "native:list") { if (!labChart.natives) answer({ ok: false, list: [] }); else labChart.natives().then((list) => answer({ ok: true, list }), (err) => answer({ ok: false, list: [], error: String(err) })); }
  if (e.data.cmd === "native:add") answer(labChart.addNative ? labChart.addNative(e.data.type, e.data.inputs, e.data.cell, e.data.cellId) : { ok: false, error: "Vela only" });
  if (e.data.cmd === "cell:active") answer(labChart.activeCell ? labChart.activeCell() : { ok: true, index: 0, id: "", count: 1 });
  if (e.data.cmd === "cells:count") answer(labChart.cellsCount ? labChart.cellsCount() : { ok: true, count: 1 });
  if (e.data.cmd === "chart:list") answer({ ok: true, list: labChart.onChart ? labChart.onChart() : [] });
  // TradeX layouts (layouts.js): read / restore the chart's layout document (Vela only).
  if (e.data.cmd === "doc:get") { try { answer(labChart.getDoc ? { ok: true, doc: labChart.getDoc() } : { ok: false, error: "layouts need the Vela engine" }); } catch (err) { answer({ ok: false, error: String(err) }); } }
  if (e.data.cmd === "drawings:now") answer({ ok: true, list: labChart.drawingsNow ? labChart.drawingsNow() : [] });
  if (e.data.cmd === "doc:apply") answer(labChart.applyDoc ? labChart.applyDoc(e.data.doc) : { ok: false, error: "layouts need the Vela engine" });
  if (e.data.cmd === "theme") { if (window.HubThemeApply) HubThemeApply(); if (labChart.applyTheme) labChart.applyTheme(); }
});

// Timeframe helpers shared by the pages.
window.LabTF = {
  list: ["M1", "M5", "M15", "M30", "H1", "H4", "D1"],
  sec: { M1: 60, M5: 300, M15: 900, M30: 1800, H1: 3600, H4: 14400, D1: 86400 },
  // Turn anything like "1", "60", "240", "1D", "1h", "15m" into our code.
  fromAny(v) {
    const s = String(v).trim();
    const m = s.match(/^(\d*)\s*([smhdwSMHDW]?)$/);
    if (!m) return "M1";
    const n = parseInt(m[1] || "1", 10), u = m[2].toLowerCase();
    const minutes = u === "h" ? n * 60 : u === "d" ? n * 1440 : u === "w" ? n * 10080 : u === "s" ? 1 : n;
    const map = { 1: "M1", 5: "M5", 15: "M15", 30: "M30", 60: "H1", 240: "H4", 1440: "D1" };
    return map[minutes] || "M1";
  },
};
