# TradeX (The Open Source TradingView)

TradeX is a free, self-hosted, TradingView-style charting app that runs on your own computer.
It puts one interface over several free market-data feeds and three chart engines. It runs Pine Script
indicators and can also show candles from your own MetaTrader 5 terminal, on this PC or on a VPS.

> Not affiliated with, endorsed by, or connected to TradingView, Inc. "TradingView" is their trademark.
> This project is an independent open-source tool. It is not financial advice.
> Also not affiliated with [TradeX Chart](https://github.com/tradex-app/TradeX-chart) (the `tradex-chart` library),
> a separate project with a similar name.
>
> **Official TradeX** is only this repository. Forks are welcome under
> their own name and logo: see [TRADEMARK.md](TRADEMARK.md).

![TradeX: gold on the free Deriv feed, Vela engine](docs/screenshots/01_hub_vela_gold.png)

| Four synced charts + favourite drawing tools | Indicators dialog |
|---|---|
| ![4 chart layout](docs/screenshots/02_four_chart_layout.png) | ![Indicators](docs/screenshots/03_indicators_dialog.png) |
| **KLineChart engine** | **Themes, fonts and candle colours** |
| ![KLineChart](docs/screenshots/04_klinechart_engine.png) | ![Settings](docs/screenshots/06_settings_themes.png) |
| **Order flow: session volume profile (real Binance trades)** | **Floating favourite-tools bar** |
| ![Order flow](docs/screenshots/07_order_flow_profile.png) | ![Favourites bar](docs/screenshots/08_favourites_bar.png) |

## What it does

- **Free feeds, no account needed:** Deriv (forex, gold, indices, crypto), Bybit (about 3,300 crypto
  and metal markets) and Binance (crypto). They work out of the box.
- **Your own MetaTrader 5 (optional):**
  - **Local MT5:** a terminal on this Windows PC (any broker).
  - **Remote MT5:** a terminal on a VPS or cloud server, reached through an SSH tunnel and a small bridge
    (`bridge/mt5_bridge.py`). The bridge only reads candles. It never places orders.
- **The broker switch keeps the instrument.** Gold stays gold when you move from Deriv to Bybit to MT5.
  Only exact matches are joined, so gold tokens such as PAXG are kept separate from gold spot.
- **Three chart engines, one switch:** Vela (main engine, with Pine Script, 79 native indicators, 79 drawing
  tools and layouts of up to 8 synced charts), Lightweight Charts with 86 drawing tools, and KLineChart Pro.
- **Deep history:** Vela loads 10,000 candles per chart (more in chart settings > Advanced), and fills any gap
  left by sleep or a reconnect before the next live candle.
- **Favourite drawing tools:** star a tool in the drawing toolbar and it appears on a small floating bar over the
  chart, like TradingView. Drag the bar by its grip, double-click the grip to turn it vertical. Stars and position
  are remembered.
- **Order flow (Vela chart types):** volume footprint, session volume profile and TPO, plus a delta indicator.
  Crypto and gold use real Binance futures trades with the buyer/seller side; other symbols get a labelled
  estimate from the chart's own candles. Deriv candles have no volume, so use Bybit, Binance or MT5 for order flow.
- **Pine Script:** write or import your own scripts in the Pine Editor. Scripts written for v1 to v4 are
  converted to v5 automatically and the original is kept. Converted values are not verified
  against any reference yet, so check them before you rely on them.
- **Backend log panel:** one live, redacted view of every feed: reconnects, lag, silent streams.
- **Safe by default:** binds to `127.0.0.1` only and serves only a whitelist of static files. With nothing
  configured, it never opens an SSH link.

## Architecture

How TradeX fits together: the one-line installer pulls the newest version from this repo, the chart app in your
browser talks only to the TradeX server on your own computer, and the server fetches candles from the exchanges and
(optionally) your MetaTrader 5. Your data, `.env` and `private/` never leave your computer.

![TradeX architecture](docs/architecture/tradex-architecture.png)

Interactive version (zoom, follow a route, light/dark): download
[`docs/architecture/tradex-architecture.html`](docs/architecture/tradex-architecture.html) and open it in a browser.
Source of the diagram: [`docs/architecture/tradex-architecture.json`](docs/architecture/tradex-architecture.json).

## Install in one line

No Python, no admin rights and no account needed. Everything goes into one folder (`D:\TradeX` when drive D: has
room, else your user folder; `~/TradeX` on macOS/Linux).

New to this? Follow the picture guide: **[How to install TradeX (no coding needed)](docs/WALKTHROUGH.md)**.

**Windows** (PowerShell):
```powershell
powershell -ExecutionPolicy Bypass -c "irm https://github.com/saifullahshafin/open-source-tradingview/releases/latest/download/install.ps1 | iex"
```
**macOS / Linux** (Terminal):
```sh
curl -LsSf https://github.com/saifullahshafin/open-source-tradingview/releases/latest/download/install.sh | sh
```
Then start **TradeX** from the Start menu (Windows) or run `~/TradeX/tradex.sh`. Your browser opens the charts.

**Update:** run the same line again. It downloads the newest TradeX and keeps your layouts, settings and data.

| What if... | TradeX does |
|---|---|
| you have no Python | the installer brings its own (via uv), inside the TradeX folder |
| you have never used MetaTrader 5 | choose "Create a free demo account for me": TradeX opens one by itself (nothing to type) |
| you have an MT5 account | choose "I have an MT5 account" and type login number, server and password (best: the read-only investor password; never saved by TradeX) |
| you do not want MetaTrader 5 | choose "Skip"; the free exchanges still work |
| you already have MetaTrader 5 installed | TradeX installs its own portable copy in its folder and leaves yours alone |
| an exchange is blocked in your country | setup tests each exchange and switches on only the ones that answer |
| port 8770 is taken | TradeX says so at start; set `HUB_HTTP_PORT` in `.env` |
| the internet drops halfway | run the line again; it continues from the step that failed |
| you are on macOS or Linux | all free exchanges work; MetaTrader 5 charts are Windows-only for now |

## Quick start (manual, for developers)

Requirements: Python 3 (tested on 3.14). Node.js is not needed to run it; the built bundle is included.

```bash
git clone https://github.com/saifullahshafin/open-source-tradingview.git
cd open-source-tradingview
python -m venv .venv
# Windows: .venv\Scripts\activate      macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # optional: only needed for MT5
python hub/server.py
```

Then open <http://localhost:8770/hub.html>. On Windows you can double-click `start.bat` instead.

## Connect MetaTrader 5 (optional)

**MT5 on this Windows PC:** install MetaTrader 5, log in to your account, then run `pip install MetaTrader5`
and set `LOCAL_MT5=1` in `.env`. A "Local MT5" feed appears.

**MT5 on a VPS or server:** step-by-step instructions are in [AGENTS.md](AGENTS.md#remote-mt5-on-a-vps). In short:
1. On the server: run MT5, enable its MCP server (Tools > Options), then start
   `MT5_MCP_TOKEN=... python3 bridge/mt5_bridge.py`. It listens on `127.0.0.1:8797` only.
2. On your PC: add the server to `~/.ssh/config` (key login) and set `REMOTE_MT5_SSH_HOST=<alias>` in `.env`.
3. Restart the hub. It opens the SSH tunnel itself and reconnects if the link drops. A "Remote MT5" feed appears.

## Let your AI agent set it up

[AGENTS.md](AGENTS.md) is written for coding agents (Claude Code, Codex, Cursor and similar). It gives the exact
install, run and check steps for every setup (free feeds, local MT5, VPS MT5), the expected outputs, and the
security rules. Point your agent at it: *"Read AGENTS.md and set this up for me."*

## Project layout

```
hub/        the app: server.py (data + web server), hub.html (the shell), engine pages, Pine tools
bridge/     mt5_bridge.py: read-only candle bridge for a remote MT5 terminal
build/      source and build script for hub/vela.bundle.js and hub/vendor (npm)
docs/       screenshots
licenses/   third-party licence and notice files
NOTICE      copyright and the two extra AGPL terms (attribution, no trademark rights)
```

## Roadmap

- Price and Pine alerts (built privately, not yet ported to run without a server)
- Accuracy tests: candles against the broker, Pine values against reference values
- Pine Script on the Lightweight and KLineChart engines (today an indicator stays on and draws when you switch
  back to Vela; adding one while on another engine switches to Vela)
- Drawings that carry across engines

## Licence

**AGPL-3.0-only** (see [LICENSE](LICENSE)), because the bundled Pine Script engine
([PineTS](https://github.com/LuxAlgo/PineTS) and [Vela-pinets](https://github.com/LuxAlgo/Vela-pinets)) is AGPL-3.0. If you run a
modified version for other people over a network, you must offer them its source.
Third-party parts keep their own licences. See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
Market data belongs to its providers. Check each feed's terms before any public or commercial use.

Copyright (C) 2026 Saifullah Shafin. [NOTICE](NOTICE) adds two terms allowed by AGPL section 7, for TradeX's own
code only: keep the line "Based on TradeX by Saifullah Shafin" wherever your version shows its legal notices, and
no right to the TradeX name or logo.

## Name and logo

The TradeX X logo and "TradeX"™ as the name of this software are trademarks of Saifullah Shafin. You may fork and
change the code, but a version you share or host needs its own name and logo. Details: [TRADEMARK.md](TRADEMARK.md).

## Contributing

Pull requests are welcome. Read [CONTRIBUTING.md](CONTRIBUTING.md) first: commits are signed off (`git commit -s`)
and contributions are accepted under its terms.
