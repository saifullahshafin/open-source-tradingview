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

  // Memory cache (parent-shared) + IndexedDB series store. Switch engines / reload: read local first, then
  // only fetch the tip gap (local last bar → now) and any missing older page. Same-origin IDB is shared by all iframes.
  // On disk: TXB1 columnar binary (time u32 + ohlcv f64), optionally deflate-compressed — much smaller than object JSON.
  const CACHE_TTL_LATEST_MS = 15000, CACHE_TTL_HIST_MS = 5 * 60 * 1000, CACHE_MAX = 48;
  const IDB_NAME = "tradex-bars", IDB_STORE = "series", IDB_VER = 2, SERIES_MAX = 5000, SERIES_KEYS_MAX = 80;
  const TF_SEC = { M1: 60, M5: 300, M15: 900, M30: 1800, H1: 3600, H4: 14400, D1: 86400 };
  const TXB_MAGIC = 0x31584254; // 'TXB1' little-endian
  function barsCache() {
    try {
      if (LabEmbedded && window.parent && window.parent !== window) {
        const p = window.parent;
        if (!p.__LabBarsCache) p.__LabBarsCache = { map: new Map(), inflight: new Map() };
        return p.__LabBarsCache;
      }
    } catch (_) {}
    if (!window.__LabBarsCache) window.__LabBarsCache = { map: new Map(), inflight: new Map() };
    return window.__LabBarsCache;
  }
  function cachePut(cache, key, bars) {
    cache.map.set(key, { at: Date.now(), bars });
    while (cache.map.size > CACHE_MAX) cache.map.delete(cache.map.keys().next().value);
  }
  function copyBars(bars) { return bars.map((b) => ({ ...b })); }
  function mergeBars(a, b) {
    const m = new Map();
    for (const x of a || []) m.set(x.time, x);
    for (const x of b || []) m.set(x.time, x);
    return [...m.values()].sort((p, q) => p.time - q.time);
  }
  function sliceBars(bars, toMs, limit) {
    let out = bars;
    if (toMs) out = out.filter((b) => b.time * 1000 <= toMs);
    return out.slice(-Math.max(1, limit | 0));
  }
  function encodeColumns(bars) {
    const n = bars.length, raw = new ArrayBuffer(n * 44), view = new DataView(raw);
    for (let i = 0; i < n; i++) {
      const b = bars[i], o = i * 44;
      view.setUint32(o, b.time >>> 0, true);
      view.setFloat64(o + 4, +b.open, true);
      view.setFloat64(o + 12, +b.high, true);
      view.setFloat64(o + 20, +b.low, true);
      view.setFloat64(o + 28, +b.close, true);
      view.setFloat64(o + 36, +b.volume || 0, true);
    }
    return new Uint8Array(raw);
  }
  function decodeColumns(u8, count) {
    const view = new DataView(u8.buffer, u8.byteOffset, u8.byteLength), out = new Array(count);
    for (let i = 0; i < count; i++) {
      const o = i * 44;
      out[i] = {
        time: view.getUint32(o, true),
        open: view.getFloat64(o + 4, true),
        high: view.getFloat64(o + 12, true),
        low: view.getFloat64(o + 20, true),
        close: view.getFloat64(o + 28, true),
        volume: view.getFloat64(o + 36, true),
      };
    }
    return out;
  }
  async function deflateBytes(u8) {
    if (typeof CompressionStream === "undefined") return { body: u8, flag: 0 };
    try {
      const stream = new Blob([u8]).stream().pipeThrough(new CompressionStream("deflate"));
      const body = new Uint8Array(await new Response(stream).arrayBuffer());
      return body.byteLength < u8.byteLength ? { body, flag: 1 } : { body: u8, flag: 0 };
    } catch (_) { return { body: u8, flag: 0 }; }
  }
  async function inflateBytes(u8) {
    if (typeof DecompressionStream === "undefined") throw new Error("no DecompressionStream");
    const stream = new Blob([u8]).stream().pipeThrough(new DecompressionStream("deflate"));
    return new Uint8Array(await new Response(stream).arrayBuffer());
  }
  async function encodeTxb(bars) {
    const cols = encodeColumns(bars);
    const { body, flag } = await deflateBytes(cols);
    const out = new Uint8Array(12 + body.byteLength);
    const view = new DataView(out.buffer);
    view.setUint32(0, TXB_MAGIC, true);
    out[4] = flag; out[5] = 1; // ver
    view.setUint16(6, 0, true);
    view.setUint32(8, bars.length, true);
    out.set(body, 12);
    return out.buffer;
  }
  async function decodeTxb(buf) {
    const u8 = buf instanceof Uint8Array ? buf : new Uint8Array(buf);
    if (u8.byteLength < 12) return [];
    const view = new DataView(u8.buffer, u8.byteOffset, u8.byteLength);
    if (view.getUint32(0, true) !== TXB_MAGIC) return [];
    const flag = u8[4], count = view.getUint32(8, true);
    if (!count) return [];
    let body = u8.subarray(12);
    if (flag & 1) body = await inflateBytes(body);
    if (body.byteLength < count * 44) return [];
    return decodeColumns(body, count);
  }
  async function rowToBars(row) {
    if (!row) return [];
    if (row.bin) return decodeTxb(row.bin);                 // TXB1 compact
    if (Array.isArray(row.bars)) return row.bars;           // legacy object rows
    if (row instanceof ArrayBuffer || ArrayBuffer.isView(row)) return decodeTxb(row);
    return [];
  }
  let _idb;
  function openBarsDb() {
    if (_idb) return _idb;
    _idb = new Promise((resolve, reject) => {
      if (!window.indexedDB) return reject(new Error("no indexedDB"));
      const req = indexedDB.open(IDB_NAME, IDB_VER);
      req.onupgradeneeded = () => { if (!req.result.objectStoreNames.contains(IDB_STORE)) req.result.createObjectStore(IDB_STORE); };
      req.onsuccess = () => resolve(req.result);
      req.onerror = () => reject(req.error);
    });
    return _idb;
  }
  async function idbGetSeries(key) {
    try {
      const db = await openBarsDb();
      const row = await new Promise((resolve, reject) => {
        const r = db.transaction(IDB_STORE, "readonly").objectStore(IDB_STORE).get(key);
        r.onsuccess = () => resolve(r.result);
        r.onerror = () => reject(r.error);
      });
      return await rowToBars(row);
    } catch (_) { return []; }
  }
  async function idbPutSeries(key, bars) {
    try {
      const trimmed = bars.length > SERIES_MAX ? bars.slice(-SERIES_MAX) : bars;
      const bin = await encodeTxb(trimmed);
      const db = await openBarsDb();
      await new Promise((resolve, reject) => {
        const tx = db.transaction(IDB_STORE, "readwrite");
        tx.objectStore(IDB_STORE).put({ bin, at: Date.now(), n: trimmed.length }, key);
        tx.oncomplete = () => resolve();
        tx.onerror = () => reject(tx.error);
      });
      // Soft cap how many symbol/tf series we keep (drop oldest by `at`).
      const all = await new Promise((resolve, reject) => {
        const r = db.transaction(IDB_STORE, "readonly").objectStore(IDB_STORE).getAllKeys();
        r.onsuccess = () => resolve(r.result || []);
        r.onerror = () => reject(r.error);
      });
      if (all.length <= SERIES_KEYS_MAX) return;
      const meta = await Promise.all(all.map(async (k) => {
        const row = await new Promise((resolve, reject) => {
          const r = db.transaction(IDB_STORE, "readonly").objectStore(IDB_STORE).get(k);
          r.onsuccess = () => resolve(r.result); r.onerror = () => reject(r.error);
        });
        return { k, at: row?.at || 0 };
      }));
      meta.sort((a, b) => a.at - b.at);
      const drop = meta.slice(0, Math.max(0, meta.length - SERIES_KEYS_MAX));
      if (!drop.length) return;
      await new Promise((resolve, reject) => {
        const tx = db.transaction(IDB_STORE, "readwrite");
        const store = tx.objectStore(IDB_STORE);
        for (const x of drop) store.delete(x.k);
        tx.oncomplete = () => resolve();
        tx.onerror = () => reject(tx.error);
      });
    } catch (_) {}
  }
  async function networkBars(symbol, tf, toMs, limit, source) {
    const u = `/api/bars?symbol=${encodeURIComponent(symbol)}&tf=${tf}&limit=${limit}&source=${source}` + (toMs ? `&to=${Math.floor(toMs)}` : "");
    const r = await fetch(u);
    const j = await r.json();
    if (!Array.isArray(j)) throw new Error(j.error || "history failed");
    return j;
  }
  async function fillGapToNow(series, symbol, tf, source) {
    if (!series.length) return networkBars(symbol, tf, null, 1000, source);
    const sec = TF_SEC[tf] || 60;
    const lastT = series[series.length - 1].time;
    const gap = Math.max(0, Math.ceil((Date.now() / 1000 - lastT) / sec));
    // Small gap: one latest request covers local tip → now (also refreshes the forming candle).
    if (gap <= 1000) {
      return mergeBars(series, await networkBars(symbol, tf, null, Math.min(1000, Math.max(5, gap + 3)), source));
    }
    // Large downtime: live tip first, then page backward until the page overlaps the local tip.
    let out = series, page = await networkBars(symbol, tf, null, 1000, source);
    for (let guard = 0; page.length && guard < 40; guard++) {
      out = mergeBars(out, page);
      if (page[0].time <= lastT + sec * 2) break;
      page = await networkBars(symbol, tf, page[0].time * 1000 - 1, 1000, source);
    }
    return out;
  }
  async function ensureOlder(series, symbol, tf, source, toMs, limit) {
    let out = series, pageTo = toMs, guard = 0;
    while (sliceBars(out, toMs, limit).length < limit && guard++ < 40) {
      const need = limit - sliceBars(out, toMs, limit).length;
      const got = await networkBars(symbol, tf, pageTo, Math.min(1000, Math.max(need, 50)), source);
      if (!got.length) break;
      const older = out.length ? got.filter((b) => b.time < out[0].time) : got;
      out = mergeBars(out, got);
      if (!older.length) break;
      pageTo = out[0].time * 1000 - 1;
    }
    return out;
  }

  async function history(symbol, tf, toMs, limit = 1000, source) {
    source = source || window.srcOf(symbol);
    limit = Math.max(1, limit | 0);
    const toKey = toMs ? Math.floor(toMs) : "";
    const reqKey = `${source}|${symbol}|${tf}|${toKey}|${limit}`;
    const seriesKey = `${source}|${symbol}|${tf}`;
    const cache = barsCache();
    const say = (extra) => { if (window.LabEmbedded) setTimeout(() => window.parent.postMessage({ evt: "loaded", symbol, tf, ...extra }, location.origin), 120); };
    const hit = cache.map.get(reqKey);
    const ttl = toMs ? CACHE_TTL_HIST_MS : CACHE_TTL_LATEST_MS;
    if (hit && Date.now() - hit.at < ttl) {
      say({ bars: hit.bars.length, cached: true });
      return copyBars(hit.bars);
    }
    if (cache.inflight.has(reqKey)) {
      try {
        const bars = await cache.inflight.get(reqKey);
        say({ bars: bars.length, cached: true });
        return copyBars(bars);
      } catch (e) { say({ error: String(e.message || e) }); throw e; }
    }
    const pending = (async () => {
      let series = await idbGetSeries(seriesKey);
      const fromLocal = series.length;
      if (!toMs) {
        series = fromLocal ? await fillGapToNow(series, symbol, tf, source)
          : await networkBars(symbol, tf, null, Math.min(limit, 1000), source);
        if (series.length < limit) series = await ensureOlder(series, symbol, tf, source, series[0] ? series[0].time * 1000 - 1 : null, limit);
      } else {
        const localPage = sliceBars(series, toMs, limit);
        if (localPage.length < limit) series = await ensureOlder(series, symbol, tf, source, toMs, limit);
        // Refresh nothing on pure historical pages when local already covers `limit` bars ending at `to`.
        else if (!fromLocal) series = await networkBars(symbol, tf, toMs, limit, source);
      }
      await idbPutSeries(seriesKey, series);
      const out = sliceBars(series, toMs || null, limit);
      cachePut(cache, reqKey, out);
      return out;
    })();
    cache.inflight.set(reqKey, pending);
    try {
      const j = await pending;
      say({ bars: j.length });  // TradeX lifts its "Loading" cover only after the new ticker's candles arrived
      return copyBars(j);
    } catch (e) { say({ error: String(e.message || e) }); throw e; }
    finally { cache.inflight.delete(reqKey); }
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
  if (e.data.cmd === "pine:add") {
    const reply = (r) => window.parent.postMessage({ evt: "pine", key: e.data.key, ...r }, location.origin);
    if (!labChart.addPine) reply({ ok: false, error: "Pine indicators run on Vela for now (the other engines get them in step 4)." });
    else labChart.addPine(e.data.source, e.data.inputs, e.data.props).then(reply);
  }
  if (e.data.cmd === "pine:remove" && labChart.removePine) labChart.removePine(e.data.id);
  // Indicators dialog: Vela natives catalog / add, and the live list of what is on the chart.
  const answer = (r) => window.parent.postMessage({ evt: "reply", rid: e.data.rid, ...r }, location.origin);
  if (e.data.cmd === "native:list") { if (!labChart.natives) answer({ ok: false, list: [] }); else labChart.natives().then((list) => answer({ ok: true, list }), (err) => answer({ ok: false, list: [], error: String(err) })); }
  if (e.data.cmd === "native:add") answer(labChart.addNative ? labChart.addNative(e.data.type, e.data.inputs) : { ok: false, error: "Vela only" });
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
