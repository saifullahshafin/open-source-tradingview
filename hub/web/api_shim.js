// TradeX web beta: answers the pages' server calls (/api/..., the live-candle WebSocket) inside the browser, so the
// desktop pages run unchanged as a static site. Loaded first in <head> of every page of the web build only
// (scripts/build_web.py); the desktop app never loads it. Needs web/feeds_web.js before it.
(function () {
  "use strict";
  window.TRADEX_WEB = true;
  window.TX_DEFAULT_PRESET = "tradex";   // brand theme (tradex_brand/CORE) for every new visitor; Settings can still change it
  for (const [rel, size, href] of [["icon", "32x32", "web/brand/icon-32.png"], ["icon", "64x64", "web/brand/icon-64.png"], ["apple-touch-icon", "256x256", "web/brand/icon-256.png"]]) {
    const l = document.createElement("link"); l.rel = rel; l.sizes = size; l.type = "image/png"; l.href = href; document.head.appendChild(l);
  }
  const F = window.TXWebFeeds, NativeWS = window.WebSocket, nativeFetch = window.fetch.bind(window);
  const REPO = "https://github.com/saifullahshafin/open-source-tradingview";
  const WAITLIST = "https://tradexwaitlist.vercel.app";
  const DESKTOP = REPO + "/blob/main/docs/WALKTHROUGH.md";
  const json = (obj, status = 200) => new Response(JSON.stringify(obj), { status, headers: { "Content-Type": "application/json" } });
  const DEPTH = { okx: 1500, coinbase: 3000 };   // candles per chart on paged feeds (Kraken itself stops at 720)
  const DESKTOP_ONLY = { error: "This part needs the free desktop version of TradeX (it runs a small server on your PC)." };

  // ---- market lists: made at build time (hub/web/data/), same shape as the desktop server's answers ----
  let instP = null;
  const instruments = () => instP || (instP = nativeFetch("web/data/instruments.json").then((r) => r.json()));

  // Same ranking as the desktop Symbol Search (server.py search_instruments).
  async function search(q, cat, broker, offset, limit) {
    const D = await instruments(), A = D.aliases || {};
    const words = q.toUpperCase().split(/\s+/).filter(Boolean), joined = words.join(""), rows = [];
    D.instruments.forEach((i, n) => {
      if (cat && i.cat !== cat) return;
      if (broker && !i.brokers[broker]) return;
      let r = 0;
      if (words.length) {
        const k = i.key, nm = i.name.toUpperCase();
        if (k !== A[joined] && !words.every((w) => k.includes(w) || nm.includes(w))) return;
        const w0 = words[0], nw = nm.replace(/[\/-]/g, " ").split(/\s+/);
        r = A[joined] === k ? -1 : k === w0 || k === joined ? 0 : k.startsWith(w0) ? 1 : nw.some((x) => x.startsWith(w0)) ? 2 : k.includes(w0) ? 3 : 4;
      }
      rows.push([r, n, i]);
    });
    rows.sort((a, b) => a[0] - b[0] || a[1] - b[1]);
    return { total: rows.length, offset, results: rows.slice(offset, offset + limit).map((x) => x[2]) };
  }

  // ---- saved layouts + indicator templates: this browser's localStorage (desktop: hub/layouts_store.py) ----
  const LS = "tradexWebLayouts";
  const lsLoad = () => { try { const d = JSON.parse(localStorage.getItem(LS) || "{}"); return { layouts: d.layouts || {}, templates: d.templates || {} }; } catch (_) { return { layouts: {}, templates: {} }; } };
  const lsSave = (d) => { try { localStorage.setItem(LS, JSON.stringify(d)); return true; } catch (_) { return false; } };
  function layouts(path, q, body) {
    const d = lsLoad();
    if (path === "/api/layouts" || path === "/api/layouts/") {
      if (q.get("id")) { const doc = (d[q.get("kind") || "layouts"] || {})[q.get("id")]; return doc ? json(doc) : json({ error: "not found" }, 404); }
      const list = (k) => Object.entries(d[k]).map(([id, v]) => ({ id, name: v.name || "", saved_at: v.saved_at || 0, summary: v.summary || "" })).sort((a, b) => b.saved_at - a.saved_at);
      return json({ layouts: list("layouts"), templates: list("templates") });
    }
    let req = {};
    try { req = JSON.parse(body || "{}"); } catch (_) { return json({ error: "bad JSON" }, 400); }
    const kind = req.kind || "layouts";
    if (!d[kind]) return json({ error: "kind must be layouts or templates" }, 400);
    if (path.endsWith("/delete")) { delete d[kind][String(req.id)]; lsSave(d); return json({ ok: true }); }
    if (path.endsWith("/save")) {
      if (!req.doc || typeof req.doc !== "object") return json({ error: "doc must be an object" }, 400);
      const id = String(req.id || "") || Math.random().toString(36).slice(2, 14);
      d[kind][id] = { name: String(req.name || "").trim().slice(0, 60) || "Untitled", saved_at: Math.floor(Date.now() / 1000), summary: String(req.summary || "").slice(0, 120), doc: req.doc };
      return lsSave(d) ? json({ ok: true, id }) : json({ error: "this browser's storage is full or blocked" }, 413);
    }
    return json({ error: "not found" }, 404);
  }

  // Which store answers: the account (signed in) or this browser (guest). Wait until sign-in has settled first: the
  // hub asks for its layouts at once on load, before the sign-in library is up, and used to get the guest store
  // (wrong list, pointer cleared, a fresh "My layout" made). If a session exists but the account cannot be reached,
  // say so instead of quietly using guest storage.
  const hasSession = () => { try { return Object.keys(localStorage).some((k) => /^sb-.+-auth-token$/.test(k)); } catch (_) { return false; } };
  async function layoutsRoute(p, q, body) {
    const A = window.TXAuth;
    if (A && A.whenReady) await Promise.race([A.whenReady, new Promise((r) => setTimeout(r, 20000))]);
    if (A && A.user) return A.layouts(p, q, body);
    if (A && (!A.ready || A.failed) && hasSession()) return json({ error: "your account is not reachable right now; nothing was changed, try again in a moment" }, 503);
    return layouts(p, q, body);
  }

  function status() {
    F.probeAll();
    const out = {};
    for (const id of Object.keys(F.feeds)) { const h = F.health[id] || {}; out[id] = { ok: !!h.ok, msg: h.ok ? "answering from your browser" : h.msg || "checking ..." }; }
    return out;
  }

  // ---- fetch: every /api/... call is answered here ----
  window.fetch = async function (input, init) {
    const raw = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
    const u = new URL(raw, location.href);
    if (u.origin !== location.origin || !u.pathname.startsWith("/api/")) return nativeFetch(input, init);
    const p = u.pathname, q = u.searchParams, body = init && init.body;
    // TradingView script lists + open-source code: a real route on the site (Cloudflare Pages Function), when it has one.
    if (p.startsWith("/api/tv/")) return nativeFetch(input, init);
    try {
      if (p.startsWith("/api/bars")) {
        const feed = F.feeds[q.get("source") || "binance"];
        if (!feed) return json({ error: `${q.get("source")}: this market needs the desktop version` }, 502);
        const to = q.get("to") ? +q.get("to") : undefined, tf = q.get("tf") || "M1", src = q.get("source") || "binance";
        // Feeds that hand out candles in small pages stop at a fixed depth, so a chart opens in seconds (Vela pages
        // back until it has ~10,000). An empty answer = "no older history" for the chart.
        const depth = DEPTH[src];
        let limit = Math.max(1, +(q.get("limit") || 1000));
        if (depth) {
          const oldest = Date.now() - depth * F.SEC[tf] * 1000;
          if (to && to <= oldest + F.SEC[tf] * 1000 * 50) return json([]);
          limit = Math.min(limit, to ? Math.max(1, Math.floor((to - oldest) / 1000 / F.SEC[tf])) : depth);
        }
        return json(await feed.history(q.get("symbol") || "BTCUSD", tf, limit, to));
      }
      if (p.startsWith("/api/orderflow/") && window.TXWebOrderflow) {
        const kind = p.split("/")[3], fn = window.TXWebOrderflow[kind];
        if (fn) return json(await fn(q));
      }
      if (p.startsWith("/api/instruments/search")) return json(await search(q.get("q") || "", q.get("cat") || "", q.get("broker") || "", Math.max(0, +(q.get("offset") || 0)), Math.min(500, Math.max(1, +(q.get("limit") || 50)))));
      if (p.startsWith("/api/instruments")) return json(await instruments());
      if (p.startsWith("/api/status")) return json(status());
      if (p.startsWith("/api/layouts")) return await layoutsRoute(p, q, body);
      if (p.startsWith("/api/restart")) return json({ running: false, step: "" });
      if (p.startsWith("/api/sources")) return json({ sources: [] });
      if (p.startsWith("/api/symbols")) return nativeFetch("web/data/symbols.json");
      return json(DESKTOP_ONLY, 404);
    } catch (e) { return json({ error: String(e.message || e) }, 502); }
  };

  // ---- the one blocking request (lab.js loads /api/symbols synchronously) ----
  const xOpen = XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open = function (method, url, ...rest) {
    try { const u = new URL(url, location.href); if (u.origin === location.origin && u.pathname.startsWith("/api/symbols")) url = "web/data/symbols.json"; } catch (_) {}
    return xOpen.call(this, method, url, ...rest);
  };

  // ---- live candles: the page opens ws://<this host>:<port>; a stand-in socket runs the browser feed instead ----
  class FeedSocket {
    constructor(url) {
      this.url = url; this.readyState = 0; this.stop = null; this.L = {};
      setTimeout(() => { this.readyState = 1; this._emit("open", {}); }, 0);
    }
    addEventListener(t, f) { (this.L[t] = this.L[t] || []).push(f); }
    removeEventListener(t, f) { this.L[t] = (this.L[t] || []).filter((x) => x !== f); }
    _emit(t, ev) { const h = this["on" + t]; if (h) h.call(this, ev); for (const f of this.L[t] || []) f.call(this, ev); }
    _send(obj) { if (this.readyState === 1) this._emit("message", { data: JSON.stringify(obj) }); }
    send(text) {
      if (this.stop) { this.stop(); this.stop = null; }
      let req; try { req = JSON.parse(text); } catch (_) { return; }
      const feed = F.feeds[req.source || "binance"];
      if (!feed) { this._send({ type: "error", msg: `${req.source}: this market needs the desktop version` }); return; }
      if (!F.SEC[req.tf]) { this._send({ type: "error", msg: `Unknown timeframe ${req.tf}` }); return; }
      this.stop = feed.live(req.symbol, req.tf, (b, srcMs) => {
        const now = Date.now();
        this._send({ type: "bar", bar: b, sent: now, trade_ms: srcMs || undefined, lag_ms: srcMs ? Math.max(0, now - srcMs) : undefined });
      }, (msg) => this._send({ type: "status", msg }));
    }
    close() { if (this.stop) { this.stop(); this.stop = null; } if (this.readyState < 2) { this.readyState = 3; this._emit("close", { code: 1000 }); } }
  }
  const isLocalFeed = (url) => { try { const u = new URL(url, location.href); return u.protocol === "ws:" && (u.hostname === location.hostname || u.hostname === "localhost" || u.hostname === "127.0.0.1"); } catch (_) { return false; } };
  function WebSocketShim(url, protocols) { return isLocalFeed(url) ? new FeedSocket(url) : protocols ? new NativeWS(url, protocols) : new NativeWS(url); }
  WebSocketShim.prototype = NativeWS.prototype;
  Object.assign(WebSocketShim, { CONNECTING: 0, OPEN: 1, CLOSING: 2, CLOSED: 3 });
  window.WebSocket = WebSocketShim;

  // ---- web look: hide desktop-only controls; banner with desktop, waitlist and source links (top page only) ----
  const css = document.createElement("style");
  css.textContent = "#restart,#restartMsg{display:none!important}" +
    "#txweb{position:fixed;top:0;left:0;right:0;height:26px;z-index:100000;display:flex;align-items:center;gap:14px;padding:0 12px;box-sizing:border-box;" +
    "background:#131416;color:#b4bcbf;border-bottom:1px solid #2a2d31;font:12px/26px system-ui,Segoe UI,sans-serif;white-space:nowrap;overflow:hidden}" +
    "#txweb b{color:#e8ecee;font-weight:600}#txweb a{color:#e8ecee;text-decoration:none;border-bottom:1px solid #797e86;line-height:15px}#txweb a:hover{border-color:#e8ecee}" +
    "#txweb a.wl{color:#0a0a0b;background:#e8ecee;border:0;border-radius:3px;padding:0 8px;line-height:18px;font-weight:600}#txweb a.wl:hover{background:#ffffff}" +
    "#txweb .m{overflow:hidden;text-overflow:ellipsis;flex:1;min-width:0}" +
    "#txweb .x{margin-left:auto;background:none;border:0;color:#797e86;cursor:pointer;font:14px system-ui;padding:0 2px}#txweb .x:hover{color:#e8ecee}" +
    "html.txweb-banner #app{top:26px!important}" +
    "#brand .txlogo{width:20px;height:20px;border-radius:5px;vertical-align:-5px;margin-right:7px}" +
    "@media (max-width:640px){#txweb .long{display:none}#txweb{gap:10px}}";
  document.head.appendChild(css);
  const top = window.parent === window;
  if (top) document.addEventListener("DOMContentLoaded", () => {
    let closed = false; try { closed = sessionStorage.getItem("txwebBanner") === "0"; } catch (_) {}
    const brand = document.getElementById("brand");
    if (brand && !brand.querySelector(".txlogo")) { const im = document.createElement("img"); im.className = "txlogo"; im.src = "web/brand/icon-64.png"; im.alt = ""; brand.prepend(im); }
    const stat = document.getElementById("status");   // AGPL: source link always visible, also with the banner closed
    if (stat && !stat.querySelector(".txsrc")) { const a = document.createElement("a"); a.className = "txsrc"; a.href = REPO; a.target = "_blank"; a.rel = "noopener"; a.textContent = "Source (AGPL)"; a.style.cssText = "color:var(--dim);text-decoration:none;margin-left:auto;margin-right:12px;white-space:nowrap"; stat.appendChild(a);
      // Vela licence (THIRD_PARTY_NOTICES.md): the chart corner shows the TradeX logo instead of Vela's mark, so "Vela"
      // with a link to velacharts.dev must be on the same screen. It lives here, next to the source link.
      const v = document.createElement("a"); v.className = "txvela"; v.href = "https://velacharts.dev"; v.target = "_blank"; v.rel = "noopener"; v.textContent = "Charts: Vela";
      v.style.cssText = "color:var(--dim);text-decoration:none;margin-right:12px;white-space:nowrap"; stat.insertBefore(v, a); a.style.marginLeft = "0"; v.style.marginLeft = "auto"; }
    if (closed || !document.getElementById("app")) return;
    const b = document.createElement("div"); b.id = "txweb";
    b.innerHTML = `<span class="m"><b>TradeX web beta</b><span class="long"> &middot; free charts straight from the exchanges, in your browser</span></span>` +
      `<a class="wl" href="${WAITLIST}" target="_blank" rel="noopener">Join the waitlist</a>` +
      `<a href="${DESKTOP}" target="_blank" rel="noopener" title="MT5, alerts, order flow and more">Desktop version<span class="long"> (MT5, alerts)</span></a>` +
      `<a href="${REPO}" target="_blank" rel="noopener" title="Open source (AGPL-3.0)">Source</a>` +
      `<button class="x" title="Hide">&#x2715;</button>`;
    b.querySelector(".x").onclick = () => { b.remove(); document.documentElement.classList.remove("txweb-banner"); try { sessionStorage.setItem("txwebBanner", "0"); } catch (_) {} };
    document.body.appendChild(b); document.documentElement.classList.add("txweb-banner");
  });
  F.probeAll();
})();
