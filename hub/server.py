"""TradeX live data server.

Serves the test pages on http://localhost:8770 and streams candles over
ws://localhost:8765. Every chart connects to the same stream, so a chart
comparison measures the chart, not the feed.

Sources:
  mt5      your broker through the MetaTrader 5 terminal on this PC (LOCAL_MT5=1, needs a logged-in account)
  arm      "Remote MT5": a terminal on your VPS through bridge/mt5_bridge.py and an SSH tunnel
           (REMOTE_MT5_SSH_HOST), tickers <REMOTE_MT5_PREFIX><name> (default RMT_)
  binance, bybit, deriv  public feeds, no login; more sources are plugins in hub/feeds/
Settings come from .env (see .env.example). Personal extras live in the git-ignored private/ folder
(private/tradex_private.py), loaded only when present.

Client -> server: {"source": "mt5"|"binance"|"deriv"|"arm"|"bybit", "symbol": "XAUUSD", "tf": "M1"}
Server -> client: {"type": "history", "bars": [...]}, then {"type": "bar", "bar": {...}, "sent": epoch_ms}
                  {"type": "error", "msg": "..."}
"""
import asyncio, json, re, subprocess, threading, time, urllib.request, functools, os, sys
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
import websockets
HERE = os.path.dirname(os.path.abspath(__file__))

def load_env(path):
    """Minimal .env reader (KEY=VALUE lines). Real environment variables win."""
    try:
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    except OSError:
        pass

load_env(os.path.join(HERE, "..", ".env"))
load_env(os.path.join(HERE, ".env"))
import layouts_store
import logbus, feedlog
import feeds  # chart-source plugins (hub/feeds/)

HTTP_PORT = int(os.environ.get("HUB_HTTP_PORT", "8770"))
WS_PORT = int(os.environ.get("HUB_WS_PORT", "8765"))
# LAN access is opt-in: set HOST_IP (this PC's LAN address) to listen on all interfaces so phones and
# other computers on the same network can reach the hub. Empty = localhost only (the safe default).
HOST_IP = os.environ.get("HOST_IP", "").strip()
BIND_HOST = "0.0.0.0" if HOST_IP else "127.0.0.1"
ORIGINS = {f"http://localhost:{HTTP_PORT}", f"http://127.0.0.1:{HTTP_PORT}"}
if HOST_IP:
    ORIGINS.add(f"http://{HOST_IP}:{HTTP_PORT}")
LOCAL_MT5 = os.environ.get("LOCAL_MT5", "0") == "1"
MT5_TERMINAL_PATH = os.environ.get("MT5_TERMINAL_PATH", r"C:\Program Files\MetaTrader 5\terminal64.exe")
REMOTE_SSH_HOST = os.environ.get("REMOTE_MT5_SSH_HOST", "").strip()   # an alias from your ~/.ssh/config; empty = off
REMOTE_RESTART_CMD = os.environ.get("REMOTE_MT5_RESTART_CMD", "").strip()  # run over SSH by the Restart button, arg bridge|full
REMOTE_ON = bool(REMOTE_SSH_HOST)
REMOTE_TUNNEL = os.environ.get("REMOTE_MT5_TUNNEL", "1") != "0"     # 0 = another program owns the SSH tunnel: use it, never start or kill it
REMOTE_PREFIX = os.environ.get("REMOTE_MT5_PREFIX", "RMT_")          # remote MT5 ticker prefix (saved layouts use it)
REMOTE_LABEL = os.environ.get("REMOTE_MT5_LABEL", "Remote MT5")      # broker name shown in the app
REMOTE_SHORT = os.environ.get("REMOTE_MT5_SHORT", "RMT")             # its short name on the status lights and Symbol Search tags
LOCAL_LABEL = os.environ.get("LOCAL_MT5_LABEL", "Local MT5")          # name of the MT5 on this PC in the app
REMOTE_EXTRA_PORTS = [int(x) for x in os.environ.get("REMOTE_MT5_EXTRA_PORTS", "").split(",") if x.strip().isdigit()]

# Personal extras (git-ignored private/ folder): extra routes, pages scripts, background jobs. Absent = plain TradeX.
PRIV = None
_priv_dir = os.path.join(HERE, "..", "private")
if os.environ.get("TRADEX_PRIVATE", "1") != "0" and os.path.exists(os.path.join(_priv_dir, "tradex_private.py")):  # TRADEX_PRIVATE=0: test as a plain public install
    sys.path.insert(0, _priv_dir)
    try:
        import tradex_private as PRIV
    except Exception as _e:
        print(f"private/ extension not loaded: {_e}", flush=True)
        PRIV = None
TF_SEC = {"M1": 60, "M5": 300, "M15": 900, "M30": 1800, "H1": 3600, "H4": 14400, "D1": 86400}
BINANCE_TF = {"M1": "1m", "M5": "5m", "M15": "15m", "M30": "30m", "H1": "1h", "H4": "4h", "D1": "1d"}
# 2026-11-23: Binance = spot (ticker BTCUSD -> BTCUSDT) AND USD-M futures (ticker BINF_<SYMBOL>, e.g. BINF_XAUUSDT =
# real gold perpetual, BINF_TSLAUSDT, BINF_BTCUSDT). Old saved charts with Binance "XAUUSD" now open the gold perpetual
# instead of the thin PAXG token (PAXGUSD stays available as its own market).
BINANCE_SYM = {"XAUUSD": "BINF_XAUUSDT", "XAGUSD": "BINF_XAGUSDT"}
BINANCE_SPOT_REST, BINANCE_FUT_REST = "https://api.binance.com/api/v3/klines", "https://fapi.binance.com/fapi/v1/klines"
BINANCE_SPOT_WS, BINANCE_FUT_WS = "wss://data-stream.binance.vision/ws/", "wss://fstream.binance.com/market/ws/"

def bsym_of(symbol):
    """Ticker -> (Binance symbol, is_futures)."""
    s = symbol.upper().split(":")[-1]
    s = BINANCE_SYM.get(s, s)
    if s.startswith("BINF_"):
        return s[5:], True
    return (s + "T" if s.endswith("USD") else s), False
HISTORY = 1000

# ---------------- MT5 ----------------
mt5_lock = threading.Lock()
mt5_ready = False

mt5_fail = {"at": 0.0, "msg": ""}

def mt5_init():
    """Connect once; after a failure, wait 30 s before trying again so pages never hang."""
    global mt5_ready
    import MetaTrader5 as mt5
    if mt5_ready and mt5.terminal_info() is not None and mt5.account_info() is not None:
        return mt5, None
    mt5_ready = False
    if time.time() - mt5_fail["at"] < 30:
        return None, mt5_fail["msg"]
    # MT5_PORTABLE=1: the terminal keeps its data inside its own folder (the installer sets this up)
    ok = mt5.initialize(path=MT5_TERMINAL_PATH, timeout=10000, portable=os.environ.get("MT5_PORTABLE", "0") == "1")
    if not ok or mt5.account_info() is None:
        mt5_fail.update(at=time.time(), msg=f"MT5 not logged in ({mt5.last_error()}). Open MetaTrader 5 and log in to your demo account.")
        return None, mt5_fail["msg"]
    mt5_ready = True
    return mt5, None

def mt5_resolve(mt5, symbol):
    for s in (symbol, symbol + "m", symbol + ".", symbol + "z"):  # broker suffixes, e.g. Exness XAUUSDm
        if mt5.symbol_select(s, True):
            return s
    return None

def mt5_bars(mt5, sym, tf, count):
    tfc = getattr(mt5, "TIMEFRAME_" + tf)
    rates = mt5.copy_rates_from_pos(sym, tfc, 0, count)
    if rates is None:
        return []
    return [{"time": int(r["time"]), "open": float(r["open"]), "high": float(r["high"]),
             "low": float(r["low"]), "close": float(r["close"]), "volume": float(r["tick_volume"])} for r in rates]

async def stream_mt5(ws, symbol, tf):
    def setup():
        with mt5_lock:
            mt5, err = mt5_init()
            if err:
                return None, None, err
            sym = mt5_resolve(mt5, symbol)
            if not sym:
                return None, None, f"Symbol {symbol} not found at this broker."
            return sym, mt5_bars(mt5, sym, tf, HISTORY), None
    sym, bars, err = await asyncio.to_thread(setup)
    if err:
        await ws.send(json.dumps({"type": "error", "msg": err}))
        return
    await ws.send(json.dumps({"type": "history", "bars": bars, "source": "mt5", "symbol": sym, "tf": tf}))
    import MetaTrader5 as mt5
    last = None
    while True:
        def poll():
            with mt5_lock:
                return mt5_bars(mt5, sym, tf, 1)
        b = await asyncio.to_thread(poll)
        if b and b[-1] != last:
            last = b[-1]
            record_tick("mt5", symbol, 0)
            await ws.send(json.dumps({"type": "bar", "bar": last, "sent": int(time.time() * 1000)}))
        await asyncio.sleep(0.1)  # 10 checks per second

# ---------------- Remote MT5 (a terminal on your VPS, through an SSH tunnel to bridge/mt5_bridge.py) ----------------
# Internal broker id "arm". Tickers carry REMOTE_PREFIX (default "RMT_") so they never clash with other feeds.
# Use a chart-only terminal for this, never the terminal that trades (set its bridge port in REMOTE_MT5_BRIDGE_PORT).
ARM_HOST, ARM_PORT = "127.0.0.1", int(os.environ.get("REMOTE_MT5_BRIDGE_PORT", "8797"))
ARM_SYMBOLS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "arm_symbols_cache.json")
arm_symbols_cache = []
arm_status = {"msg": "connecting to the remote MT5 bridge..." if REMOTE_ON else "off"}
ARM_NODATA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "arm_nodata.json")
ARM_NODATA_DAYS = 7  # a symbol the VPS had no candles for stays hidden this long, then gets another chance
try:
    with open(ARM_NODATA_FILE, encoding="utf-8") as _f:
        arm_nodata = json.load(_f)
except (OSError, ValueError):
    arm_nodata = {}

def arm_nodata_live():
    cut = time.time() - ARM_NODATA_DAYS * 86400
    return {k for k, v in arm_nodata.items() if v.get("at", 0) > cut}

ARM_DEAD_RE = re.compile(r"no candles|invalid datetime range|symbol not found|unknown symbol", re.I)
ARM_DEAD_MSG = "{} has no candles on " + REMOTE_LABEL + "; it is now hidden from the list. Pick another broker or symbol"

arm_strikes = {}  # symbol -> time of its first empty answer

def arm_mark_nodata(ticker, why):
    """Hide a VPS symbol that answered with no candles (Symbol Search + broker switch), saved across restarts.
    Two strikes at least 20 s apart: a symbol the terminal opens for the first time can be empty while it downloads.
    Returns True when the symbol is now hidden."""
    sym, now = arm_raw(ticker), time.time()
    first = arm_strikes.get(sym)
    if not first or now - first > 3600:
        arm_strikes[sym] = now
        return False
    if now - first < 20:
        return False
    try:  # never hide a symbol while the terminal itself is unwell: 2026-11-24 a 16 h broker disconnect made
        arm_get("/health", timeout=10)  # every never-downloaded symbol look empty (health now checks the broker link)
    except Exception:
        return False
    arm_strikes.pop(sym, None)
    arm_nodata[sym] = {"at": int(now), "why": str(why)[:120]}
    try:
        with open(ARM_NODATA_FILE, "w", encoding="utf-8") as f:
            json.dump(arm_nodata, f, indent=0)
    except OSError:
        pass
    logbus.ev("feed.arm", "warn", f"{ticker}: no candles on {REMOTE_LABEL}, hidden from the list ({why})", every=5)
    return True
arm_bars_cache = {}  # (symbol, tf, limit) -> last good candles: shown while ARM reconnects, so the chart never goes blank
ARM_SSH = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15"]
arm_tunnel_proc = None
restart_state = {"running": False, "step": "", "ok": None, "at": 0}

def arm_tunnel_loop():
    """The chart server owns the SSH link to ARM: it starts with the hub and comes back 5 s after any drop."""
    global arm_tunnel_proc
    while True:
        try:
            err_path = os.path.join(logbus.LOG_DIR, "ssh_link.err")
            err_file = open(err_path, "w", encoding="utf-8")
            arm_tunnel_proc = subprocess.Popen(ARM_SSH + ["-N", "-C", "-o", "ExitOnForwardFailure=yes", "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3",
                                                          "-L", f"{ARM_PORT}:127.0.0.1:{ARM_PORT}"] + [a for x in REMOTE_EXTRA_PORTS for a in ("-L", f"{x}:127.0.0.1:{x}")] + [REMOTE_SSH_HOST],
                                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=err_file, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            logbus.ev("link", "debug", "SSH link process started", every=60)
            try:
                rc = arm_tunnel_proc.wait(timeout=20)
            except subprocess.TimeoutExpired:  # it held the ports for 20 s: this link is the owner
                logbus.problem("link.owner")
                rc = arm_tunnel_proc.wait()
            err_file.close()
            reason = open(err_path, encoding="utf-8", errors="replace").read().strip()[-200:]
            logbus.problem("link.owner", "another program holds the SSH link ports, so this server's link cannot start" if "already in use" in reason else None)
            logbus.ev("link", "error", f"SSH link process ended (exit {rc}){': ' + reason if reason else ''}; reconnecting in 5 s", every=60)
        except Exception as e:
            logbus.ev("link", "error", f"SSH link could not start: {e}", every=60)
        time.sleep(5)

def arm_kill_tunnel():
    """Drop the link, ours or one left by an old arm_tunnel.bat window. arm_tunnel_loop brings ours back."""
    if not REMOTE_TUNNEL:
        return   # the tunnel belongs to another program (REMOTE_MT5_TUNNEL=0)
    out = subprocess.run(["netstat", "-ano"], capture_output=True, text=True, timeout=10).stdout
    pids = {l.split()[-1] for l in out.splitlines() if f"127.0.0.1:{ARM_PORT} " in l and "LISTENING" in l}
    for pid in pids:
        names = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True, timeout=10).stdout
        if "ssh.exe" in names.lower():
            subprocess.run(["taskkill", "/F", "/PID", pid], capture_output=True, timeout=10)

def arm_answers():
    try:
        return bool(arm_get("/health", timeout=8).get("ok"))
    except Exception:
        return False

def arm_restart():
    """Restart button: fresh link, then a fresh bridge. The MT5 terminal (trading path) restarts only if the bridge still cannot answer."""
    def step(s):
        restart_state["step"] = s
        logbus.ev("restart", "info", s)
    def remote(mode, timeout):
        # Your own restart command on the VPS (REMOTE_MT5_RESTART_CMD), called with "bridge" or "full". None set = link only.
        if REMOTE_RESTART_CMD:
            subprocess.run(ARM_SSH + ["-n", REMOTE_SSH_HOST, f"{REMOTE_RESTART_CMD} {mode}"], capture_output=True, timeout=timeout)
    def wait_ok(seconds):
        end = time.time() + seconds
        while time.time() < end:
            if arm_answers():
                return True
            time.sleep(2)
        return False
    restart_state.update(running=True, ok=None, at=time.time())
    try:
        step(f"Reconnecting the {REMOTE_LABEL} link")
        arm_kill_tunnel()
        time.sleep(6)
        step(f"Restarting the {REMOTE_LABEL} bridge")
        remote("bridge", 40)
        ok = wait_ok(15)
        if not ok:
            step("Bridge still silent: full restart on the server (about 1 min)")
            remote("full", 150)
            ok = wait_ok(30)
        _status_cache["at"] = 0
        restart_state.update(ok=ok)
        step(f"{REMOTE_LABEL} is back" if ok else f"{REMOTE_LABEL} did not come back. Check the server over SSH.")
    except Exception as e:
        restart_state.update(ok=False)
        step(f"Restart failed: {e.__class__.__name__}: {e}")
    restart_state["running"] = False

_arm_conns = threading.local()  # one kept-alive tunnel connection per worker thread

def arm_get(path, conn=None, timeout=30):
    """GET from the VPS chart bridge. Without `conn`, reuses this thread's open connection: the laptop is ~260 ms
    from the VPS, and a fresh connection through the SSH link costs extra round trips (2026-11-24: 1.05 s -> 0.3 s)."""
    import http.client
    if conn is not None:
        conn.timeout = timeout
        conn.request("GET", path)
        data = json.loads(conn.getresponse().read())
    else:
        for attempt in (0, 1):
            c = getattr(_arm_conns, "c", None)
            fresh = c is None
            if fresh:
                c = _arm_conns.c = http.client.HTTPConnection(ARM_HOST, ARM_PORT, timeout=timeout)
            try:
                c.timeout = timeout
                if c.sock: c.sock.settimeout(timeout)
                c.request("GET", path)
                data = json.loads(c.getresponse().read())
                break
            except (http.client.HTTPException, ConnectionError, OSError, ValueError) as e:
                c.close(); _arm_conns.c = None
                if fresh or attempt or isinstance(e, TimeoutError):  # a slow answer is not retried (no double wait)  # a new connection failing is a real failure; a stale kept one gets one retry
                    raise
    if isinstance(data, dict) and data.get("error"):
        raise RuntimeError(data["error"])
    return data

def arm_raw(ticker):
    return ticker[len(REMOTE_PREFIX):] if ticker.startswith(REMOTE_PREFIX) else ticker

METAL_PREFIX = ("XAU", "XAG", "XPT", "XPD", "XAL", "XCU", "XNI", "XPB", "XZN")

def arm_group(name, path):
    """Group from the broker folder path. Works for MetaQuotes (Forex, Nasdaq ...) and Exness (Zero/Forex ...)."""
    parts = [p.lower() for p in (path or "").split("\\")]
    if name.upper().startswith(METAL_PREFIX) or "metals" in parts:
        return "arm-metal"
    for p in parts:
        if p.startswith("crypto"): return "arm-crypto"
        if p.startswith("forex"): return "arm-forex"
        if p in ("indices", "indexes", "idx_enlarge"): return "arm-index"
        if p.startswith("energ"): return "arm-energy"
        if p in ("stocks", "nasdaq"): return "arm-stock"
    return "arm-other"

ARM_ORDER = ["arm-metal", "arm-crypto", "arm-forex", "arm-index", "arm-energy", "arm-other", "arm-stock"]

# Always-include indices (2026-11-24). The chart terminal's symbol list stops at 4,096 names, so most indices never
# reach the menu although the terminal has them. Names below were checked one by one on MetaQuotes-Demo (one D1 candle
# each); the hub looks up any that are missing from the list once and adds them. Nifty / Sensex / VIX are NOT there.
ARM_ALWAYS_INDEX = {"US500": "S&P 500", "USTEC": "Nasdaq 100", "US30": "Dow Jones 30", "US2000": "Russell 2000",
                    "DE40": "Germany 40 (DAX)", "FRA40": "France 40 (CAC)", "UK100": "UK 100 (FTSE)",
                    "JPN225": "Japan 225 (Nikkei)", "HK50": "Hong Kong 50 (Hang Seng)", "AUS200": "Australia 200",
                    "EUSTX50": "Euro Stoxx 50", "IT40": "Italy 40", "SWI20": "Switzerland 20 (SMI)",
                    "NETH25": "Netherlands 25 (AEX)", "CHINA50": "China A50", "CHINAH": "China H-shares"}
# MetaQuotes names that are the same market as an existing instrument key (joined in one search row).
ARM_KEY_ALIAS = {"DE40": "DE30", "FRA40": "FR40", "JPN225": "JP225", "EUSTX50": "STOXX50"}
_arm_extra = {}  # name -> symbol row, looked up once per server run

def arm_always_rows(have):
    """Rows for always-include indices missing from the terminal's list (probed once, paced for the bridge limit)."""
    for name, desc in ARM_ALWAYS_INDEX.items():
        if REMOTE_PREFIX + name in have or name in _arm_extra:
            continue
        try:
            bars = arm_get(f"/bars?symbol={name}&tf=D1&limit=2", timeout=30)
        except Exception:
            bars = None
        _arm_extra[name] = {"ticker": REMOTE_PREFIX + name, "name": REMOTE_LABEL + " - " + desc, "source": "arm",
                            "group": "arm-index", "digits": 2} if bars else None
        time.sleep(0.3)
    return [r for n, r in _arm_extra.items() if r and r["ticker"] not in have]

def arm_symbols_loop():
    """Background: fetch the ARM symbol list; retry every 30 s until it answers, then refresh every 10 min
    (so switching the ARM MT5 to another account shows up without restarting this server)."""
    global arm_symbols_cache
    try:  # last known list, so ARM charts exist even if the hub opens during an ARM outage
        with open(ARM_SYMBOLS_FILE, encoding="utf-8") as f:
            arm_symbols_cache = json.load(f)
    except (OSError, ValueError):
        pass
    while True:
        try:
            rows = arm_get("/symbols", timeout=60)
            out = [{"ticker": REMOTE_PREFIX + s["name"], "name": REMOTE_LABEL + " - " + (s["desc"] or s["name"]),
                    "source": "arm", "group": arm_group(s["name"], s["path"]), "digits": s["digits"]} for s in rows]
            out += arm_always_rows({x["ticker"] for x in out})
            out.sort(key=lambda x: (ARM_ORDER.index(x["group"]), x["ticker"]))
            if len(out) != len(arm_symbols_cache):
                print(f"{REMOTE_LABEL}: {len(out)} symbols", flush=True)
            arm_symbols_cache = out
            with open(ARM_SYMBOLS_FILE, "w", encoding="utf-8") as f:
                json.dump(out, f)
            arm_status["msg"] = "ok"
            time.sleep(600)
        except Exception as e:
            arm_status["msg"] = f"{REMOTE_LABEL} bridge unreachable ({e}). Is the SSH tunnel on port {ARM_PORT} running?"
            print(f"{REMOTE_LABEL}:", arm_status["msg"], flush=True)
            time.sleep(30)

def arm_history(symbol, tf, limit=HISTORY, to_ms=None):
    from urllib.parse import quote
    path = f"/bars?symbol={quote(arm_raw(symbol))}&tf={tf}&limit={int(limit)}" + (f"&to={int(to_ms)}" if to_ms else "")
    if to_ms:
        return arm_get(path, timeout=20)
    key = (symbol, tf, int(limit))
    try:
        bars = arm_get(path, timeout=20)
        arm_bars_cache[key] = bars
        return bars
    except Exception:
        if key in arm_bars_cache:  # ARM is reconnecting: keep the chart on its last good candles
            logbus.ev("feed.arm", "warn", f"{symbol} {tf}: bridge not answering, serving last good candles", every=30)
            return arm_bars_cache[key]
        raise

async def stream_arm(ws, symbol, tf):
    import http.client
    from urllib.parse import quote
    # 2026-11-24: no history download here any more. Every chart loads its candles through /api/bars (arm_history),
    # and no WebSocket client uses a "history" message (lab.js ignores it), so fetching 1,000 candles here too sent the
    # same history over the slow SSH link twice on every symbol switch. "No data" hiding stays with /api/bars; the
    # /wait loop below already reports "reconnecting" while the VPS restarts.
    raw = quote(arm_raw(symbol))
    conn, after = None, ""
    while True:
        try:
            if conn is None:
                conn = http.client.HTTPConnection(ARM_HOST, ARM_PORT, timeout=40)
            m = await asyncio.to_thread(arm_get, f"/wait?symbol={raw}&tf={tf}&after={quote(after)}", conn)
            if m["key"] != after and m["bar"]:
                after = m["key"]
                lag = record_tick("arm", symbol, m["tick_ms"]) if m.get("tick_ms") else None
                await ws.send(json.dumps({"type": "bar", "bar": m["bar"], "sent": int(time.time() * 1000), "lag_ms": lag}))
        except websockets.ConnectionClosed:
            return
        except Exception as e:
            try:
                conn.close()
            except Exception:
                pass
            conn = None
            await ws.send(json.dumps({"type": "status", "msg": f"{REMOTE_LABEL} reconnecting: {e}"}))
            await asyncio.sleep(3)

# ---------------- Binance (no login) ----------------
async def stream_binance(ws, symbol, tf):
    bsym, fut = bsym_of(symbol)
    url = f"{BINANCE_FUT_REST if fut else BINANCE_SPOT_REST}?symbol={bsym}&interval={BINANCE_TF[tf]}&limit={HISTORY}"
    try:
        raw = await asyncio.to_thread(lambda: urllib.request.urlopen(url, timeout=15).read())
    except Exception as e:
        await ws.send(json.dumps({"type": "error", "msg": f"Binance history failed: {e}"}))
        return
    bars = [{"time": k[0] // 1000, "open": float(k[1]), "high": float(k[2]), "low": float(k[3]),
             "close": float(k[4]), "volume": float(k[5])} for k in json.loads(raw)]
    await ws.send(json.dumps({"type": "history", "bars": bars, "source": "binance", "symbol": bsym, "tf": tf}))
    span = TF_SEC[tf]
    cur = dict(bars[-1])
    last_sent = 0.0
    # Build the live candle from every trade (aggTrade), so updates are tick-level.
    # Port 9443 is blocked on this network; the public market-data host on 443 works.
    try:
        # Futures: the plain /ws/ path times out on this network; /market/ws/ works (tested 2026-11-23).
        feed = await websockets.connect(f"{BINANCE_FUT_WS if fut else BINANCE_SPOT_WS}{bsym.lower()}@aggTrade", open_timeout=10)
    except Exception as e:
        await ws.send(json.dumps({"type": "error", "msg": f"Live stream failed: {e}"}))
        return
    async with feed:
        async for msg in feed:
            t = json.loads(msg)
            p, q, ts = float(t["p"]), float(t["q"]), t["T"] // 1000
            start = ts - ts % span if tf != "D1" else ts - ts % 86400
            if start > cur["time"]:
                cur = {"time": start, "open": p, "high": p, "low": p, "close": p, "volume": q}
            else:
                cur["high"] = max(cur["high"], p); cur["low"] = min(cur["low"], p)
                cur["close"] = p; cur["volume"] += q
            now = time.time()
            lag = record_tick("binance", symbol, t["T"])
            if now - last_sent >= 0.05:  # at most 20 updates per second
                last_sent = now
                await ws.send(json.dumps({"type": "bar", "bar": cur, "sent": int(now * 1000), "trade_ms": t["T"], "lag_ms": lag}))

# ---------------- Bybit (public market data, no login; laptop only, Bybit blocks the ARM's US IP) ----------------
# Ticker format: BYBIT_<SYM> = USDT perpetual/futures (linear), BYBIT_S_<SYM> = spot, BYBIT_I_<SYM> = inverse.
# Suffix .MARK / .INDEX = mark-price / index-price candles (linear and inverse only).
BYBIT_REST = "https://api.bybit.com"
BYBIT_WS = "wss://stream.bybit.com/v5/public/"
BYBIT_TF = {"M1": "1", "M5": "5", "M15": "15", "M30": "30", "H1": "60", "H4": "240", "D1": "D"}
BYBIT_METAL = ("XAU", "XAG", "XPT", "XPD", "PAXG")
bybit_symbols_cache = []

def bybit_parse(ticker):
    """BYBIT_S_BTCUSDT.MARK -> ("spot", "BTCUSDT", "mark")."""
    t = ticker[6:]
    kind = "trade"
    if t.endswith(".MARK"): t, kind = t[:-5], "mark"
    elif t.endswith(".INDEX"): t, kind = t[:-6], "index"
    cat = "linear"
    if t.startswith("S_"): t, cat = t[2:], "spot"
    elif t.startswith("I_"): t, cat = t[2:], "inverse"
    return cat, t, kind

def bybit_get(path):
    d = json.loads(urllib.request.urlopen(BYBIT_REST + path, timeout=15).read())
    if d.get("retCode"):
        raise RuntimeError(f"Bybit: {d.get('retMsg')}")
    return d["result"]

def bybit_load_symbols():
    """Every trading Bybit market, most traded first inside each group. Returns True on success."""
    global bybit_symbols_cache
    try:
        out = []
        for cat, prefix in (("linear", ""), ("spot", "S_"), ("inverse", "I_")):
            rows, cur = [], ""
            while True:
                r = bybit_get(f"/v5/market/instruments-info?category={cat}&limit=1000" + (f"&cursor={cur}" if cur else ""))
                rows += r["list"]; cur = r.get("nextPageCursor")
                if not cur:
                    break
            turnover = {t["symbol"]: float(t.get("turnover24h") or 0) for t in bybit_get(f"/v5/market/tickers?category={cat}")["list"]}
            for s in rows:
                if s.get("status") != "Trading":
                    continue
                tick = (s.get("priceFilter") or {}).get("tickSize", "0.01").rstrip("0")
                digits = len(tick.split(".")[1]) if "." in tick else 0
                base, sym, ctype = s.get("baseCoin", ""), s["symbol"], s.get("contractType", "")
                if base.upper().startswith(BYBIT_METAL) or base.upper() == "XAUT":
                    group = "bybit-metal"
                elif cat == "spot":
                    group = "bybit-spot"
                elif cat == "inverse":
                    group = "bybit-inverse"
                elif ctype == "LinearFutures":
                    group = "bybit-futures"
                else:
                    group = "bybit-perp"
                label = {"linear": "Perp" if ctype != "LinearFutures" else "Futures", "spot": "Spot", "inverse": "Inverse"}[cat]
                row = {"ticker": f"BYBIT_{prefix}{sym}", "name": f"Bybit {label} - {base}/{s.get('quoteCoin', '')}",
                       "source": "bybit", "group": group, "digits": digits, "_t": turnover.get(sym, 0)}
                out.append(row)
                if cat != "spot":
                    for kind, g in (("MARK", "bybit-mark"), ("INDEX", "bybit-index")):
                        out.append({**row, "ticker": f"{row['ticker']}.{kind}", "name": f"{row['name']} ({kind.lower()} price)", "group": g})
        order = ["bybit-metal", "bybit-perp", "bybit-spot", "bybit-futures", "bybit-inverse", "bybit-mark", "bybit-index"]
        out.sort(key=lambda x: (order.index(x["group"]), -x["_t"]))
        for x in out:
            x.pop("_t")
        bybit_symbols_cache = out
        print(f"Bybit: {len(out)} charts loaded", flush=True)
        return True
    except Exception as e:
        print("Bybit symbols failed:", e, flush=True)
        return False

def bybit_symbols_loop():
    while True:
        time.sleep(3600 if bybit_load_symbols() else 60)

def bybit_history(symbol, tf, limit=HISTORY, to_ms=None):
    cat, sym, kind = bybit_parse(symbol)
    ep = {"trade": "kline", "mark": "mark-price-kline", "index": "index-price-kline"}[kind]
    path = f"/v5/market/{ep}?category={cat}&symbol={sym}&interval={BYBIT_TF[tf]}&limit={min(int(limit), 1000)}"
    if to_ms:
        path += f"&end={int(to_ms)}"
    rows = bybit_get(path)["list"]
    return [{"time": int(k[0]) // 1000, "open": float(k[1]), "high": float(k[2]), "low": float(k[3]), "close": float(k[4]),
             "volume": float(k[5]) if len(k) > 5 else 0.0} for k in reversed(rows)]

async def stream_bybit(ws, symbol, tf):
    cat, sym, kind = bybit_parse(symbol)
    try:
        bars = await asyncio.to_thread(bybit_history, symbol, tf)
    except Exception as e:
        await ws.send(json.dumps({"type": "error", "msg": f"Bybit history failed: {e}"})); return
    await ws.send(json.dumps({"type": "history", "bars": bars, "source": "bybit", "symbol": symbol, "tf": tf}))
    if not bars:
        return
    span = TF_SEC[tf]
    cur = dict(bars[-1])
    topic = f"publicTrade.{sym}" if kind == "trade" else f"tickers.{sym}"
    field = {"mark": "markPrice", "index": "indexPrice"}.get(kind)
    last_sent = 0.0

    def apply(p, q, ts_ms):
        nonlocal cur
        ts = ts_ms // 1000
        start = ts - ts % span
        if start > cur["time"]:
            cur = {"time": start, "open": p, "high": p, "low": p, "close": p, "volume": q}
        elif start == cur["time"]:
            cur["high"] = max(cur["high"], p); cur["low"] = min(cur["low"], p)
            cur["close"] = p; cur["volume"] += q

    while True:
        try:
            async with websockets.connect(BYBIT_WS + cat, open_timeout=25, ping_interval=None) as feed:
                await feed.send(json.dumps({"op": "subscribe", "args": [topic]}))
                next_ping = time.time() + 20
                while True:
                    try:
                        msg = json.loads(await asyncio.wait_for(feed.recv(), timeout=max(0.1, next_ping - time.time())))
                    except asyncio.TimeoutError:
                        await feed.send(json.dumps({"op": "ping"})); next_ping = time.time() + 20
                        continue
                    if msg.get("topic") != topic:
                        continue
                    if kind == "trade":
                        for t in msg["data"]:
                            apply(float(t["p"]), float(t["v"]), int(t["T"]))
                        src_ms = int(msg["data"][-1]["T"])
                    else:
                        v = msg["data"].get(field)
                        if not v:
                            continue
                        src_ms = int(msg["ts"])
                        apply(float(v), 0.0, src_ms)
                    lag = record_tick("bybit", symbol, src_ms)
                    now = time.time()
                    if now - last_sent >= 0.05:  # at most 20 updates per second
                        last_sent = now
                        await ws.send(json.dumps({"type": "bar", "bar": cur, "sent": int(now * 1000), "trade_ms": src_ms, "lag_ms": lag}))
        except websockets.ConnectionClosed as e:
            if ws.close_code is not None:
                return
            await ws.send(json.dumps({"type": "status", "msg": f"Bybit reconnecting: {e}"}))
        except Exception as e:
            await ws.send(json.dumps({"type": "status", "msg": f"Bybit reconnecting: {e}"}))
        await asyncio.sleep(2)

# ---------------- Clock + feed health ----------------
CLOCK_OFFSET_MS = 0.0  # add to local time to get true time (this laptop runs about 3 s slow)

def measure_clock_offset(server="time.windows.com"):
    """Tiny SNTP query. Returns true_time - local_time in ms."""
    import socket, struct
    global CLOCK_OFFSET_MS
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.settimeout(5)
        t1 = time.time(); s.sendto(b"\x1b" + 47 * b"\0", (server, 123)); data, _ = s.recvfrom(48); t4 = time.time()
        secs, frac = struct.unpack("!II", data[40:48])
        srv = secs - 2208988800 + frac / 2**32
        CLOCK_OFFSET_MS = (srv - (t1 + t4) / 2) * 1000
    except Exception as e:
        print("clock offset failed:", e, flush=True)
    return CLOCK_OFFSET_MS

def true_now_ms():
    return time.time() * 1000 + CLOCK_OFFSET_MS

FEED_HEALTH = {}  # "source:symbol" -> stats the agent reads to pick the freshest feed

def record_tick(source, symbol, src_ms):
    now = true_now_ms()
    h = FEED_HEALTH.setdefault(f"{source}:{symbol}", {"ticks": 0, "lags": []})
    h["ticks"] += 1; h["last_tick_ms"] = now; h["last_src_ms"] = src_ms
    lag = now - src_ms if src_ms else None
    if lag is not None:
        h["lags"].append(lag); h["lags"] = h["lags"][-300:]
    return lag

def health_report():
    now = true_now_ms(); out = {}
    for k, h in FEED_HEALTH.items():
        lags = sorted(h["lags"])
        out[k] = {"ticks": h["ticks"], "age_ms": round(now - h.get("last_tick_ms", 0)),
                  "lag_median_ms": round(lags[len(lags) // 2]) if lags else None,
                  "lag_p95_ms": round(lags[int(len(lags) * 0.95)]) if lags else None}
    return {"clock_offset_ms": round(CLOCK_OFFSET_MS), "feeds": out}

# ---------------- Deriv (free, no login) ----------------
DERIV_URL = "wss://api.derivws.com/trading/v1/options/ws/public"
DERIV_GRAN = {"M1": 60, "M5": 300, "M15": 900, "M30": 1800, "H1": 3600, "H4": 14400, "D1": 86400}
DERIV_GROUP = {"forex": "forex", "commodities": "commodity", "cryptocurrency": "crypto", "indices": "indices"}
deriv_symbols_cache = []

# Opening a Deriv connection costs about 1.5 s, more than the request itself, and Deriv returns at most 1,000 candles
# per reply, so a deep chart needs about 10 requests. Reuse open connections (a small pool) instead of one per request:
# a 10,000-candle gold chart went from about 30 s to under 10 s (2026-11-15).
_deriv_pool, _deriv_pool_lock, _deriv_req_id = [], threading.Lock(), [0]

def deriv_request(req):
    from websockets.sync.client import connect
    with _deriv_pool_lock:
        c = _deriv_pool.pop() if _deriv_pool else None
        _deriv_req_id[0] += 1; rid = _deriv_req_id[0]
    for attempt in (1, 2):
        try:
            if c is None:
                c = connect(DERIV_URL, open_timeout=10).__enter__()  # kept open for reuse (pool)
            c.send(json.dumps({**req, "req_id": rid}))
            while True:
                r = json.loads(c.recv(timeout=20))
                if r.get("req_id") in (rid, None):
                    break
            with _deriv_pool_lock:
                if len(_deriv_pool) < 6:
                    _deriv_pool.append(c); c = None
            if c is not None:
                c.close()
            return r
        except Exception:
            try:
                c and c.close()
            except Exception:
                pass
            c = None
            if attempt == 2:
                raise

def deriv_load_symbols():
    global deriv_symbols_cache
    try:
        r = deriv_request({"active_symbols": "brief"})
        out = []
        for s in r.get("active_symbols", []):
            g = DERIV_GROUP.get(s.get("market"))
            if not g:
                continue
            pip = s.get("pip_size") or 0.01
            digits = max(0, round(-__import__("math").log10(pip))) if pip < 1 else 0
            out.append({"ticker": s["underlying_symbol"], "name": s.get("underlying_symbol_name", s["underlying_symbol"]) + " (Deriv)",
                        "source": "deriv", "group": g, "digits": digits, "open": bool(s.get("exchange_is_open"))})
        out.sort(key=lambda x: (["commodity", "forex", "indices", "crypto"].index(x["group"]), x["ticker"]))
        deriv_symbols_cache = out
        print(f"Deriv: {len(out)} symbols loaded", flush=True)
        return True
    except Exception as e:
        print("Deriv symbols failed:", e, flush=True)
        return False

def deriv_symbols_loop():
    """Background: retry the Deriv symbol list every 60 s until it loads."""
    while not deriv_load_symbols():
        time.sleep(60)

def _deriv_bars(r):
    if r.get("error"):
        raise RuntimeError(r["error"].get("message"))
    return [{"time": int(c["epoch"]), "open": float(c["open"]), "high": float(c["high"]), "low": float(c["low"]),
             "close": float(c["close"]), "volume": 0.0} for c in r.get("candles") or []]

def deriv_history(symbol, tf, limit=HISTORY, to_ms=None):
    limit, g = int(limit), DERIV_GRAN[tf]
    if limit <= 1000:
        return _deriv_bars(deriv_request({"ticks_history": symbol, "style": "candles", "granularity": g, "count": limit,
                                          "end": str(int(int(to_ms) / 1000)) if to_ms else "latest", "adjust_start_time": 1}))
    # Deriv answers at most 1,000 candles per request. For a deep chart, ask for back-to-back time windows of 1,000
    # candles in parallel (5 at a time) instead of one after another: 10,000 gold candles in ~3 s, not ~20 s (2026-11-15).
    # Closed-market windows (weekends) come back empty; keep stepping back until enough candles or 4 empty batches.
    from concurrent.futures import ThreadPoolExecutor
    end = int(int(to_ms) / 1000) if to_ms else int(time.time())
    end -= end % g
    span, got, empty = 1000 * g, {}, 0
    with ThreadPoolExecutor(max_workers=5) as pool:
        while len(got) < limit and empty < 4:
            n = max(1, min(10, -(-(limit - len(got)) // 1000)))
            wins = [(end - (k + 1) * span + g, end - k * span) for k in range(n)]
            reqs = [{"ticks_history": symbol, "style": "candles", "granularity": g, "start": a, "end": b, "count": 1000} for a, b in wins]
            before = len(got)
            for bars in pool.map(lambda q: _deriv_bars(deriv_request(q)), reqs):
                for x in bars:
                    got[x["time"]] = x
            empty = empty + 1 if len(got) == before else 0
            end -= n * span
    return [got[t] for t in sorted(got)][-limit:]

async def stream_deriv(ws, symbol, tf):
    try:
        bars = await asyncio.to_thread(deriv_history, symbol, tf)
    except Exception as e:
        await ws.send(json.dumps({"type": "error", "msg": f"Deriv history failed: {e}"})); return
    await ws.send(json.dumps({"type": "history", "bars": bars, "source": "deriv", "symbol": symbol, "tf": tf}))
    span = TF_SEC[tf]
    cur = dict(bars[-1]) if bars else None
    while True:  # reconnects; waits while the market is closed
        try:
            async with websockets.connect(DERIV_URL, open_timeout=10) as feed:
                await feed.send(json.dumps({"ticks": symbol, "subscribe": 1}))
                async for msg in feed:
                    m = json.loads(msg)
                    if m.get("error"):
                        await ws.send(json.dumps({"type": "status", "msg": f"Deriv: {m['error'].get('message')}"}))
                        break
                    t = m.get("tick")
                    if not t:
                        continue
                    p, ts = float(t["quote"]), int(t["epoch"])
                    lag = record_tick("deriv", symbol, ts * 1000)
                    start = ts - ts % span
                    if not cur or start > cur["time"]:
                        cur = {"time": start, "open": p, "high": p, "low": p, "close": p, "volume": 0.0}
                    else:
                        cur["high"] = max(cur["high"], p); cur["low"] = min(cur["low"], p); cur["close"] = p
                    await ws.send(json.dumps({"type": "bar", "bar": cur, "sent": int(time.time() * 1000), "lag_ms": lag}))
        except websockets.ConnectionClosed:
            if ws.state.name != "OPEN":
                return  # the chart page closed
        except Exception as e:
            await ws.send(json.dumps({"type": "status", "msg": f"Deriv reconnecting: {e}"}))
        await asyncio.sleep(60)

# ---------------- WebSocket hub ----------------
async def handler(ws):
    task = None
    try:
        async for msg in ws:
            req = json.loads(msg)
            if task:
                task.cancel()
            src, sym, tf = req.get("source", "binance"), req.get("symbol", "XAUUSD"), req.get("tf", "M1")
            if tf not in TF_SEC:
                await ws.send(json.dumps({"type": "error", "msg": f"Unknown timeframe {tf}"}))
                continue
            fn = feeds.stream_fn(src) or {"mt5": stream_mt5, "deriv": stream_deriv, "arm": stream_arm, "bybit": stream_bybit}.get(src, stream_binance)
            task = feedlog.start_stream(fn, ws, src if fn is not stream_binance else "binance", sym, tf)
    except websockets.ConnectionClosed:
        pass
    finally:
        if task:
            task.cancel()

def history_fn(src):
    """Candle history function for a source id: a plugin's history(), else the built-in feeds (Binance default)."""
    plug = feeds.get(src or "")
    if plug:
        return plug.history
    return {"mt5": mt5_history, "deriv": deriv_history, "arm": arm_history, "bybit": bybit_history}.get(src, binance_history)

def binance_history(symbol, tf, limit=HISTORY, to_ms=None):
    """History for scroll-back. to_ms = newest bar open time wanted (epoch ms)."""
    bsym, fut = bsym_of(symbol)
    url = f"{BINANCE_FUT_REST if fut else BINANCE_SPOT_REST}?symbol={bsym}&interval={BINANCE_TF[tf]}&limit={min(int(limit), 1000)}"
    if to_ms:
        url += f"&endTime={int(to_ms)}"
    rows = json.loads(urllib.request.urlopen(url, timeout=15).read())
    return [{"time": k[0] // 1000, "open": float(k[1]), "high": float(k[2]), "low": float(k[3]),
             "close": float(k[4]), "volume": float(k[5])} for k in rows]

CRYPTO = [("BTCUSD", "Bitcoin"), ("ETHUSD", "Ethereum"), ("SOLUSD", "Solana"), ("BNBUSD", "BNB"),
          ("XRPUSD", "XRP"), ("DOGEUSD", "Dogecoin"), ("ADAUSD", "Cardano"), ("LINKUSD", "Chainlink"),
          ("PAXGUSD", "Gold token PAXG (thin trading, not gold spot)")]
# Full Binance list (spot USDT + USD-M perpetuals incl. TradFi: gold, silver, oil, US stocks). Fetched in the background,
# refreshed hourly; CRYPTO above is only the fallback when Binance cannot be reached at start.
BINANCE_TRADFI_KEY = {"XAU": "XAUUSD", "XAG": "XAGUSD", "XPT": "XPTUSD", "XPD": "XPDUSD", "COPPER": "XCUUSD",
                      "CL": "USOIL", "BZ": "UKOIL", "NATGAS": "XNGUSD"}
binance_cache = {"list": [{"ticker": t, "name": n, "source": "binance", "group": "crypto"} for t, n in CRYPTO], "at": 0}

def binance_load():
    out = []
    si = json.loads(urllib.request.urlopen("https://api.binance.com/api/v3/exchangeInfo?permissions=SPOT", timeout=20).read())
    for s in si["symbols"]:
        if s["status"] == "TRADING" and s["quoteAsset"] == "USDT" and s["baseAsset"].isalnum():
            out.append({"ticker": s["baseAsset"] + "USD", "name": f"{s['baseAsset']} / Tether (Binance spot)", "source": "binance",
                        "group": "metal" if s["baseAsset"] == "PAXG" else "crypto", "bkind": "spot", "base": s["baseAsset"]})
    fi = json.loads(urllib.request.urlopen("https://fapi.binance.com/fapi/v1/exchangeInfo", timeout=20).read())
    for s in fi["symbols"]:
        if s["status"] != "TRADING" or s["quoteAsset"] != "USDT" or s["contractType"] not in ("PERPETUAL", "TRADIFI_PERPETUAL"):
            continue
        u, base = s.get("underlyingType"), s["baseAsset"]
        grp = {"COMMODITY": "metal" if base in ("XAU", "XAG", "XPT", "XPD", "COPPER") else "energy", "EQUITY": "stock"}.get(u, "crypto")
        out.append({"ticker": "BINF_" + s["symbol"], "name": f"{base} perpetual (Binance futures)", "source": "binance",
                    "group": grp, "bkind": "perp", "base": base, "utype": u})
    return out

def binance_loop():
    while True:
        try:
            lst = binance_load()
            if lst:
                binance_cache.update(list=lst, at=time.time())
                print(f"Binance: {len(lst)} markets", flush=True)
            time.sleep(3600)
        except Exception as e:
            print("Binance market list failed:", e, flush=True)
            time.sleep(120)
MT5_GROUPS = ("forex", "metal", "energ", "commod", "oil", "agri", "soft")  # path words we keep

def mt5_watchdog():
    """Background: keep trying to connect to MT5 once a minute. Requests never wait for it."""
    if not LOCAL_MT5:
        return
    while True:
        if not mt5_ready:
            with mt5_lock:
                mt5_fail["at"] = 0
                _, err = mt5_init()
            print("MT5:", "connected" if not err else err, flush=True)
        time.sleep(60)

_mt5_list_cache = {"at": 0, "data": None}

def mt5_symbol_list():
    """Cached 60 s: every request used to call into the laptop MT5, and one stuck call froze the whole server."""
    if _mt5_list_cache["data"] is not None and time.time() - _mt5_list_cache["at"] < 60:
        return _mt5_list_cache["data"]
    res = _mt5_symbol_list()
    if res[0]:
        _mt5_list_cache.update(at=time.time(), data=res)
    return res

def _mt5_symbol_list():
    """Every forex pair and commodity the logged-in broker offers. Empty if MT5 is not logged in."""
    if not LOCAL_MT5:
        return [], "off"
    if not mt5_ready:
        return [], mt5_fail["msg"] or "MT5 connecting..."
    with mt5_lock:
        mt5, err = mt5_init()
        if err:
            return [], err
        out = []
        for s in mt5.symbols_get() or []:
            path = (s.path or "").lower()
            group = next((g for g in MT5_GROUPS if g in path), None)
            if not group:
                continue
            kind = "forex" if group == "forex" else "commodity"
            out.append({"ticker": s.name, "name": s.description or s.name, "source": "mt5", "group": kind,
                        "digits": s.digits, "path": s.path})
        out.sort(key=lambda x: (x["group"] != "forex", x["ticker"]))
        return out, None

def mt5_history(symbol, tf, limit=HISTORY, to_ms=None):
    from datetime import datetime, timezone
    with mt5_lock:
        mt5, err = mt5_init()
        if err:
            raise RuntimeError(err)
        if not mt5.symbol_select(symbol, True):
            raise RuntimeError(f"{symbol} not available at this broker")
        tfc = getattr(mt5, "TIMEFRAME_" + tf)
        if to_ms:
            rates = mt5.copy_rates_from(symbol, tfc, datetime.fromtimestamp(int(to_ms) / 1000, tz=timezone.utc), int(limit))
        else:
            rates = mt5.copy_rates_from_pos(symbol, tfc, 0, int(limit))
    if rates is None:
        return []
    return [{"time": int(r["time"]), "open": float(r["open"]), "high": float(r["high"]), "low": float(r["low"]),
             "close": float(r["close"]), "volume": float(r["tick_volume"])} for r in rates]

# ---------------- Instrument map (TradeX: switch broker, stay on the same instrument) ----------------
# One instrument = the same underlying market at several brokers. Only exact matches are joined:
# gold tokens (PAXG, XAUT) are NOT gold spot, so they stay separate instruments.
BROKERS = ([("arm", REMOTE_LABEL)] if REMOTE_ON else []) + [("bybit", "Bybit"), ("deriv", "Deriv")] + \
          ([("mt5", LOCAL_LABEL)] if LOCAL_MT5 else []) + [("binance", "Binance")]
BROKERS += [(pid, m.MANIFEST["name"]) for pid, m in feeds.enabled().items()]  # plugins on at start (switching needs a restart)
SHORTS = {pid: m.MANIFEST.get("short", pid[:3].upper()) for pid, m in feeds.enabled().items()}
DERIV_INDEX = {"OTC_NDX": "USTEC", "OTC_DJI": "US30", "OTC_SPC": "US500", "OTC_FTSE": "UK100", "OTC_GDAXI": "DE30",
               "OTC_FCHI": "FR40", "OTC_N225": "JP225", "OTC_HSI": "HK50", "OTC_AS51": "AUS200", "OTC_SX5E": "STOXX50",
               "OTC_AEX": "NETH25", "OTC_SSMI": "SWI20"}  # last two joined with remote MT5 names 2026-11-24
def variant_rank(label):
    """Main market first: perpetual, USDC perpetual, spot, inverse, dated futures, then mark/index price."""
    for n, w in enumerate(("mark price", "index price")):
        if w in label: return 10 + n
    for n, w in enumerate(("perpetual", "USDC perpetual", "spot", "inverse", "futures")):
        if label.startswith(w) and (w != "perpetual" or label == "perpetual"): return n
    return 0
_instr_cache = {"n": -1, "data": None}

def instrument_key(s):
    """(key, variant label) for a symbol, or (None, None) when it has no cross-broker meaning."""
    t, src = s["ticker"], s["source"]
    if src in SHORTS:   # a plugin feed: its symbol list carries the cross-broker key
        return feeds.instrument_key(t, src), None
    if src == "arm":
        return ARM_KEY_ALIAS.get(arm_raw(t), arm_raw(t)), None
    if src == "deriv":
        if t in DERIV_INDEX: return DERIV_INDEX[t], None
        if t[:3] in ("frx", "cry"): return t[3:], None
        return t, None
    if src == "mt5":
        return t, None
    if src == "binance":
        if not t.startswith("BINF_"):
            return t, ("spot" if s.get("bkind") else None)
        base, u = s.get("base") or t[5:-4], s.get("utype")
        if u == "COMMODITY":
            return BINANCE_TRADFI_KEY.get(base, base + "USD"), "perpetual"
        if u == "EQUITY":
            return base, "perpetual"
        if base[:1].isdigit():            # 1000PEPE style contracts: a different unit, not the same price as spot
            return None, None
        return base + "USD", "perpetual"
    if src == "bybit":
        cat, sym, kind = bybit_parse(t)
        if "-" in sym and kind != "trade":
            return None, None  # mark/index of dated futures: too niche for the switch
        # Label from the market itself, not the menu group (metals mix perps, futures and spot).
        if cat == "spot": label = "spot"
        elif "-" in sym: label = "futures " + sym.split("-")[-1]
        elif cat == "inverse": label = "inverse" + (" futures " + sym[-3:] if sym[-3:-2].isalpha() and sym[-2:].isdigit() else "")
        elif sym.endswith("PERP"): label = "USDC perpetual"
        else: label = "perpetual"
        if kind != "trade": label += " - " + kind + " price"
        base = sym.split("-")[0]
        for q in ("USDT", "USD", "PERP", "USDC"):
            if base.endswith(q) and len(base) > len(q):
                if q == "USDC":
                    return None, None  # USDC-quoted duplicates; the USDT market is the main one
                return base[: -len(q)] + "USD", label
        return None, None
    return None, None

# ---------------- Symbol Search categories (one source for the hub dialog and for agents) ----------------
# TradingView-style tabs. Only the kinds we really have data for (no bonds, economy or options yet).
CATS = [("stock", "Stocks"), ("fund", "Funds"), ("forex", "Forex"), ("crypto", "Crypto"), ("index", "Indices"), ("commodity", "Commodities")]
METAL_PREFIX = ("XAU", "XAG", "XPT", "XPD", "XAL", "XNI", "XPB", "XZN", "XCU")
FUND_RE = re.compile(r"\b(ETF|ETN|ETFs|Fund|Funds|iShares|SPDR|ProShares|Invesco|Vanguard|Direxion|GraniteShares|Daily Target|"
                     r"Global X|WisdomTree|VanEck|Schwab|KraneShares|Roundhill|YieldMax|Defiance|Tradr|Leverage Shares|T-Rex)\b", re.I)

# Binance-only stock perpetuals have no description ("SPY perpetual"), so the ETFs among them are named here.
KNOWN_FUNDS = {"SPY", "QQQ", "IWM", "DIA", "GLD", "SLV", "TLT", "SMH", "SOXX", "SOXL", "SOXS", "TQQQ", "SQQQ", "TZA", "TMF", "TBT",
               "UVXY", "XBI", "XLE", "EWT", "EWZ", "EWJ", "EWY", "KORU", "URNM", "TSLL", "MSTU", "MUU", "SKUU", "SNXX", "AGPU"}

def instrument_cat(i):
    g, k, name = i["group"], i["key"], i["name"]
    if k in KNOWN_FUNDS and g in ("stock", "other"):
        return "fund"
    if g in ("metal", "commodity", "energy") or (g == "forex" and k.startswith(METAL_PREFIX)):
        return "commodity"
    if g == "forex":
        return "forex"
    if g in ("index", "indices"):
        return "index"
    if g in ("crypto", "bybit-perp", "bybit-spot", "bybit-metal") or g.startswith("bybit"):
        return "crypto"
    if g in ("stock", "other"):
        return "fund" if FUND_RE.search(name) else "stock"
    return "stock"

# A trader typing these words means the main market, so it comes first (above e.g. the stock ticker GOLD = Barrick).
SEARCH_ALIASES = {"GOLD": "XAUUSD", "SILVER": "XAGUSD", "PLATINUM": "XPTUSD", "PALLADIUM": "XPDUSD", "OIL": "USOIL", "WTI": "USOIL",
                  "BRENT": "UKOIL", "GAS": "XNGUSD", "BITCOIN": "BTCUSD", "BTC": "BTCUSD", "ETHEREUM": "ETHUSD", "ETH": "ETHUSD",
                  "NASDAQ": "USTEC", "NAS100": "USTEC", "SP500": "US500", "SPX": "US500", "DOW": "US30", "DAX": "DE30", "FTSE": "UK100", "NIKKEI": "JP225",
                  "DE40": "DE30", "GER40": "DE30", "CAC": "FR40", "FRA40": "FR40", "JPN225": "JP225", "HANGSENG": "HK50",
                  "RUSSELL": "US2000", "STOXX": "STOXX50", "EUSTX50": "STOXX50", "SMI": "SWI20", "AEX": "NETH25"}

def search_instruments(q="", cat="", broker="", offset=0, limit=50):
    """Ranked instrument search, same order the hub's Symbol Search shows.
    Rank: a common word's main market (SEARCH_ALIASES), exact symbol, symbol starts with, a name word starts with, symbol contains, name contains; then the
    default order (most brokers first). Every word of the query must match the symbol or the name."""
    words = [w for w in q.upper().split() if w]
    rows = []
    for n, i in enumerate(build_instruments()["instruments"]):
        if cat and i["cat"] != cat: continue
        if broker and broker not in i["brokers"]: continue
        if words:
            k, nm = i["key"], i["name"].upper()
            if k != SEARCH_ALIASES.get("".join(words)) and not all(w in k or w in nm for w in words): continue
            w0, nw = words[0], nm.replace("/", " ").replace("-", " ").split()
            r = -1 if SEARCH_ALIASES.get("".join(words)) == k else 0 if k == w0 or k == "".join(words) else 1 if k.startswith(w0) else 2 if any(x.startswith(w0) for x in nw) else 3 if w0 in k else 4
        else:
            r = 0
        rows.append((r, n, i))
    rows.sort(key=lambda x: (x[0], x[1]))
    return {"total": len(rows), "offset": offset, "results": [x[2] for x in rows[offset: offset + limit]]}

def build_instruments():
    dead = arm_nodata_live()
    arm_ok = [x for x in arm_symbols_cache if arm_raw(x["ticker"]) not in dead]
    syms = arm_ok + bybit_symbols_cache + deriv_symbols_cache + mt5_symbol_list()[0] + binance_cache["list"] + feeds.all_symbols()
    if _instr_cache["n"] == (len(syms), len(dead)):
        return _instr_cache["data"]
    inst = {}
    for s in syms:
        k, variant = instrument_key(s)
        if not k:
            continue
        i = inst.setdefault(k, {"key": k, "name": "", "group": s["group"], "brokers": {}})
        opts = i["brokers"].setdefault(s["source"], [])
        opts.append({"ticker": s["ticker"], "label": variant or "", "_o": variant_rank(variant or "")})
        if s["source"] == "arm" or not i["name"]:
            i["name"] = s["name"].replace(REMOTE_LABEL + " - ", "").split(" (")[0]
            if s["source"] == "arm":
                i["group"] = s["group"].replace("arm-", "")
    out = []
    for i in inst.values():
        for src, opts in i["brokers"].items():
            opts.sort(key=lambda o: (o["_o"], o["label"]))
            for o in opts: o.pop("_o")
        out.append(i)
    for i in out:
        i["cat"] = instrument_cat(i)
    rank = {"commodity": 0, "crypto": 1, "forex": 2, "index": 3, "fund": 5, "stock": 6}
    out.sort(key=lambda i: (-len(i["brokers"]), rank.get(i["cat"], 4), i["key"]))
    data = {"brokers": [{"id": b, "name": n, "short": SHORTS.get(b) or (REMOTE_SHORT if b == "arm" else None)} for b, n in BROKERS], "cats": [{"id": c, "name": n} for c, n in CATS], "instruments": out}
    _instr_cache.update(n=(len(syms), len(dead)), data=data)
    return data

_status_cache = {"at": 0, "data": None}

def feed_status():
    """Small, cached (5 s) health of each broker feed for the TradeX status lights."""
    if time.time() - _status_cache["at"] < 5:
        return _status_cache["data"]
    data = {}
    if REMOTE_ON:
        try:
            h = arm_get("/health", timeout=3)
            data["arm"] = {"ok": True, "msg": f"{h.get('server')} {h.get('login')}"}
        except Exception as e:
            data["arm"] = {"ok": False, "msg": f"{REMOTE_LABEL} link down ({e.__class__.__name__}); it reconnects by itself"}
    data.update({
            "bybit": {"ok": bool(bybit_symbols_cache), "msg": f"{len(bybit_symbols_cache)} charts"},
            "deriv": {"ok": bool(deriv_symbols_cache), "msg": f"{len(deriv_symbols_cache)} symbols"},
            "binance": {"ok": bool(binance_cache["at"]), "msg": f"{len(binance_cache['list'])} markets (spot + futures)" if binance_cache["at"] else "loading market list"}})
    if LOCAL_MT5:
        data["mt5"] = {"ok": bool(mt5_ready), "msg": "connected" if mt5_ready else (mt5_fail.get("msg") or "not connected")}
    data.update(feeds.status())
    for k, v in data.items():  # one log line per change of a status light
        logbus.state("feed." + k, "ok", v["ok"], "info" if v["ok"] else "error" if k == "arm" else "warn", ("up: " if v["ok"] else "DOWN: ") + v["msg"])
        logbus.problem("feed." + k, None if v["ok"] else v["msg"])
    _status_cache.update(at=time.time(), data=data)
    return data

# Public-ready file serving (2026-11-23): only the app's own pages and libraries. Never logs, user data, caches, code,
# backups or screenshots (they used to be served: every file in this folder answered 200).
_STATIC_RE = re.compile(r"^/(?:vendor/)?[A-Za-z0-9_-][A-Za-z0-9_.-]*\.(?:html|js|css|png|svg|ico|woff|woff2)$")
_PRIV_JS_RE = re.compile(r"^/private/([A-Za-z0-9_-]+\.js)$")   # private/web/<name>.js, only when private/ exists
def static_allowed(path):
    from urllib.parse import urlparse, unquote
    p = unquote(urlparse(path).path)
    if p in ("/", ""):
        return True
    if any(part.startswith(".") for part in p.split("/") if part):   # no dotfiles or dot-folders
        return False
    return bool(_STATIC_RE.match(p)) and ".bak" not in p and ".." not in p and "\\" not in p

class LabHandler(SimpleHTTPRequestHandler):
    def do_HEAD(self):
        if not static_allowed(self.path):
            self.send_response(404); self.end_headers(); return
        return super().do_HEAD()

    """Static files plus GET /api/bars?symbol=&tf=&limit=&to=<ms>&source=binance|mt5 and GET /api/symbols."""
    def do_GET(self):
        from urllib.parse import urlparse, parse_qs
        if self.path.startswith("/api/layouts"):   # saved layouts + indicator templates (layouts_store.py)
            q = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
            if q.get("id"):
                doc = layouts_store.get(q.get("kind", "layouts"), q["id"])
                return self._json(doc or {"error": "not found"}, 200 if doc else 404)
            return self._json(layouts_store.listing(), 200)
        if self.path.startswith("/api/symbols"):
            crypto = binance_cache["list"]
            mt5_list, err = mt5_symbol_list()
            return self._json({"symbols": arm_symbols_cache + bybit_symbols_cache + deriv_symbols_cache + mt5_list + crypto + feeds.all_symbols(), "mt5": err or "ok", "arm": arm_status["msg"],
                               "deriv": len(deriv_symbols_cache), "bybit": len(bybit_symbols_cache), "ws_port": WS_PORT}, 200)
        if self.path.startswith("/api/sources"):   # chart-source plugins (hub/feeds/): manifest + on/off + health
            return self._json({"sources": feeds.manifests()}, 200)
        if self.path.startswith("/api/feeds/"):     # a plugin's own routes, e.g. /api/feeds/angel/status
            parts = urlparse(self.path).path.split("/")
            pid, action = (parts[3] if len(parts) > 3 else ""), (parts[4] if len(parts) > 4 else "status")
            try:
                return self._json(feeds.plugin_api(pid, action or "status", {}), 200)
            except KeyError:
                return self._json({"ok": False, "error": f"no such source: {pid}"}, 404)
            except Exception as e:
                return self._json({"ok": False, "error": f"{e.__class__.__name__}: {e}"}, 502)
        if self.path.startswith("/api/backtest/"):   # options backtest jobs (hub/backtest.py, research only)
            parts = urlparse(self.path).path.split("/")
            action = parts[3] if len(parts) > 3 and parts[3] else "defaults"
            q = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
            try:
                import backtest
                return self._json(backtest.api(action, {"id": q.get("id", ""),
                                                        "result": q.get("result", "") in ("1", "true", "yes")}), 200)
            except KeyError:
                return self._json({"ok": False, "error": f"no such backtest route: {action}"}, 404)
            except Exception as e:
                return self._json({"ok": False, "error": f"{e.__class__.__name__}: {e}"}, 502)
        if PRIV and getattr(PRIV, "handle_get", None) and PRIV.handle_get(self):   # personal routes (private/)
            return
        if self.path.startswith("/api/restart"):
            return self._json(restart_state, 200)
        if self.path.startswith("/api/logs"):
            q = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
            since = q.get("since", "0")
            return self._json({**logbus.snapshot(int(since) if since.isdigit() else 0, brief="brief" in q), "journal_raw": bool(PRIV and getattr(PRIV, "journal_raw", lambda: False)())}, 200)
        if self.path.startswith("/api/status"):
            return self._json({**feed_status(), **(PRIV.status() if PRIV and hasattr(PRIV, "status") else {})}, 200)
        if self.path.startswith("/api/instruments/search"):
            # Symbol Search for the hub dialog and for agents: ?q=gold&cat=stock|fund|forex|crypto|index|commodity&broker=arm&offset=0&limit=50
            q = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
            try:
                off, lim = max(0, int(q.get("offset", 0))), min(500, max(1, int(q.get("limit", 50))))
            except ValueError:
                return self._json({"error": "offset and limit must be numbers"}, 400)
            return self._json(search_instruments(q.get("q", ""), q.get("cat", ""), q.get("broker", ""), off, lim), 200)
        if self.path.startswith("/api/instruments"):
            return self._json(build_instruments(), 200)
        if self.path.startswith("/api/health"):
            return self._json(health_report(), 200)
        if self.path.startswith("/api/orderflow/delta"):
            # Delta per candle for any chart symbol (orderflow_api.py; real Binance data or estimated from the feed).
            import orderflow_api
            q = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
            try:
                src = q.get("feed") or "binance"
                hist = history_fn(src)
                now = int(time.time())
                rows, info = orderflow_api.delta(q.get("symbol", REMOTE_PREFIX + "XAUUSD"), q.get("tf", "M5"), int(q.get("from", now - 86400)),
                                                 int(q.get("to", now)), q.get("source", "auto"), history=hist)
                return self._json({"rows": rows, **info}, 200)
            except Exception as e:
                logbus.ev("server", "warn", f"orderflow delta {q.get('symbol')} {q.get('tf')}: {e}", every=30)
                return self._json({"error": f"{e.__class__.__name__}: {e}"}, 502)
        if self.path.startswith("/api/orderflow/footprint"):
            # Footprint: buy and sell at each price row inside every candle (real trades or estimate), on the chart's prices.
            import orderflow_api
            q = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
            try:
                src = q.get("feed") or "binance"
                hist = history_fn(src)
                now = int(time.time()); tf = q.get("tf", "M5"); sym = q.get("symbol", REMOTE_PREFIX + "XAUUSD")
                a, b = int(q.get("from", now - 3 * 3600)), int(q.get("to", now))
                sec = orderflow_api.TF_SEC[tf]
                n = min(1000, max(5, (b - a) // sec + 5))
                cb = [(x["time"], x["high"], x["low"]) for x in hist(sym, tf, n, b * 1000) if a <= x["time"] <= b]
                rows, info = orderflow_api.footprint(sym, tf, a, b, q.get("source", "auto"), history=hist,
                                                     row_size=float(q.get("row_size", 0)), chart_bars=cb, fill=q.get("fill", "1") != "0")
                return self._json({"candles": rows, **info}, 200)
            except Exception as e:
                logbus.ev("server", "warn", f"orderflow footprint {q.get('symbol')} {q.get('tf')}: {e}", every=30)
                return self._json({"error": f"{e.__class__.__name__}: {e}"}, 502)
        if self.path.startswith("/api/orderflow/profile"):
            # Session volume profile (buy/sell by price, POC, value area, developing levels) on the chart's own prices.
            import orderflow_api
            q = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
            try:
                src = q.get("feed") or "binance"
                hist = history_fn(src)
                now = int(time.time())
                sess, info = orderflow_api.profile(q.get("symbol", REMOTE_PREFIX + "XAUUSD"), q.get("tf", "M5"), int(q.get("from", now - 86400)),
                                                   int(q.get("to", now)), q.get("source", "auto"), history=hist,
                                                   mode=q.get("mode", "sessions"), rows=int(q.get("rows", 24)),
                                                   row_size=float(q.get("row_size", 0)), va_pct=float(q.get("va", 70)) / 100,
                                                   developing=q.get("developing", "1") == "1")
                return self._json({"sessions": sess, **info}, 200)
            except Exception as e:
                logbus.ev("server", "warn", f"orderflow profile {q.get('symbol')} {q.get('tf')}: {e}", every=30)
                return self._json({"error": f"{e.__class__.__name__}: {e}"}, 502)
        if not self.path.startswith("/api/bars"):
            page = urlparse(self.path).path
            m = _PRIV_JS_RE.match(page)
            if m and PRIV:
                f = os.path.join(_priv_dir, "web", m.group(1))
                return self._file(f, "application/javascript; charset=utf-8") if os.path.isfile(f) else self._json({"error": "not found"}, 404)
            if not static_allowed(self.path):
                return self._json({"error": "not found"}, 404)
            extra = PRIV.scripts(page.lstrip("/")) if PRIV and page in ("/hub.html", "/vela.html") and hasattr(PRIV, "scripts") else []
            if extra:   # the private extension's page scripts go in just before </body>
                html = open(os.path.join(HERE, page.lstrip("/")), encoding="utf-8").read()
                tags = "".join(f'<script src="/private/{n}"></script>\n' for n in extra)
                i = html.rfind("</body>")
                html = html[:i] + tags + html[i:] if i >= 0 else html + tags
                return self._bytes(html.encode("utf-8"), "text/html; charset=utf-8")
            return super().do_GET()
        q = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
        try:
            fn = history_fn(q.get("source"))
            bars = fn(q.get("symbol", "BTCUSD"), q.get("tf", "M1"), q.get("limit", HISTORY), q.get("to"))
            if fn is arm_history and not bars and not q.get("to"):
                raise RuntimeError("no candles")
            body, code = json.dumps(bars).encode(), 200
            feedlog.http_bars(q.get("source"), q.get("symbol"), q.get("tf"), n=len(bars))
        except Exception as e:
            msg = str(e)
            if q.get("source") == "arm" and not q.get("to") and isinstance(e, RuntimeError) and ARM_DEAD_RE.search(msg):
                sym = arm_raw(q.get("symbol", ""))  # the VPS answered: this symbol has no data there (2nd time = hidden)
                msg = ARM_DEAD_MSG.format(sym) if arm_mark_nodata(sym, msg) else f"{sym}: no candles from {REMOTE_LABEL} yet, the terminal may still be downloading it"
            body, code = json.dumps({"error": msg}).encode(), 502
            feedlog.http_bars(q.get("source"), q.get("symbol"), q.get("tf"), err=f"{e.__class__.__name__}: {e}")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        """Only /api/restart (Restart button), /api/layouts/* (saved layouts), /api/feeds/* (source logins,
        this computer only) and the private extension's routes. Same-origin JSON only, so another web page
        open in the browser cannot act through this local server."""
        allowed = ("/api/restart", "/api/layouts/", "/api/feeds/", "/api/backtest/") + (tuple(PRIV.post_prefixes()) if PRIV and hasattr(PRIV, "post_prefixes") else ())
        if not self.path.startswith(allowed):
            return self._json({"error": "not found"}, 404)
        origin = self.headers.get("Origin", "")
        if origin and origin not in ORIGINS:
            return self._json({"error": "forbidden origin"}, 403)
        if "application/json" not in self.headers.get("Content-Type", ""):
            return self._json({"error": "JSON only"}, 415)
        n = int(self.headers.get("Content-Length") or 0)
        if n > (2_100_000 if self.path.startswith("/api/layouts/") else 300000):
            return self._json({"error": "too large"}, 413)
        body = self.rfile.read(n)
        if self.path.startswith("/api/layouts/"):
            code, obj = layouts_store.handle_post(self.path, body)
            return self._json(obj, code)
        if self.path.startswith("/api/feeds/"):
            # A plugin route that stores credentials or logs in: only this computer may call it.
            if self.client_address[0] not in ("127.0.0.1", "::1"):
                return self._json({"ok": False, "error": "only this computer can change feed logins"}, 403)
            from urllib.parse import urlparse
            parts = urlparse(self.path).path.split("/")
            pid, action = (parts[3] if len(parts) > 3 else ""), (parts[4] if len(parts) > 4 else "")
            try:
                payload = json.loads(body or b"{}")
            except ValueError:
                return self._json({"ok": False, "error": "invalid JSON"}, 400)
            if not isinstance(payload, dict):
                return self._json({"ok": False, "error": "JSON object expected"}, 400)
            try:
                return self._json(feeds.plugin_api(pid, action, payload), 200)
            except KeyError:
                return self._json({"ok": False, "error": f"no such source: {pid}"}, 404)
            except Exception as e:
                return self._json({"ok": False, "error": str(e)}, 400)
        if self.path.startswith("/api/backtest/"):   # start a run (the job runs in a background thread)
            from urllib.parse import urlparse
            action = urlparse(self.path).path.split("/")[3]
            try:
                payload = json.loads(body or b"{}")
            except ValueError:
                return self._json({"ok": False, "error": "invalid JSON"}, 400)
            if not isinstance(payload, dict):
                return self._json({"ok": False, "error": "JSON object expected"}, 400)
            try:
                import backtest
                return self._json(backtest.api(action, payload), 200)
            except KeyError:
                return self._json({"ok": False, "error": f"no such backtest route: {action}"}, 404)
            except Exception as e:
                return self._json({"ok": False, "error": f"{e.__class__.__name__}: {e}"}, 400)
        if self.path.startswith("/api/restart"):
            if not restart_state["running"]:
                restart_state["running"] = True  # set here so a double click cannot start two restarts
                threading.Thread(target=arm_restart, daemon=True).start()
            return self._json(restart_state, 200)
        if PRIV and hasattr(PRIV, "handle_post") and PRIV.handle_post(self, body):
            return
        return self._json({"error": "not found"}, 404)

    def _file(self, path, ctype):
        return self._bytes(open(path, "rb").read(), ctype)

    def _bytes(self, body, ctype):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _raw(self, code, raw):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def _json(self, obj, code):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def end_headers(self):
        # Pages and scripts change often while we build: never let the browser serve a stale copy.
        if not self.path.startswith("/api/"):
            self.send_header("Cache-Control", "no-cache, must-revalidate")
        super().end_headers()

    def log_message(self, *a):
        pass

class HubHTTPServer(ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        """A closed browser tab is not an error; anything else keeps its traceback."""
        import traceback
        e = sys.exc_info()[1]
        if isinstance(e, (ConnectionAbortedError, ConnectionResetError, BrokenPipeError)):
            logbus.ev("server", "debug", f"browser dropped a request ({e.__class__.__name__})", every=60, persist=False)
        else:
            logbus.ev("server.err", "error", traceback.format_exc())

def run_http():
    h = functools.partial(LabHandler, directory=HERE)
    HubHTTPServer((BIND_HOST, HTTP_PORT), h).serve_forever()

def ports_free_or_exit():
    """Before anything starts: both ports must be free. TradeX already running -> say where; another program -> say how to change."""
    import socket
    for port, what in ((HTTP_PORT, "HUB_HTTP_PORT"), (WS_PORT, "HUB_WS_PORT")):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)  # a just-stopped hub leaves TIME_WAIT sockets
        try:
            s.bind((BIND_HOST, port))
        except OSError:
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{HTTP_PORT}/api/restart", timeout=3)
                print(f"TradeX is already running: open http://localhost:{HTTP_PORT}/hub.html", flush=True)
                sys.exit(0)
            except Exception:
                print(f"Port {port} is used by another program. Close it, or set {what}=<another port> in .env", flush=True)
                sys.exit(1)
        finally:
            s.close()

async def main():
    ports_free_or_exit()
    logbus.install(lambda: CLOCK_OFFSET_MS)
    threading.Thread(target=run_http, daemon=True).start()
    threading.Thread(target=mt5_watchdog, daemon=True).start()
    if REMOTE_ON:   # remote MT5 set up in .env (REMOTE_MT5_SSH_HOST)
        threading.Thread(target=arm_symbols_loop, daemon=True).start()
        if REMOTE_TUNNEL:
            threading.Thread(target=arm_tunnel_loop, daemon=True).start()
    print(f"Clock offset vs time.windows.com: {measure_clock_offset():+.0f} ms (corrected in lag numbers)", flush=True)
    threading.Thread(target=deriv_symbols_loop, daemon=True).start()
    threading.Thread(target=bybit_symbols_loop, daemon=True).start()
    threading.Thread(target=binance_loop, daemon=True).start()
    threading.Thread(target=feeds.symbols_loop, daemon=True).start()   # chart-source plugins
    for _pid, _mod in feeds.enabled().items():                         # plugin background jobs (e.g. daily login)
        if hasattr(_mod, "start"):
            try:
                _mod.start()
            except Exception as _e:
                print(f"feeds: {_pid} start failed: {_e}", flush=True)
    feedlog.start(FEED_HEALTH, ARM_PORT if REMOTE_ON else None, feed_status)
    if PRIV and hasattr(PRIV, "start"):
        PRIV.start(globals())
    print(f"TradeX: open http://localhost:{HTTP_PORT}/  (data on ws://localhost:{WS_PORT})", flush=True)
    async with websockets.serve(handler, BIND_HOST, WS_PORT, max_size=None):
        await asyncio.Future()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
