# Security

## Design
- The hub listens on `127.0.0.1` only (HTTP 8770, WebSocket 8765). The bridge listens on `127.0.0.1:8797` only.
  Remote access is through SSH tunnels, never open ports.
- The web server serves only a whitelist of static files (`.html`, `.js`, `.css` and images, in `hub/` and
  `hub/vendor/`). `.env`, logs, caches, tests and Python files all answer 404.
- POST requests are accepted only as same-origin JSON, so another website open in your browser cannot use the hub.
- Log lines are redacted (tokens, passwords, keys, long hex and base64 strings) before they are shown or saved.
- The bridge only reads candles and symbols. Nothing in this project places, changes or closes orders.
- Secrets (MT5 MCP token, SSH keys) come from environment variables or `.env`, which git ignores.

## Do not
- Do not put the hub on a public address or behind a public reverse proxy. It has no login.
- Do not commit `.env`, MT5 tokens or SSH keys.

## Reporting a problem
Please open a GitHub issue without exploit details and ask for a private contact, or use GitHub's
"Report a vulnerability" (private advisory) on this repository.
