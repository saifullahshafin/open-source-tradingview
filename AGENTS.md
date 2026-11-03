# AGENTS.md: setup guide for AI coding agents

This file is for AI agents (Claude Code, Codex, Cursor, Copilot and similar) that set up this project for a user.
Follow the steps in order, run each check, and report what passed and what did not. Do not skip the checks.

## Rules
- **Never commit or print secrets.** `.env`, MT5 passwords, MCP tokens and SSH keys stay on the user's machine.
  `.env` is already in `.gitignore`.
- **Read-only market data.** Nothing in this project places orders. Do not add order placement unless the user
  asks for it explicitly.
- **Local only.** The hub binds to `127.0.0.1`. Do not expose ports 8770, 8765 or 8797 to the internet. Use an SSH
  tunnel for remote MT5.
- Ask the user before installing MetaTrader 5, changing their SSH config, or doing anything on their server.

## 1. Free feeds (always do this first)
Needs Python 3 (tested on 3.14).

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate      macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
python hub/server.py
```

Expected output, within about 15 s:
```
Chart Hub: open http://localhost:8770/hub.html  (data on ws://localhost:8765)
Bybit: <about 3300> charts loaded
Deriv: <about 40> symbols loaded
```

Checks (in a second terminal):
```bash
curl -s http://127.0.0.1:8770/api/status
# -> {"deriv": {"ok": true, ...}, "bybit": {"ok": true, ...}, "binance": {"ok": true, ...}}
curl -s "http://127.0.0.1:8770/api/bars?source=deriv&symbol=frxXAUUSD&tf=M1&limit=3"
# -> a JSON list of 3 candles {time, open, high, low, close, volume}
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8770/.env
# -> 404 (secrets are never served)
```
Then tell the user to open http://localhost:8770/hub.html.

Known limits: Bybit blocks some countries and many cloud or US data-centre IPs, so its light goes grey there.
Deriv and Binance still work. Ports can be changed with `HUB_HTTP_PORT` and `HUB_WS_PORT` in `.env`.

## 2. Local MT5 (MetaTrader 5 on the same Windows PC)
Windows only, because the `MetaTrader5` Python package is Windows only.
1. The user installs MetaTrader 5 from their broker and logs in. A demo account is fine.
2. `pip install MetaTrader5`
3. Copy `.env.example` to `.env` and set `LOCAL_MT5=1`. If MT5 is not in the default folder, also set
   `MT5_TERMINAL_PATH`.
4. Restart `python hub/server.py`. Check that `/api/status` contains `"mt5": {"ok": true, "msg": "connected"}` and
   that a "Local MT5" button appears in the hub.

## Remote MT5 on a VPS
Use this when MT5 runs on another machine (a Windows VPS, or Linux with MT5 under Wine).

**On the server (the machine running MT5):**
1. MT5 must be logged in to the user's account.
2. Enable MT5's built-in MCP server: Tools > Options > MCP (recent MT5 builds). Note the port (default 22346)
   and copy the token. The token is a secret.
3. Copy `bridge/mt5_bridge.py` to the server (it needs only Python 3, standard library). Start it:
   ```bash
   MT5_MCP_TOKEN='<token>' python3 mt5_bridge.py
   # optional: BRIDGE_PORT=8797  MT5_MCP_URL=http://127.0.0.1:22346/mcp  BRIDGE_STATE_DIR=<folder>
   ```
   Check on the server: `curl -s http://127.0.0.1:8797/health`, which should return `{"ok": true, "server": "<broker server>", ...}`.
4. Keep it running with a systemd service (Linux) or a scheduled task (Windows). Keep the token in an environment
   file readable only by that user, never in the repo.

**On the user's PC (where the hub runs):**
1. SSH must work without a password prompt (key login). Add a host alias to `~/.ssh/config`, for example:
   ```
   Host my-mt5-vps
       HostName <server address>
       User <user>
       IdentityFile <path to private key>
   ```
   Check: `ssh -o BatchMode=yes my-mt5-vps echo ok` prints `ok`.
2. In `.env`, set `REMOTE_MT5_SSH_HOST=my-mt5-vps`. If the bridge uses another port, also set
   `REMOTE_MT5_BRIDGE_PORT`.
3. Optional: `REMOTE_MT5_RESTART_CMD`, a command on the server that the Restart chart button may run. It is
   called with one argument, `bridge` or `full`, for example `bash ~/restart_mt5_stack.sh`.
4. Restart the hub. It opens the tunnel itself (`ssh -N -L 8797:127.0.0.1:8797 my-mt5-vps`) and reconnects
   5 s after any drop. Check that `/api/status` contains `"arm": {"ok": true, ...}`. ("arm" is the internal id
   of Remote MT5.) A "Remote MT5" button appears, and its tickers start with `RMT_`.

Troubleshooting: if the light stays grey, check `hub/logs/hub.log` or the log panel (terminal icon in the right
rail). If you see "another program holds the SSH link ports", another ssh process is already using port 8797.
Stop it.

## Developing
- Main files: `hub/server.py` (feeds, instrument map, HTTP and WebSocket), `hub/hub.html` (the shell), `hub/vela.html`,
  `hub/lightweight.html`, `hub/klinechart.html` (engines), `hub/ind_*.js`, `hub/pine_*.js` (indicators and Pine).
- New feed: add a `<name>_history()` and a `stream_<name>()` in `server.py`, register them in `handler()` and
  `/api/bars`, add the symbols to `/api/symbols` and `build_instruments()`, and add one entry to `BROKERS`.
- Rebuilding `hub/vela.bundle.js`: see THIRD_PARTY_NOTICES.md. The project is AGPL-3.0. Keep the licence and
  notices, and keep the Vela attribution mark visible.
