// Copies the backup chart engines from node_modules into hub/vendor (unmodified npm files).
// Run after `npm ci`:  node copy_vendor.js
const fs = require("fs"), path = require("path");
const out = path.join(__dirname, "..", "hub", "vendor");
const files = {
  "lightweight-charts/dist/lightweight-charts.standalone.production.js": "lightweight-charts.standalone.production.js",
  "lightweight-charts-drawing/dist/lightweight-charts-drawing.umd.cjs": "lightweight-charts-drawing.umd.js",
  "klinecharts/dist/umd/klinecharts.min.js": "klinecharts.min.js",
  "@klinecharts/pro/dist/klinecharts-pro.umd.js": "klinecharts-pro.umd.js",
  "@klinecharts/pro/dist/klinecharts-pro.css": "klinecharts-pro.css",
};
fs.mkdirSync(out, { recursive: true });
for (const [from, to] of Object.entries(files)) {
  fs.copyFileSync(path.join(__dirname, "node_modules", from), path.join(out, to));
  console.log("copied", to);
}
