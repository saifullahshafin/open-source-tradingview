// Pine "Style tab" for the TradeX (2026-11-13).
// TradingView builds a Style tab by itself from every plot(): colour, thickness, show/hide. Vela's settings dialog
// only shows the script's own input.*() calls, so TradingView built-ins (EMA, TEMA, MACD, ...) arrived with fixed
// colours and no way to change them. This rewrite gives every plot()/hline() with a fixed colour real inputs in a
// "Style" group, so Vela's Inputs tab shows them. Non-fixed colours (e.g. close > open ? green : red) are left as they are.
// Pure text rewrite on the converted v5/v6 source; idempotent (marker comment); never throws (returns the source unchanged).
(function () {
  const MARK = "// hub-style-inputs v1";
  const CONST_COLOR = /^(#[0-9a-fA-F]{6}(?:[0-9a-fA-F]{2})?|color\.[a-z_]+|color\.new\(\s*(?:#[0-9a-fA-F]{6}(?:[0-9a-fA-F]{2})?|color\.[a-z_]+)\s*,\s*\d+(?:\.\d+)?\s*\)|color\.rgb\(\s*\d+\s*,\s*\d+\s*,\s*\d+\s*(?:,\s*\d+(?:\.\d+)?\s*)?\))$/;
  // positional order of the arguments we care about
  const POS = { plot: ["series", "title", "color", "linewidth"], hline: ["price", "title", "color", "linestyle", "linewidth"] };
  const DEFAULT_COLOR = { plot: "#2962FF", hline: "#787B86" };

  // index of the ")" closing the "(" at i, skipping strings and nested brackets; -1 if none
  function closeParen(s, i) {
    let depth = 0;
    for (let k = i; k < s.length; k++) {
      const c = s[k];
      if (c === '"' || c === "'") { k++; while (k < s.length && s[k] !== c) { if (s[k] === "\\") k++; k++; } continue; }
      if (c === "/" && s[k + 1] === "/") { while (k < s.length && s[k] !== "\n") k++; continue; }
      if (c === "(" || c === "[") depth++;
      else if (c === ")" || c === "]") { depth--; if (depth === 0) return k; }
    }
    return -1;
  }
  // split top-level arguments
  function splitArgs(s) {
    const out = []; let depth = 0, start = 0;
    for (let k = 0; k < s.length; k++) {
      const c = s[k];
      if (c === '"' || c === "'") { k++; while (k < s.length && s[k] !== c) { if (s[k] === "\\") k++; k++; } continue; }
      if (c === "/" && s[k + 1] === "/") { while (k < s.length && s[k] !== "\n") k++; continue; }
      if ("([{".includes(c)) depth++;
      else if (")]}".includes(c)) depth--;
      else if (c === "," && depth === 0) { out.push(s.slice(start, k)); start = k + 1; }
    }
    if (s.slice(start).trim()) out.push(s.slice(start));
    return out;
  }
  const strLit = (v) => { const m = /^\s*(["'])((?:\\.|(?!\1).)*)\1\s*$/.exec(v || ""); return m ? m[2] : null; };
  const q = (t) => '"' + String(t).replace(/\\/g, "\\\\").replace(/"/g, '\\"') + '"';

  function addStyleInputs(src) {
    try {
      if (!src || src.includes(MARK)) return src;
      const ver = /^\s*\/\/\s*@version\s*=\s*(\d+)/m.exec(src);
      if (!ver || +ver[1] < 5) return src;  // v1-v4 go through PineConvert first
      const lines = src.split("\n");
      // find statement starts at global scope (no indentation) that call plot( / hline( (optionally "x = plot(")
      const re = /^([A-Za-z_]\w*\s*=\s*)?(plot|hline)\s*\(/;
      const text = lines.join("\n");
      let offsets = [], pos = 0;
      for (const l of lines) { offsets.push(pos); pos += l.length + 1; }
      const edits = []; // {start, end, replacement, before}
      let n = 0;
      const used = new Set();
      for (let li = 0; li < lines.length; li++) {
        const m = re.exec(lines[li]);
        if (!m) continue;
        const fn = m[2];
        const open = offsets[li] + lines[li].indexOf("(", (m[1] || "").length);
        const close = closeParen(text, open);
        if (close < 0) continue;
        const args = splitArgs(text.slice(open + 1, close));
        const named = {}, posArgs = [];
        args.forEach((a, idx) => {
          const km = /^\s*([A-Za-z_]\w*)\s*=(?!=)\s*([\s\S]*)$/.exec(a);
          if (km) named[km[1]] = { idx, val: km[2].trim() }; else posArgs.push({ idx, val: a.trim() });
        });
        const get = (name) => named[name] || (posArgs[POS[fn].indexOf(name)] ? posArgs[POS[fn].indexOf(name)] : null);
        const color = get("color"), width = get("linewidth"), title = get("title");
        const colorVal = color ? color.val : DEFAULT_COLOR[fn];
        if (!CONST_COLOR.test(colorVal)) continue;   // conditional / computed colour: leave it alone
        n++;
        let name = strLit(title && title.val) || (fn === "hline" ? "Line " + n : "Plot " + n);
        while (used.has(name)) name += " ";
        used.add(name);
        const id = "__hubS" + n, wv = width && /^\d+$/.test(width.val.trim()) ? +width.val.trim() : 1;
        const decl = [
          `${id}c = input.color(${colorVal}, ${q(name + " colour")}, group="Style")`,
          `${id}w = input.int(${wv}, ${q(name + " thickness")}, minval=${Math.min(1, wv)}, maxval=${Math.max(4, wv)}, group="Style")`,   // range widened to the script's own width (0 or 15 failed)
        ];
        const showable = fn === "plot" && !named.display;
        if (showable) decl.push(`${id}v = input.bool(true, ${q("Show " + name)}, group="Style")`);
        // rebuild the argument list
        const newArgs = args.map((a) => a);
        const setArg = (key, value) => {
          const cur = named[key] || (posArgs[POS[fn].indexOf(key)] && POS[fn].indexOf(key) >= 0 ? posArgs[POS[fn].indexOf(key)] : null);
          if (cur) newArgs[cur.idx] = (named[key] ? ` ${key}=` : " ") + value;
          else newArgs.push(` ${key}=${value}`);
        };
        setArg("color", `${id}c`);
        if (!width || /^\d+$/.test(width.val)) setArg("linewidth", `${id}w`);
        if (showable) newArgs.push(` display=${id}v ? display.all : display.none`);
        edits.push({ start: open + 1, end: close, replacement: newArgs.join(",").replace(/^\s+/, ""), line: li, decl });
      }
      if (!edits.length) return src;
      // one pass from the end: rewrite the arguments, then put the input declarations on the lines just above
      // that statement (global scope). Working backwards keeps every earlier offset valid.
      let out = text;
      for (const e of edits.slice().reverse()) {
        out = out.slice(0, e.start) + e.replacement + out.slice(e.end);
        const at = offsets[e.line];
        out = out.slice(0, at) + e.decl.join("\n") + "\n" + out.slice(at);
      }
      return out + "\n" + MARK + "\n";
    } catch (e) {
      console.warn("pine_style: left the script unchanged", e);
      return src;
    }
  }
  window.PineStyle = { addStyleInputs };
})();
