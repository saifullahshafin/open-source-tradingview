// Chart Hub: Backend log terminal. One merged, live, UTC view of the whole trading backend (logbus.py, /api/logs).
// Loaded after hub.html's inline script; plugs into PANELS and the rail by itself.
(() => {
  const css = document.createElement("style");
  css.textContent = `
  #dock.wide.logwide { width:min(860px, 62vw) }
  .lg { display:flex; flex-direction:column; height:100%; min-height:0; font:12px/1.45 var(--mono) }
  .lg-ban { padding:7px 12px; border-bottom:1px solid var(--line); cursor:pointer; color:var(--ok) }
  .lg-ban.bad { color:var(--bad); background:color-mix(in srgb, var(--bad) 12%, transparent) }
  .lg-ban div { white-space:nowrap; overflow:hidden; text-overflow:ellipsis }
  .lg-bar { display:flex; gap:6px; padding:6px 10px; border-bottom:1px solid var(--line); flex-wrap:wrap; align-items:center }
  .lg-bar input[type=search] { flex:1; min-width:120px; background:var(--bg); color:var(--text); border:1px solid var(--line); border-radius:3px; padding:3px 6px; font:inherit }
  .lg-bar input[type=search]:focus { outline:none; border-color:var(--accent) }
  .lg-bar select { font:inherit; padding:2px 4px }
  .lg-bar label { color:var(--dim); display:flex; gap:4px; align-items:center; cursor:pointer }
  .lg-bar label input { accent-color:var(--accent); margin:0 }
  .lg-bar .btn { padding:2px 8px; font:inherit } .lg-bar .btn.on { color:var(--accentInk); background:var(--accent); border-color:var(--accent) }
  .lg-src { display:flex; flex-wrap:wrap; gap:3px; padding:5px 10px; border-bottom:1px solid var(--line) }
  .lg-src button { background:transparent; color:var(--dim); border:1px solid var(--line); border-radius:3px; padding:1px 6px; font:inherit; font-size:11px; cursor:pointer }
  .lg-src button.off { color:var(--faint); text-decoration:line-through }
  .lg-src button.silent { border-color:var(--bad); color:var(--bad) }
  .lg-src button.unknown { border-color:var(--accent); color:var(--accent) }
  .lg-body { flex:1; min-height:0; overflow:auto; background:var(--bg); padding:4px 0 }
  .lg-l { display:flex; gap:8px; padding:0 10px; white-space:pre-wrap; word-break:break-word }
  .lg-l time { color:var(--faint); flex:none } .lg-l b { font-weight:400; color:var(--dim); flex:none; width:88px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap }
  .lg-l.debug span { color:var(--faint) } .lg-l.warn span { color:var(--accent) }
  .lg-l.error { background:color-mix(in srgb, var(--bad) 13%, transparent) } .lg-l.error span, .lg-l.error b { color:var(--bad) }
  .lg-foot { padding:3px 10px; border-top:1px solid var(--line); color:var(--dim); font-size:11px; display:flex; justify-content:space-between; gap:10px }
  #logBadge { cursor:pointer; white-space:nowrap } #logBadge.bad { color:var(--bad) }`;
  document.head.appendChild(css);

  const MAX_KEEP = 8000, MAX_SHOW = 1500, LV = { debug: 0, info: 1, warn: 2, error: 3 };
  let lines = [], lastId = 0, boot = null, info = null, down = false, dirty = true;
  const v = { level: 1, q: "", paused: false, off: new Set(), raw: false, held: 0 };
  let body = null, ban = null, srcs = null, foot = null;

  // ---------- rail button + status bar count ----------
  const rail = document.getElementById("rail"), b = document.createElement("button");
  b.dataset.panel = "logs"; b.title = "Backend log: everything the backend is doing, live";
  b.innerHTML = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"><rect x="3" y="4" width="18" height="16" rx="2"/><path d="M7 9l3 3-3 3M12.5 15H17"/></svg>';
  b.onclick = () => openPanelByName(openPanel === "logs" ? null : "logs");
  rail.insertBefore(b, rail.querySelector('[data-panel="settings"]'));
  PANELS.logs = render;
  const baseOpen = openPanelByName;
  window.openPanelByName = (name) => { baseOpen(name); if (name !== "logs") { document.getElementById("dock").classList.remove("logwide"); body = null; } };

  const badge = document.createElement("span");
  badge.id = "logBadge"; badge.title = "Open the backend log"; badge.textContent = "log ...";
  badge.onclick = () => { if (openPanel !== "logs") openPanelByName("logs"); };
  document.getElementById("status").appendChild(badge);

  function paintBadge() {
    if (down) { badge.className = "bad"; badge.textContent = "log: server not answering"; return; }
    if (!info) return;
    const p = Object.keys(info.problems).length, e = info.errors_15m;
    badge.className = p || e ? "bad" : "";
    badge.textContent = p || e ? `log: ${p ? p + " problem" + (p > 1 ? "s" : "") : ""}${p && e ? ", " : ""}${e ? e + " error" + (e > 1 ? "s" : "") + " 15m" : ""}` : "log: ok";
  }

  // ---------- data ----------
  const clock = (t) => new Date(t).toISOString().slice(5, 23).replace("T", " ");
  const esc = (s) => s.replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));

  async function tick() {
    const open = openPanel === "logs";
    try {
      const r = await (await fetch(open ? `/api/logs?since=${lastId}` : "/api/logs?brief=1", { cache: "no-store" })).json();
      down = false; info = r;
      if (boot !== null && boot !== r.boot) { lines = []; lastId = 0; dirty = true; boot = r.boot; if (open) return; }
      boot = r.boot;
      if (open && r.lines) {
        if (r.lines.length) { lines.push(...r.lines); lastId = r.last; dirty = true; if (v.paused) v.held += r.lines.length; }
        if (lines.length > MAX_KEEP) lines = lines.slice(-MAX_KEEP);
      }
    } catch (_) { down = true; }
    paintBadge();
    if (open && body) paint();
  }

  // ---------- panel ----------
  function render() {
    const d = document.getElementById("dock");
    d.classList.add("wide", "logwide");
    d.innerHTML = `<div class="lg"><div class="lg-ban"></div>
      <div class="lg-bar"><input type="search" placeholder="Search text, Enter not needed" value="${esc(v.q).replace(/"/g, "&quot;")}">
        <select title="Lowest level shown"><option value="0">All, with debug</option><option value="1">Info and up</option><option value="2">Warnings and errors</option><option value="3">Errors only</option></select>
        <button class="btn" data-a="pause">Pause</button><button class="btn" data-a="all">All sources</button>
        <label title="Also show the repetitive MT5 chart-read lines for 5 minutes. Off by default: they are about 3 lines a second."><input type="checkbox">chart reads</label></div>
      <div class="lg-src"></div><div class="lg-body"></div><div class="lg-foot"><span></span><span></span></div></div>`;
    const q = d.querySelector("input[type=search]"), sel = d.querySelector("select"), raw = d.querySelector("input[type=checkbox]");
    body = d.querySelector(".lg-body"); ban = d.querySelector(".lg-ban"); srcs = d.querySelector(".lg-src"); foot = d.querySelector(".lg-foot");
    sel.value = v.level; raw.checked = v.raw;
    q.oninput = () => { v.q = q.value.toLowerCase(); dirty = true; paint(true); };
    sel.onchange = () => { v.level = +sel.value; dirty = true; paint(true); };
    raw.onchange = () => { v.raw = raw.checked; if (raw.checked && v.level > 0) { v.level = 0; sel.value = 0; }
      fetch("/api/logs/journal", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ raw: raw.checked }) }); dirty = true; };
    const pause = d.querySelector('[data-a="pause"]');
    pause.onclick = () => { v.paused = !v.paused; v.held = 0; pause.classList.toggle("on", v.paused); pause.textContent = v.paused ? "Paused" : "Pause"; dirty = true; paint(true); };
    pause.classList.toggle("on", v.paused); pause.textContent = v.paused ? "Paused" : "Pause";
    d.querySelector('[data-a="all"]').onclick = () => { v.off.clear(); dirty = true; paint(true); };
    ban.onclick = () => { v.level = 3; sel.value = 3; v.off.clear(); dirty = true; paint(true); };
    dirty = true; tick();
  }

  function paintBanner() {
    if (down) { ban.className = "lg-ban bad"; ban.innerHTML = "<div>Chart server is not answering: this view is frozen at its last lines.</div>"; return; }
    if (!info) return;
    const p = Object.entries(info.problems), e = info.errors_15m, row = [];
    p.slice(0, 4).forEach(([s, m]) => row.push(`<div>PROBLEM ${esc(s)}: ${esc(m)}</div>`));
    if (p.length > 4) row.push(`<div>and ${p.length - 4} more</div>`);
    if (e) row.push(`<div>${e} error${e > 1 ? "s" : ""} in the last 15 min. Latest ${clock(info.latest_error.t)} ${esc(info.latest_error.src)}: ${esc(info.latest_error.msg.slice(0, 160))}</div>`);
    ban.className = "lg-ban" + (row.length ? " bad" : "");
    ban.title = row.length ? "Click to show errors only" : "";
    ban.innerHTML = row.length ? row.join("") : "<div>No problems. No errors in the last 15 min.</div>";
  }

  function paintSources() {
    if (!info) return;
    const now = info.now, sig = [];
    const names = Object.keys(info.sources).sort();
    srcs.innerHTML = "";
    names.forEach((n) => { const s = info.sources[n], x = document.createElement("button");
      const age = s.last ? Math.round((now - s.last) / 1000) : null;
      const prob = Object.keys(info.problems).some((k) => k === n || k.startsWith(n + ":"));
      x.textContent = n;
      x.className = (v.off.has(n) ? "off " : "") + (s.state === "reader down" ? "unknown" : prob || s.state === "silent" ? "silent" : "");
      x.title = `${n}: last line ${age === null ? "never" : age + " s ago"}${s.expect ? `, expected every ${s.expect} s` : ", writes only on events"}${s.state === "reader down" ? ". ARM log reader is down, state unknown" : ""}`;
      x.onclick = (ev) => { if (ev.shiftKey || ev.ctrlKey) { v.off = new Set(names.filter((k) => k !== n)); } else if (v.off.has(n)) v.off.delete(n); else v.off.add(n); dirty = true; paint(true); };
      srcs.appendChild(x); sig.push(n); });
  }

  function paint(force) {
    if (!body) return;
    paintBanner(); paintSources();
    foot.children[1].textContent = `${new Date(info ? info.now : Date.now()).toISOString().slice(11, 19)} UTC`;
    if (v.paused && !force) { foot.children[0].textContent = `Paused. ${v.held} new lines waiting.`; return; }
    if (!dirty) return;
    dirty = false;
    const q = v.q, rows = lines.filter((r) => LV[r.lvl] >= v.level && !v.off.has(r.src) && (!q || (r.msg + " " + r.src).toLowerCase().includes(q)));
    rows.sort((a, b) => a.t - b.t || a.id - b.id);
    const show = rows.slice(-MAX_SHOW);
    const atEnd = body.scrollHeight - body.scrollTop - body.clientHeight < 30;
    const keep = body.scrollTop;
    body.innerHTML = show.map((r) => `<div class="lg-l ${r.lvl}"><time>${clock(r.t)}</time><b title="${esc(r.src)}">${esc(r.src)}</b><span>${esc(r.msg)}</span></div>`).join("");
    if (atEnd || force) body.scrollTop = body.scrollHeight; else body.scrollTop = keep;
    foot.children[0].textContent = `${show.length} shown of ${lines.length} loaded. Times are UTC. Shift-click a source for only that one.`;
  }

  // Open panel: live tail. Closed: only the count, so the problem is visible anywhere in the hub.
  setInterval(() => { if (openPanel === "logs") tick(); }, 1500);
  setInterval(() => { if (openPanel !== "logs") tick(); }, 5000);
  tick();
})();
