// Pine Script v1-v4 -> v5 converter (Chart Hub). Runs in the browser (window.PineConvert) and in node (require).
// Rules follow TradingView's official migration guides ("To Pine Script version 4" and "To version 5").
// Approach: mask strings and comments, rewrite the code text, unmask. Returns {source, from, changed, warnings}.
// A converted script is NOT proven correct until its values are checked against TradingView (Chart Hub step 3).
(function (root) {
  "use strict";

  const TA_FN = "alma atr bb bbw cci cmo cog dmi ema hma kc kcw linreg macd mfi mom rma roc rsi sar sma stoch supertrend swma tsi vwma wma wpr barssince change correlation cross crossover crossunder cum dev falling highest highestbars lowest lowestbars median mode percentile_linear_interpolation percentile_nearest_rank percentrank pivothigh pivotlow range rising stdev valuewhen variance".split(" ");
  const TA_VAR = "accdist iii nvi obv pvi pvt tr vwap wad wvad".split(" ");
  const MATH_FN = "abs acos asin atan avg ceil cos exp floor log log10 max min pow random round round_to_mintick sign sin sqrt sum tan todegrees toradians".split(" ");
  const REQ_FN = "financial quandl security splits dividends earnings".split(" ");
  const TICKER_FN = { heikinashi: "ticker.heikinashi", kagi: "ticker.kagi", linebreak: "ticker.linebreak", pointfigure: "ticker.pointfigure", renko: "ticker.renko", tickerid: "ticker.new" };
  const STR_FN = { tostring: "str.tostring", tonumber: "str.tonumber" };
  const COLORS = "aqua black blue fuchsia gray green lime maroon navy olive orange purple red silver teal white yellow".split(" ");
  const PLOT_STYLES = "line stepline histogram cross area columns circles linebr areabr".split(" ");
  const HLINE_STYLES = "solid dotted dashed".split(" ");
  const INPUT_TYPES = { integer: "int", float: "float", bool: "bool", string: "string", symbol: "symbol", resolution: "timeframe", session: "session", source: "source", color: "color", time: "time" };
  const RESERVED_V5 = "catch class do ellipse in is polygon range return struct text throw try".split(" ");
  const DOW = "sunday monday tuesday wednesday thursday friday saturday".split(" ");

  // ---- masking: strings and comments become placeholders so rewrites never touch them ----
  function mask(src) {
    const store = []; let out = "", i = 0;
    while (i < src.length) {
      const c = src[i];
      if (c === "/" && src[i + 1] === "/") { let j = src.indexOf("\n", i); if (j < 0) j = src.length; store.push(src.slice(i, j)); out += "\u0001" + (store.length - 1) + "\u0002"; i = j; continue; }
      if (c === '"' || c === "'") { let j = i + 1; while (j < src.length && src[j] !== c && src[j] !== "\n") { if (src[j] === "\\") j++; j++; } j = Math.min(j + 1, src.length); store.push(src.slice(i, j)); out += "\u0001" + (store.length - 1) + "\u0002"; i = j; continue; }
      out += c; i++;
    }
    return { code: out, store };
  }
  const unmask = (code, store) => code.replace(/\u0001(\d+)\u0002/g, (_, n) => store[+n]);

  // ---- call helpers on masked code ----
  function matchParen(code, open) { let d = 0; for (let i = open; i < code.length; i++) { const c = code[i]; if (c === "(" || c === "[") d++; else if (c === ")" || c === "]") { d--; if (d === 0) return i; } } return -1; }
  function splitArgs(s) { const out = []; let d = 0, cur = ""; for (const c of s) { if (c === "(" || c === "[") d++; if (c === ")" || c === "]") d--; if (c === "," && d === 0) { out.push(cur); cur = ""; } else cur += c; } if (cur.trim() !== "" || out.length) out.push(cur); return out; }
  const named = (a) => { const m = a.match(/^\s*([A-Za-z_]\w*)\s*=(?!=)\s*([\s\S]*)$/); return m ? { k: m[1], v: m[2].trim() } : null; };
  // Rewrite every call of `name(` (bare name, not a method/field). fn(args[], full) -> replacement text or null to keep.
  function rewriteCalls(code, name, fn) {
    const re = new RegExp("(?<![.\\w])" + name.replace(".", "\\.") + "\\s*\\(", "g"); let m, out = "", last = 0;
    while ((m = re.exec(code))) {
      const open = m.index + m[0].length - 1, close = matchParen(code, open); if (close < 0) break;
      const rep = fn(splitArgs(code.slice(open + 1, close)), code.slice(m.index, close + 1));
      if (rep == null) continue;
      out += code.slice(last, m.index) + rep; last = close + 1; re.lastIndex = close + 1;
    }
    return out + code.slice(last);
  }

  // Names the script declares itself (variables, functions, loop vars): never rename those.
  function declared(code) {
    const s = new Set(), fns = new Set(); let m;
    const fd = /(?:^|\n)[ \t]*([A-Za-z_]\w*)\s*\([^()\n]*\)\s*=>/g; while ((m = fd.exec(code))) fns.add(m[1]);
    s.fns = fns; // own FUNCTIONS block ta./math. prefixing; an own VARIABLE named atr does not
    const re = /(?:^|\n)[ \t]*(?:var\s+|varip\s+)?(?:(?:float|int|bool|string|color|line|label|box|table)\s+)?([A-Za-z_]\w*)\s*(?:=(?!=)|\([^()\n]*\)\s*=>)/g;
    while ((m = re.exec(code))) s.add(m[1]);
    const tup = /\[\s*([\w\s,]+)\]\s*=(?!=)/g; while ((m = tup.exec(code))) m[1].split(",").forEach((x) => s.add(x.trim()));
    const fr = /\bfor\s+([A-Za-z_]\w*)\s*=/g; while ((m = fr.exec(code))) s.add(m[1]);
    // function parameters
    const fp = /([A-Za-z_]\w*)\s*\(([^()\n]*)\)\s*=>/g; while ((m = fp.exec(code))) m[2].split(",").forEach((x) => { const n = x.trim().split(/\s+/).pop(); if (n) s.add(n.replace(/=.*/, "")); });
    return s;
  }
  const bare = (name) => new RegExp("(?<![.\\w#])" + name + "(?![\\w])", "g");

  function v3to4(code, W, own) {
    // na-initialised variables need an explicit type in v4
    code = code.replace(/(^|\n)([ \t]*)([A-Za-z_]\w*)\s*=\s*na\b(?!\s*\()/g, (all, nl, ind, n) => `${nl}${ind}float ${n} = na`);
    COLORS.forEach((c) => { if (!own.has(c)) code = code.replace(new RegExp("(?<![.\\w#])" + c + "(?![\\w(])(?!\\s*=(?!=))", "g"), "color." + c); });
    code = rewriteCalls(code, "color", (a) => `color.new(${a.join(",")})`);
    code = code.replace(/\btype\s*=\s*(integer|float|bool|string|symbol|resolution|session|source)\b/g, "type=input.$1");
    code = code.replace(new RegExp("\\bstyle\\s*=\\s*(" + PLOT_STYLES.join("|") + ")\\b", "g"), "style=plot.style_$1");
    code = code.replace(new RegExp("\\blinestyle\\s*=\\s*(" + HLINE_STYLES.join("|") + ")\\b", "g"), "linestyle=hline.style_$1");
    DOW.forEach((d) => { if (!own.has(d)) code = code.replace(bare(d), "dayofweek." + d); });
    const vars = { period: "timeframe.period", isintraday: "timeframe.isintraday", isdaily: "timeframe.isdaily", isweekly: "timeframe.isweekly", ismonthly: "timeframe.ismonthly", isdwm: "timeframe.isdwm", interval: "timeframe.multiplier", tickerid: "syminfo.tickerid", ticker: "syminfo.ticker", n: "bar_index" };
    for (const [k, v] of Object.entries(vars)) if (!own.has(k)) code = code.replace(new RegExp("(?<![.\\w])" + k + "(?![\\w(])(?!\\s*=(?!=))", "g"), v);
    return code;
  }

  // v1/v2: a top-level variable may read its own past in its declaration (x = nz(x[1]) + 1). v3+ needs declare-then-:=.
  function selfRefs(code, W) {
    // a statement continues on the next lines while its brackets are open
    const lines = code.split("\n"), out = [];
    for (let i = 0; i < lines.length; i++) {
      let stmt = lines[i], j = i;
      const depth = (s) => (s.match(/[([]/g) || []).length - (s.match(/[)\]]/g) || []).length;
      while (depth(stmt) > 0 && j + 1 < lines.length) stmt += "\n" + lines[++j];
      const m = stmt.match(/^([ \t]*)([A-Za-z_]\w*)\s*=(?!=)([\s\S]*)$/);
      if (m && new RegExp("(?<![.\\w])" + m[2] + "\\s*\\[").test(m[3])) {
        W.push(`self-referencing "${m[2]}" split into a declaration and :=`);
        out.push(`${m[1]}float ${m[2]} = na`, `${m[1]}${m[2]} :=${m[3]}`); i = j; continue;
      }
      out.push(lines[i]);
    }
    return out.join("\n");
  }

  function v4to5(code, W, own) {
    code = rewriteCalls(code, "study", (a) => "indicator(" + a.join(",") + ")");
    code = code.replace(/\bresolution_gaps\s*=/g, "timeframe_gaps=").replace(/\bresolution\s*=(?!=)/g, "timeframe=");
    // aliases like `bool_ = input.bool` used as type=bool_: inline the constant, drop the alias line
    code = code.replace(/(^|\n)[ \t]*([A-Za-z_]\w*)\s*=\s*input\.(integer|float|bool|string|symbol|resolution|session|source|color|time)[ \t]*(?=\n|$)/g, (all, nl, alias, t) => {
      code = code; W.push(`type alias ${alias} replaced by input.${t}`);
      (v4to5.aliases = v4to5.aliases || []).push([alias, t]); return nl;
    });
    (v4to5.aliases || []).forEach(([alias, t]) => { code = code.replace(new RegExp("\\btype\\s*=\\s*" + alias + "\\b", "g"), "type=input." + t); });
    v4to5.aliases = [];
    // input(..., type=input.X) -> input.X(...)
    code = rewriteCalls(code, "input", (a) => {
      const i = a.findIndex((x) => /^\s*type\s*=\s*input\.\w+/.test(x)); if (i < 0) return null;
      const t = a[i].match(/input\.(\w+)/)[1], rest = a.filter((_, j) => j !== i);
      return `input.${INPUT_TYPES[t] || t}(${rest.join(",")})`;
    });
    // input(defval, ...) without type= -> typed input.X() from the default value (pinets' generic input() takes no minval/options)
    code = rewriteCalls(code, "input", (a) => {
      const di = a.findIndex((x) => named(x) && named(x).k === "defval"), d = di >= 0 ? named(a[di]).v : (a[0] && !named(a[0]) ? a[0].trim() : "");
      let t = null;
      if (/^-?\d+$/.test(d)) t = "int"; else if (/^-?(\d+\.\d*|\.\d+|\d+e-?\d+)$/i.test(d)) t = "float";
      else if (/^(true|false)$/.test(d)) t = "bool"; else if (/^(open|high|low|close|volume|hl2|hlc3|ohlc4|hlcc4)$/.test(d)) t = "source";
      else if (/^(color\.\w+|#[0-9a-fA-F]{6,8})$/.test(d)) t = "color"; else if (/^\u0001\d+\u0002$/.test(d)) t = "string";
      if (!t) return null;
      // an int default with a fractional minval/maxval/step is really a float input
      if (t === "int" && a.some((x) => { const n = named(x); return n && /^(minval|maxval|step)$/.test(n.k) && /\.\d*[1-9]/.test(n.v); })) t = "float";
      let args = a.slice();
      if (t === "int") args = args.map((x) => { const n = named(x); return n && /^(minval|maxval|step)$/.test(n.k) && /^-?\d+\.0*$/.test(n.v) ? ` ${n.k}=${parseInt(n.v, 10)}` : x; });
      return `input.${t}(${args.join(",")})`;
    });
    code = code.replace(/(?<![.\w])input\.(integer|resolution)(?![\w(])/g, (_, t) => "input." + INPUT_TYPES[t]); // stray constants
    // iff(c, a, b) -> (c ? a : b); repeat for nesting
    // (one line: a ternary split over lines can read as a wrongly indented block)
    const one = (s) => s.trim().replace(/\s*\n\s*/g, " ");
    for (let k = 0; k < 20 && /(?<![.\w])iff\s*\(/.test(code); k++) code = rewriteCalls(code, "iff", (a) => a.length === 3 ? `(${one(a[0])} ? ${one(a[1])} : ${one(a[2])})` : null);
    if (/\?/.test(code)) W.push("iff() became a ternary: a ternary is lazy, check functions that must run on every bar");
    code = rewriteCalls(code, "offset", (a) => a.length === 2 ? `(${a[0].trim()})[${a[1].trim()}]` : null);
    code = code.replace(/\blookahead\s*=\s*true\b/g, "lookahead=barmerge.lookahead_on").replace(/\blookahead\s*=\s*false\b/g, "lookahead=barmerge.lookahead_off");
    code = code.replace(/(strategy\.(?:entry|order)\s*\([^)]*?)\blong\s*=/g, "$1direction=");
    // transp= -> color.new(color, transp). bgcolor / fill also lose their implicit transp 90.
    const POS = { plot: 2, bgcolor: 0, fill: 2, plotshape: 4, plotchar: 5, plotarrow: -1, barcolor: 0, hline: 2 };
    for (const fn of Object.keys(POS)) {
      code = rewriteCalls(code, fn, (a, full) => {
        const ti = a.findIndex((x) => named(x) && named(x).k === "transp");
        let t = ti >= 0 ? named(a[ti]).v : (fn === "bgcolor" || fn === "fill") ? "90" : null;
        if (t == null) return null;
        const args = a.filter((_, j) => j !== ti);
        let ci = args.findIndex((x) => named(x) && named(x).k === "color");
        if (ci < 0 && POS[fn] >= 0 && POS[fn] < args.length && !named(args[POS[fn]])) {
          const val = args[POS[fn]].trim(); if (ti < 0 && !/^(color\.\w+|#[0-9a-fA-F]{6})$/.test(val)) return null;
          args[POS[fn]] = ` color.new(${val}, ${t})`;
        } else if (ci >= 0) {
          const val = named(args[ci]).v; if (ti < 0 && !/^(color\.\w+|#[0-9a-fA-F]{6})$/.test(val)) return null;
          args[ci] = ` color=color.new(${val}, ${t})`;
        } else if (ti >= 0) { args.push(` color=color.new(color.blue, ${t})`); W.push(`${fn}: transp without a colour, used blue`); }
        else return null;
        return `${fn}(${args.join(",").replace(/^\s+/, "")})`;
      });
    }
    const pref = (list, ns) => list.forEach((f) => { if (!own.fns.has(f)) code = code.replace(new RegExp("(?<![.\\w])" + f + "(?=\\s*\\()", "g"), ns + "." + f); });
    pref(TA_FN, "ta"); pref(MATH_FN, "math"); pref(REQ_FN, "request");
    for (const [k, v] of Object.entries(TICKER_FN)) if (!own.fns.has(k)) code = code.replace(new RegExp("(?<![.\\w])" + k + "(?=\\s*\\()", "g"), v);
    for (const [k, v] of Object.entries(STR_FN)) if (!own.fns.has(k)) code = code.replace(new RegExp("(?<![.\\w])" + k + "(?=\\s*\\()", "g"), v);
    TA_VAR.forEach((f) => { if (!own.has(f)) code = code.replace(new RegExp("(?<![.\\w])" + f + "(?![\\w(])(?!\\s*=(?!=))", "g"), "ta." + f); });
    // v5 reserved words used as the script's own names -> add an underscore
    RESERVED_V5.forEach((r) => {
      if (!own.has(r)) return;
      code = code.replace(new RegExp("(?<![.\\w])" + r + "(?![\\w])", "g"), (m, off, s) => {
        const after = s.slice(off + r.length), before = s.slice(Math.max(0, off - 200), off);
        const isNamedArg = /^\s*=(?!=)/.test(after) && /[(,]\s*$/.test(before);
        return isNamedArg ? m : r + "_";
      });
      W.push(`"${r}" is a reserved word in v5, renamed to ${r}_`);
    });
    return code;
  }

  // Engine compatibility (all versions): pinets rejects an empty timeframe ("" = chart timeframe in Pine).
  function compat(code, store, W) {
    return rewriteCalls(code, "request.security", (a) => {
      const i = a.findIndex((x, j) => (named(x) && named(x).k === "timeframe") || (j === 1 && !named(x)));
      if (i < 0) return null;
      const n = named(a[i]), v = (n ? n.v : a[i]).trim(), m = v.match(/^\u0001(\d+)\u0002$/);
      if (!m || !/^(""|'')$/.test(store[+m[1]])) return null;
      const args = a.slice(); args[i] = n ? " timeframe=timeframe.period" : " timeframe.period";
      W.push('security timeframe "" (chart timeframe) written as timeframe.period');
      return "request.security(" + args.join(",") + ")";
    });
  }

  function convert(source) {
    const vm = String(source).match(/\/\/\s*@version\s*=\s*(\d+)/), from = vm ? +vm[1] : 1, W = [];
    if (from >= 5) {
      const mm = mask(String(source)), c2 = compat(mm.code, mm.store, W);
      return c2 === mm.code ? { source, from, changed: false, warnings: [] } : { source: unmask(c2, mm.store), from, changed: true, compatOnly: true, warnings: W };
    }
    let { code, store } = mask(String(source).replace(/\r\n/g, "\n"));
    const own = declared(code);
    if (from <= 2) code = selfRefs(code, W);
    if (from <= 3) code = v3to4(code, W, own);
    code = v4to5(code, W, own);
    code = compat(code, store, W);
    let out = unmask(code, store);
    out = vm ? out.replace(/\/\/\s*@version\s*=\s*\d+/, "//@version=5") : "//@version=5\n" + out;
    if (from <= 2) W.push("converted from Pine v" + from + " (no @version tag means v1): check the result carefully");
    return { source: out, from, changed: true, warnings: [...new Set(W)] };
  }

  const api = { convert, version: "1.0" };
  if (typeof module !== "undefined" && module.exports) module.exports = api; else root.PineConvert = api;
})(typeof self !== "undefined" ? self : this);
