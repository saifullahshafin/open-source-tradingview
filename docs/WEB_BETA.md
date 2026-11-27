# TradeX web beta

Hosted on Cloudflare Pages (free, unlimited static requests). The web version is in private early access.

The same TradeX pages, served as a static site with **no TradeX server**. Each visitor's browser fetches free
exchange data itself (their own IP, so no shared rate limit and nothing to pay).

## How it works
| Desktop app asks the server for | Web beta answers in the browser (`hub/web/`) |
|---|---|
| `/api/bars` history | `feeds_web.js`: Binance, Bybit, Deriv, Coinbase, Kraken, OKX REST straight from the browser |
| live candles over `ws://<host>:8765` | `api_shim.js` swaps that socket for the exchange's own WebSocket (Coinbase, Kraken, OKX: polled every few seconds) |
| `/api/symbols`, `/api/instruments`, Symbol Search | `hub/web/data/*.json`, made by the build on a laptop, searched in the browser with the desktop ranking |
| `/api/status` | which feeds answered this browser |
| `/api/layouts` | this browser's `localStorage` |
| `/api/orderflow/delta`, `profile`, `footprint` | `orderflow_web.js` (port of `hub/orderflow_api.py`, results identical): real taker-buy volume from Binance futures, else Binance spot (works where futures is blocked), else estimated. Footprint uses 1-minute buy/sell totals (no trade-by-trade data on the web) |
| logs, restart, MT5, alerts | not in the web beta (desktop version) |

`api_shim.js` is injected as the first script of each page **in the build copy only**; the desktop pages are never
edited for the web. Theme: the "TradeX brand" preset in `hub/theme.js` is the web default.

Limits: Binance and Bybit refuse visitors from some countries (their light turns grey, the other feeds still work).
OKX charts stop at 1,500 candles and Coinbase at 3,000 (small pages per request); Kraken gives 720.

## Build and deploy
```
python scripts/build_web.py --refresh-lists   # laptop only: re-make hub/web/data/ (runs a clean public server copy)
python scripts/build_web.py                   # copy + inject + personal-data scan -> web_dist/ (fails on any hit)
npx wrangler pages deploy <copy of web_dist without .vercel/ and vercel.json> --project-name <your project>
# addresses: / = sign-in, /hub = chart (Pages drops .html); web_dist/_headers and _redirects are read by Pages
```
Lists are made on a laptop, never on Vercel: Vercel builds run in the US, where Binance.com and Bybit refuse requests.
The repo's `vercel.json` is ready for Vercel to build from GitHub (it only copies and scans, no network); that path is not tested yet.
