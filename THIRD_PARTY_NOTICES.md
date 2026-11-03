# Third-party notices

This project is AGPL-3.0-only. It bundles or ships the following open-source components, each under its own licence.

| Component | Version | Licence | Where | Project |
|---|---|---|---|---|
| Vela (`@luxalgo/vela`) | 0.9.3 | Apache-2.0 + NOTICE (attribution) | `hub/vela.bundle.js` | https://github.com/luxalgo/vela |
| Vela-pinets (`@luxalgo/vela-pinets`) | 0.2.18 | AGPL-3.0-only | `hub/vela.bundle.js` | https://github.com/LuxAlgo/Vela-pinets |
| PineTS (`pinets`) | 0.12.1 | AGPL-3.0-only | `hub/vela.bundle.js` | https://github.com/LuxAlgo/PineTS |
| Lightweight Charts | 5.2.1 | Apache-2.0 | `hub/vendor/` | https://github.com/tradingview/lightweight-charts |
| lightweight-charts-drawing | 0.6.2 | MIT | `hub/vendor/` | https://github.com/deepentropy/lightweight-charts-drawing |
| KLineChart | 9.8.12 | Apache-2.0 | `hub/vendor/` | https://github.com/klinecharts/KLineChart |
| KLineChart Pro (`@klinecharts/pro`) | 0.1.1 | Apache-2.0 | `hub/vendor/` | https://github.com/klinecharts/pro |

Licence texts: [LICENSE](LICENSE) (AGPL-3.0), [licenses/Apache-2.0.txt](licenses/Apache-2.0.txt),
[licenses/MIT-lightweight-charts-drawing.txt](licenses/MIT-lightweight-charts-drawing.txt).
Notice files: [licenses/NOTICE-vela.txt](licenses/NOTICE-vela.txt), [licenses/NOTICE-klinecharts.txt](licenses/NOTICE-klinecharts.txt).

## Attribution requirements you must keep

- **Vela:** every screen that shows a Vela chart must show a visible Vela attribution. The library draws its
  logomark on the chart by default. Do not hide it unless you show "Vela" with a link to https://velacharts.dev
  on the same screen.
- **Lightweight Charts:** *TradingView Lightweight Charts, Copyright (c) 2019 TradingView, Inc.,
  https://www.tradingview.com*. A link to https://www.tradingview.com must be available to users. The chart's
  `attributionLogo` option (on by default, and left on here) satisfies this.
- **KLineChart:** *KLineChart, Copyright (c) 2019 lihu.*

## Rebuilding the bundled files

`hub/vela.bundle.js` and `hub/vendor/` are built from the exact versions pinned in `build/package-lock.json`:

```bash
cd build
npm ci
npm run build     # -> ../hub/vela.bundle.js (byte-identical to the shipped file with these versions)
npm run vendor    # -> ../hub/vendor/*
```
