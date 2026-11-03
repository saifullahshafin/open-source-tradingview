"""Remote MT5 bridge: serves candles from a MetaTrader 5 terminal through the terminal's native MCP server.
Plain Python, standard library only. Run it on the machine that runs MT5 (for example a Linux server with MT5
under Wine), then reach it from the hub through an SSH tunnel. It binds to 127.0.0.1 only and never places orders.

Settings (environment variables):
  BRIDGE_PORT        HTTP port on 127.0.0.1 (default 8797)
  MT5_MCP_URL        the terminal's MCP endpoint (default http://127.0.0.1:22346/mcp)
  MT5_MCP_TOKEN      the MCP token from MT5: Tools > Options > MCP (required; never commit it)
  BRIDGE_STATE_DIR   folder for the small server-time offset file (default: this folder)

Every time it returns is UTC.

GET /health                              broker server name, server-time offset (asks the terminal each time, never cached)
GET /symbols                             every symbol: name, desc, path, digits
GET /bars?symbol=&tf=&limit=&to=<ms>     candles, newest last (to = newest bar wanted, UTC ms)
GET /wait?symbol=&tf=&after=<key>        long-poll: returns as soon as the last candle changes (max 20 s)

One shared poller per symbol+timeframe (a few reads a second, stopped when nobody asks), so any number of charts
and alerts adds no load on the terminal, which is also the trading path.
"""
import calendar, datetime as dt, json, os, threading, time, urllib.error, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

PORT = int(os.environ.get("BRIDGE_PORT", "8797"))
MCP_URL = os.environ.get("MT5_MCP_URL", "http://127.0.0.1:22346/mcp")
STATE_DIR = os.environ.get("BRIDGE_STATE_DIR", os.path.dirname(os.path.abspath(__file__)))
OFFSET_FILE = os.path.join(STATE_DIR, ".bridge_offset")
POLL_S = 0.25          # live read per symbol+timeframe
POLLER_IDLE_S = 60     # stop a poller nobody asked for
CALL_TIMEOUT_S = 15    # one terminal call; a stuck call fails instead of freezing the bridge
SYNC_CAP_S = 6         # how long /bars waits for the terminal to sync an idle symbol
TF_SEC = {"M1": 60, "M5": 300, "M15": 900, "M30": 1800, "H1": 3600, "H4": 14400, "D1": 86400}
CATEGORY_PATH = {"majors": "Forex", "minors": "Forex", "exotic": "Forex", "crypto": "Crypto", "metals": "Metals",
                 "indices": "Indices", "energies": "Energies", "stocks": "Stocks"}
STATE = {"offset_s": 0, "offset_src": "default (not yet measured)", "measured_at": 0}


def log(*a):
    print(time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()), *a, flush=True)


class Mcp:
    """One MCP session behind a lock. A dead session (terminal restarted) is reopened once per call."""

    def __init__(self):
        self.sid, self.n, self.lock = None, 0, threading.Lock()

    def _post(self, body):
        key = os.environ.get("MT5_MCP_TOKEN", "").strip()
        if not key:
            raise RuntimeError("MT5_MCP_TOKEN is not set")
        h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream", "Authorization": "Bearer " + key}
        if self.sid:
            h["Mcp-Session-Id"] = self.sid
        resp = urllib.request.urlopen(urllib.request.Request(MCP_URL, json.dumps(body).encode(), h), timeout=CALL_TIMEOUT_S)
        self.sid = resp.headers.get("Mcp-Session-Id") or self.sid
        raw = resp.read().decode()
        for line in raw.splitlines():
            if line.startswith("data:"):
                raw = line[5:].strip()
        return json.loads(raw) if raw.strip() else {}

    def _open(self):
        self.sid = None
        self.n += 1
        self._post({"jsonrpc": "2.0", "id": self.n, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "chart-bridge", "version": "1"}}})
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def _call(self, name, args):
        self.n += 1
        r = self._post({"jsonrpc": "2.0", "id": self.n, "method": "tools/call", "params": {"name": name, "arguments": args}})
        res = r.get("result", {})
        txt = "".join(c.get("text", "") for c in res.get("content", []))
        if res.get("isError"):
            raise ValueError(txt or "terminal error")
        return json.loads(txt)

    def call(self, name, args):
        with self.lock:
            try:
                if not self.sid:
                    self._open()
                return self._call(name, args)
            except (urllib.error.URLError, OSError, json.JSONDecodeError):
                self._open()
                return self._call(name, args)


LIVE, HIST, MISC = Mcp(), Mcp(), Mcp()  # a long history read never delays the live candle


def iso(epoch):
    return dt.datetime.fromtimestamp(epoch, dt.timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")


def parse_time(s):
    return calendar.timegm(dt.datetime.fromisoformat(s[:19]).timetuple())


def measure_offset():
    """Server time minus UTC, rounded to 30 min. Kept unchanged while the market is shut (server time is then stale)."""
    t = MISC.call("get_time_information", {})
    d = parse_time(t["trade_server_last_known_time"]) - parse_time(t["utc_time"])
    h = round(d / 1800) * 1800
    if abs(d - h) >= 90:
        return
    if h != STATE["offset_s"] or not STATE["measured_at"]:
        log(f"server time offset {h / 3600:+.1f} h")
    STATE.update(offset_s=int(h), offset_src="terminal server time", measured_at=int(time.time()))
    with open(OFFSET_FILE, "w") as f:
        f.write(str(int(h)))


def offset_loop():
    while True:
        try:
            measure_offset()
        except Exception as e:
            log("offset check failed:", e)
        time.sleep(300 if STATE["measured_at"] else 15)


def tf_sec(tf):
    if tf not in TF_SEC:
        raise ValueError(f"unknown timeframe {tf}")
    return TF_SEC[tf]


def to_bar(b):
    return {"time": parse_time(b["time"]) - STATE["offset_s"], "open": float(b["open"]), "high": float(b["high"]),
            "low": float(b["low"]), "close": float(b["close"]), "volume": float(b["tick_volume"])}


def read_bars(mcp, sym, tf, newest_utc, span_s):
    # No "limit" here: when it trims the answer the terminal also drops the candle still forming. Trim on our side.
    off = STATE["offset_s"]
    j = mcp.call("get_chart_history", {"symbol": sym, "period": tf,
                                       "datetime_from": iso(newest_utc - span_s + off), "datetime_to": iso(newest_utc + off)})
    return [to_bar(b) for b in j.get("history", [])]


def last_tick(mcp, sym):
    """(bid time in UTC seconds, ms) of the newest tick in the last 3 s, or None if the market is quiet."""
    off = STATE["offset_s"]
    now = time.time()
    j = mcp.call("get_chart_ticks_history", {"symbol": sym, "datetime_from": iso(now - 3 + off), "datetime_to": iso(now + 60 + off)})
    h = j.get("history") or []
    if not h:
        return None
    s = h[-1]["time_ms"]
    return (parse_time(s) - STATE["offset_s"]) * 1000 + int(s[20:23] or 0)


def bars(sym, tf, limit=1000, to_ms=None):
    """Newest `limit` candles. The terminal syncs an idle symbol only when asked, so the first read can be old or
    short: read again until the last candle is near the latest tick and the count has stopped growing."""
    sec, limit = tf_sec(tf), max(1, min(int(limit), 5000))
    newest = int(int(to_ms) / 1000) if to_ms else int(time.time()) + 86400
    span = int(limit * sec * 1.5) + 4 * 86400  # weekends and holidays make bar counts short; widened below if needed
    end, last_n, out = time.time() + SYNC_CAP_S, -1, []
    while True:
        out = read_bars(HIST, sym, tf, newest, span)
        grown = len(out) != last_n
        last_n = len(out)
        if len(out) < limit and span < 30 * 365 * 86400:
            span *= 3
            if time.time() <= end:
                continue
        tick = None if to_ms else last_tick(HIST, sym)
        stale = bool(tick and (not out or tick / 1000 - out[-1]["time"] >= sec))
        if (not stale and (not grown or len(out) >= limit)) or time.time() > end:
            return out[-limit:]
        time.sleep(0.4)


class Poller:
    """Reads one symbol+timeframe a few times a second while someone is waiting on it."""

    def __init__(self, sym, tf):
        self.sym, self.tf, self.sec = sym, tf, tf_sec(tf)
        self.cond = threading.Condition()
        self.key, self.bar, self.tick_ms, self.error, self.ready = "", None, None, None, False
        self.asked = time.time()
        threading.Thread(target=self.run, daemon=True).start()

    def run(self):
        while time.time() - self.asked < POLLER_IDLE_S:
            try:
                now = int(time.time())
                # A short window keeps each read tiny; the first read looks further back for a market that is shut.
                got = read_bars(LIVE, self.sym, self.tf, now + 86400, self.sec * 4 + 86400 if self.bar else max(self.sec * 4, 10 * 86400))
                tick = last_tick(LIVE, self.sym)
                with self.cond:
                    self.error, self.ready = None, True
                    if tick:
                        self.tick_ms = tick
                    if got and key(got[-1]) != self.key:
                        self.bar, self.key = got[-1], key(got[-1])
                        self.cond.notify_all()
            except Exception as e:
                with self.cond:
                    self.error, self.ready = str(e), True
                    self.cond.notify_all()
            time.sleep(POLL_S)
        with POLLERS_LOCK:
            if POLLERS.get((self.sym, self.tf)) is self:
                del POLLERS[(self.sym, self.tf)]

    def wait(self, after):
        self.asked = time.time()
        end = time.time() + 20
        with self.cond:
            while time.time() < end and (not self.ready or self.key == after) and not self.error:
                self.cond.wait(min(1, max(0, end - time.time())))
                self.asked = time.time()
            if self.error:
                raise RuntimeError(self.error)
            return {"bar": self.bar, "key": self.key, "tick_ms": self.tick_ms}


POLLERS, POLLERS_LOCK = {}, threading.Lock()


def poller(sym, tf):
    with POLLERS_LOCK:
        p = POLLERS.get((sym, tf))
        if p is None:
            p = POLLERS[(sym, tf)] = Poller(sym, tf)
        return p


def key(b):
    return "" if not b else f'{b["time"]},{b["close"]},{b["high"]},{b["low"]},{b["volume"]}'


def symbols():
    rows = MISC.call("get_marketwatch_symbols", {"include_hidden": True, "limit": 5000}).get("symbols", [])
    return [{"name": s["symbol"], "desc": s.get("description", ""), "digits": s.get("digits", 2),
             "path": CATEGORY_PATH.get((s.get("category") or "").lower(), "Other")}
            for s in rows if (s.get("category") or "").lower() != "heartbeat"]


def health():
    a = MISC.call("get_trading_account_info", {})["account"]
    if a.get("balance") and not a.get("equity"):  # the terminal answers but is not logged in: its candles are frozen
        raise RuntimeError("MT5 terminal is not logged in")
    return {"ok": True, "server": a.get("server"), "pollers": len(POLLERS), **STATE}


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"  # keep-alive: one tunnel connection per stream
    timeout = 120                  # a dead client cannot hold a thread for ever

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            if u.path == "/health":
                out = health()
            elif u.path == "/symbols":
                out = symbols()
            elif u.path == "/bars":
                out = bars(q["symbol"], q.get("tf", "M1"), q.get("limit", 1000), q.get("to"))
            elif u.path == "/wait":
                out = poller(q["symbol"], q.get("tf", "M1")).wait(q.get("after", ""))
            else:
                return self.send_json({"error": "not found"}, 404)
            self.send_json(out, 200)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            self.send_json({"error": str(e)}, 502)

    def send_json(self, obj, code):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


class Server(ThreadingHTTPServer):
    request_queue_size = 128
    daemon_threads = True


if __name__ == "__main__":
    try:
        STATE.update(offset_s=int(open(OFFSET_FILE).read()), offset_src="last measured")
    except (OSError, ValueError):
        pass
    threading.Thread(target=offset_loop, daemon=True).start()
    log(f"native MT5 bridge on 127.0.0.1:{PORT}")
    Server(("127.0.0.1", PORT), H).serve_forever()
