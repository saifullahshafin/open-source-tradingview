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

## Get TradeX only from official places
Install only from this repository's releases or the one-line installer in the README. A copy offered anywhere
else under the TradeX name is not official and may be
changed to steal keys or passwords. List of official places: [TRADEMARK.md](TRADEMARK.md).

## Reporting a problem
Use GitHub's private "Report a vulnerability" button on this repository's Security tab. Please do not post
exploit details in a public issue.
