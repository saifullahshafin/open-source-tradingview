// Chart Hub: convert Pine v1-v4 scripts to v5 and run them in pinets (the same engine as the chart) on real bars.
// Usage (after `npm ci` in build/):
//   node hub/tests/pine_convert_harness.js <bars.json> <scripts_dir> [--all]
// bars.json:   an array of {time, open, high, low, close, volume} (for example the output of /api/bars)
// scripts_dir: your own .pine files (version read from //@version=N, default 1)
// --all:       also run v5/v6 scripts as a baseline
// "Runs" is not "matches TradingView": compare the values yourself before trusting a converted script.
const fs = require("fs"), path = require("path");
const { PineTS } = require(path.join(__dirname, "..", "..", "build", "node_modules", "pinets", "dist", "pinets.min.cjs"));
const { convert } = require("../pine_convert.js");
if (process.argv.length < 4) { console.log("usage: node pine_convert_harness.js <bars.json> <scripts_dir> [--all]"); process.exit(2); }
const bars = JSON.parse(fs.readFileSync(process.argv[2], "utf8")).map((b) => ({ open: b.open, high: b.high, low: b.low, close: b.close, volume: b.volume, openTime: b.time * 1000, closeTime: b.time * 1000 + 59999 }));
const all = process.argv.includes("--all"), dir = process.argv[3], rows = [];
(async () => {
  for (const f of fs.readdirSync(dir).filter((x) => x.endsWith(".pine"))) {
    const source = fs.readFileSync(path.join(dir, f), "utf8");
    const m = source.match(/\/\/\s*@version\s*=\s*(\d+)/), v = m ? Number(m[1]) : 1;
    if (v >= 5 && !all) continue;
    const c = convert(source); let ok = false, why = "", plots = 0, filled = 0;
    try {
      const res = await Promise.race([new PineTS(bars).run(c.source), new Promise((_, no) => setTimeout(() => no(new Error("timeout 20s")), 20000))]);
      const p = res.plots || {}; plots = Object.keys(p).length;
      filled = Object.values(p).filter((x) => (x.data || []).some((d) => d && d.value != null && !Number.isNaN(d.value))).length;
      ok = true;
    } catch (e) { why = String(e && e.message || e).split("\n")[0].slice(0, 110); }
    rows.push({ v, ok, plots, filled, name: f.slice(0, 44), why });
  }
  rows.sort((a, b) => a.v - b.v);
  for (const x of rows) console.log(`v${x.v} ${x.ok ? "RUN " : "FAIL"} plots ${x.plots}/${x.filled} drawn | ${x.name}${x.why ? " | " + x.why : ""}`);
  const g = (f) => { const s = rows.filter(f); return `${s.filter((x) => x.ok).length}/${s.length}`; };
  console.log(`\nv4: ${g((x) => x.v === 4)}   v3: ${g((x) => x.v === 3)}   v1/v2: ${g((x) => x.v <= 2)}` + (all ? `   v5/v6: ${g((x) => x.v >= 5)}` : ""));
  process.exit(0);
})();
