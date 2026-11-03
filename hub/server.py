"""Chart Lab live data server.

Serves the test pages on http://localhost:8770 and streams candles over
ws://localhost:8765. Every chart connects to the same stream, so a chart
comparison measures the chart, not the feed.

Sources:
  deriv    public Deriv websocket: forex, gold, indices, crypto (no login)
  bybit    public Bybit market data (no login)
  binance  public crypto feed, no login (XAUUSD -> PAXGUSDT gold token, BTCUSD -> BTCUSDT)
  mt5      optional: a MetaTrader 5 terminal on this Windows PC (LOCAL_MT5=1 in .env)
  arm      optional: "Remote MT5", a MetaTrader 5 terminal on another machine through bridge/mt5_bridge.py
           and an SSH tunnel (REMOTE_MT5_SSH_HOST in .env), tickers RMT_<name>

All settings live in .env (see .env.example). Nothing private is needed for the public feeds.

Client -> server: {"source": "mt5"|"binance"|"deriv"|"arm"|"bybit", "symbol": "XAUUSD", "tf": "M1"}
Server -> client: {"type": "history", "bars": [...]}, then {"type": "bar", "bar": {...}, "sent": epoch_ms}
                  {"type": "error", "msg": "..."}
"""
import asyncio, json, subprocess, threading, time, urllib.request, functools, os, sys
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
import logbus, feedlog

HTTP_PORT = int(os.environ.get("HUB_HTTP_PORT", "8770"))
WS_PORT = int(os.environ.get("HUB_WS_PORT", "8765"))
LOCAL_MT5 = os.environ.get("LOCAL_MT5", "0") == "1"
MT5_TERMINAL_PATH = os.environ.get("MT5_TERMINAL_PATH", r"C:\Program Files\MetaTrader 5\terminal64.exe")
REMOTE_SSH_HOST = os.environ.get("REMOTE_MT5_SSH_HOST", "").strip()   # an alias from your ~/.ssh/config; empty = off
REMOTE_RESTART_CMD = os.environ.get("REMOTE_MT5_RESTART_CMD", "").strip()  # optional command run over SSH by the Restart button
REMOTE_ON = bool(REMOTE_SSH_HOST)
CACHE_DIR = os.path.join(HERE, ".cache")
TF_SEC = {"M1": 60, "M5": 300, "M15": 900, "M30": 1800, "H1": 3600, "H4": 14400, "D1": 86400}
BINANCE_TF = {"M1": "1m", "M5": "5m", "M15": "15m", "M30": "30m", "H1": "1h", "H4": "4h", "D1": "1d"}
BINANCE_SYM = {"XAUUSD": "PAXGUSDT"}  # gold token; any other XXXUSD maps to XXXUSDT

def bsym_of(symbol):
    s = symbol.upper().split(":")[-1]
    if s in BINANCE_SYM:
        return BINANCE_SYM[s]
    return s + "T" if s.endswith("USD") else s
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
    ok = mt5.initialize(path=MT5_TERMINAL_PATH, timeout=10000)
    if not ok or mt5.account_info() is None:
        mt5_fail.update(at=time.time(), msg=f"MT5 not logged in ({mt5.last_error()}). Open MetaTrader 5 and log in to your demo account.")
        return None, mt5_fail["msg"]
    mt5_ready = True
    return mt5, None

def mt5_resolve(mt5, symbol):
    for s in (symbol, symbol + "m", symbol + ".", symbol + "z"):  # broker suffixes, e.g. XAUUSDm
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

# ---------------- Remote MT5 (bridge/mt5_bridge.py on another machine, through an SSH tunnel) ----------------
# Internal source id "arm". Tickers carry an "RMT_" prefix so they never clash with other feeds' tickers.
ARM_HOST, ARM_PORT = "127.0.0.1", int(os.environ.get("REMOTE_MT5_BRIDGE_PORT", "8797"))
ARM_SYMBOLS_FILE = os.path.join(CACHE_DIR, "remote_symbols_cache.json")
arm_symbols_cache = []
arm_status = {"msg": "connecting to the remote MT5 bridge..." if REMOTE_ON else "off"}
arm_bars_cache = {}  # (symbol, tf, limit) -> last good candles: shown while the remote side reconnects, so the chart never goes blank
ARM_SSH = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15"]
arm_tunnel_proc = None
restart_state = {"running": False, "step": "", "ok": None, "at": 0}

def arm_tunnel_loop():
    """The chart server owns the SSH link to the remote bridge: it starts with the hub and comes back 5 s after any drop."""
    global arm_tunnel_proc
    while True:
        try:
            err_path = os.path.join(logbus.LOG_DIR, "ssh_link.err")
            err_file = open(err_path, "w", encoding="utf-8")
            arm_tunnel_proc = subprocess.Popen(ARM_SSH + ["-N", "-o", "ExitOnForwardFailure=yes", "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3",
                                                          "-L", f"{ARM_PORT}:127.0.0.1:{ARM_PORT}", REMOTE_SSH_HOST],
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
    """Drop the SSH link (Windows: whichever ssh holds the port). arm_tunnel_loop brings ours back."""
    if os.name != "nt":
        if arm_tunnel_proc:
            arm_tunnel_proc.kill()
        return
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
    """Restart button: fresh link, then (if REMOTE_MT5_RESTART_CMD is set) that command over SSH with argument
    "bridge", and if the bridge is still silent, with argument "full"."""
    def step(s):
        restart_state["step"] = s
        logbus.ev("restart", "info", s)
    def remote(mode, timeout):
        if REMOTE_RESTART_CMD:
            subprocess.run(ARM_SSH + ["-n", REMOTE_SSH_HOST, f"{REMOTE_RESTART_CMD} {mode}"], capture_output=True, timeout=timeout,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    def wait_ok(seconds):
        end = time.time() + seconds
        while time.time() < end:
            if arm_answers():
                return True
            time.sleep(2)
        return False
    restart_state.update(running=True, ok=None, at=time.time())
    try:
        step("Reconnecting the SSH link")
        arm_kill_tunnel()
        time.sleep(6)
        ok = wait_ok(15)
        if not ok and REMOTE_RESTART_CMD:
            step("Restarting the remote bridge")
            remote("bridge", 40)
            ok = wait_ok(15)
            if not ok:
                step("Bridge still silent: full restart on the remote machine (about 1 min)")
                remote("full", 150)
                ok = wait_ok(30)
        _status_cache["at"] = 0
        restart_state.update(ok=ok)
        step("Remote MT5 is back" if ok else "Remote MT5 did not come back. Check the remote machine over SSH.")
    except Exception as e:
        restart_state.update(ok=False)
        step(f"Restart failed: {e.__class__.__name__}: {e}")
    restart_state["running"] = False

def arm_get(path, conn=None, timeout=30):
    import http.client
    c = conn or http.client.HTTPConnection(ARM_HOST, ARM_PORT, timeout=timeout)
    c.request("GET", path)
    r = c.getresponse()
    data = json.loads(r.read())
    if conn is None:
        c.close()
    if isinstance(data, dict) and data.get("error"):
        raise RuntimeError(data["error"])
    return data

def arm_raw(ticker):
    return ticker[4:] if ticker.startswith("RMT_") else ticker

METAL_PREFIX = ("XAU", "XAG", "XPT", "XPD", "XAL", "XCU", "XNI", "XPB", "XZN")

def arm_group(name, path):
    """Group from the broker folder path (Forex, Metals, Crypto, Nasdaq ...)."""
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

def arm_symbols_loop():
    """Background: fetch the remote symbol list; retry every 30 s until it answers, then refresh every 10 min
    (so switching the remote MT5 to another account shows up without restarting this server)."""
    global arm_symbols_cache
    if not REMOTE_ON:
        return
    os.makedirs(CACHE_DIR, exist_ok=True)
    try:  # last known list, so remote charts exist even if the hub opens during an outage
        with open(ARM_SYMBOLS_FILE, encoding="utf-8") as f:
            arm_symbols_cache = json.load(f)
    except (OSError, ValueError):
        pass
    while True:
        try:
            rows = arm_get("/symbols", timeout=60)
            out = [{"ticker": "RMT_" + s["name"], "name": "Remote MT5 - " + (s["desc"] or s["name"]),
                    "source": "arm", "group": arm_group(s["name"], s["path"]), "digits": s["digits"]} for s in rows]
            out.sort(key=lambda x: (ARM_ORDER.index(x["group"]), x["ticker"]))
            if len(out) != len(arm_symbols_cache):
                print(f"Remote MT5: {len(out)} symbols", flush=True)
            arm_symbols_cache = out
            with open(ARM_SYMBOLS_FILE, "w", encoding="utf-8") as f:
                json.dump(out, f)
            arm_status["msg"] = "ok"
            time.sleep(600)
        except Exception as e:
            arm_status["msg"] = f"remote MT5 bridge unreachable ({e}). Is the SSH tunnel on port {ARM_PORT} running?"
            print("Remote MT5:", arm_status["msg"], flush=True)
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
        if key in arm_bars_cache:  # reconnecting: keep the chart on its last good candles
            logbus.ev("feed.arm", "warn", f"{symbol} {tf}: bridge not answering, serving last good candles", every=30)
            return arm_bars_cache[key]
        raise

async def stream_arm(ws, symbol, tf):
    import http.client
    from urllib.parse import quote
    while True:  # the remote side may be restarting: keep trying, the chart page stays open and fills in when it answers
        try:
            bars = await asyncio.to_thread(arm_history, symbol, tf)
            break
        except Exception as e:
            await ws.send(json.dumps({"type": "status", "msg": f"Remote MT5 reconnecting: {e}"}))
            await asyncio.sleep(3)
    await ws.send(json.dumps({"type": "history", "bars": bars, "source": "arm", "symbol": symbol, "tf": tf}))
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
            await ws.send(json.dumps({"type": "status", "msg": f"Remote MT5 reconnecting: {e}"}))
            await asyncio.sleep(3)

# ---------------- Binance (no login) ----------------
async def stream_binance(ws, symbol, tf):
    bsym = bsym_of(symbol)
    url = f"https://api.binance.com/api/v3/klines?symbol={bsym}&interval={BINANCE_TF[tf]}&limit={HISTORY}"
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
        feed = await websockets.connect(f"wss://data-stream.binance.vision/ws/{bsym.lower()}@aggTrade", open_timeout=10)
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

# ---------------- Bybit (public market data, no login; Bybit blocks some countries and cloud IPs) ----------------
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
CLOCK_OFFSET_MS = 0.0  # add to local time to get true time (measured at start)

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

def deriv_request(req):
    from websockets.sync.client import connect
    with connect(DERIV_URL, open_timeout=10) as c:
        c.send(json.dumps(req)); return json.loads(c.recv(timeout=20))

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

def deriv_history(symbol, tf, limit=HISTORY, to_ms=None):
    req = {"ticks_history": symbol, "style": "candles", "granularity": DERIV_GRAN[tf], "count": min(int(limit), 5000),
           "end": str(int(int(to_ms) / 1000)) if to_ms else "latest", "adjust_start_time": 1}
    r = deriv_request(req)
    if r.get("error"):
        raise RuntimeError(r["error"].get("message"))
    return [{"time": int(c["epoch"]), "open": float(c["open"]), "high": float(c["high"]), "low": float(c["low"]),
             "close": float(c["close"]), "volume": 0.0} for c in r.get("candles") or []]

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
            fn = {"mt5": stream_mt5, "deriv": stream_deriv, "arm": stream_arm, "bybit": stream_bybit}.get(src, stream_binance)
            task = feedlog.start_stream(fn, ws, src if fn is not stream_binance else "binance", sym, tf)
    except websockets.ConnectionClosed:
        pass
    finally:
        if task:
            task.cancel()

def binance_history(symbol, tf, limit=HISTORY, to_ms=None):
    """History for scroll-back. to_ms = newest bar open time wanted (epoch ms)."""
    bsym = bsym_of(symbol)
    url = f"https://api.binance.com/api/v3/klines?symbol={bsym}&interval={BINANCE_TF[tf]}&limit={min(int(limit), 1000)}"
    if to_ms:
        url += f"&endTime={int(to_ms)}"
    rows = json.loads(urllib.request.urlopen(url, timeout=15).read())
    return [{"time": k[0] // 1000, "open": float(k[1]), "high": float(k[2]), "low": float(k[3]),
             "close": float(k[4]), "volume": float(k[5])} for k in rows]

CRYPTO = [("BTCUSD", "Bitcoin"), ("ETHUSD", "Ethereum"), ("SOLUSD", "Solana"), ("BNBUSD", "BNB"),
          ("XRPUSD", "XRP"), ("DOGEUSD", "Dogecoin"), ("ADAUSD", "Cardano"), ("LINKUSD", "Chainlink"),
          ("PAXGUSD", "Gold token PAXG (thin trading, not gold spot)")]
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
    """Cached 60 s: every request used to call into the local MT5, and one stuck call froze the whole server."""
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

# ---------------- Instrument map (Chart Hub: switch broker, stay on the same instrument) ----------------
# One instrument = the same underlying market at several brokers. Only exact matches are joined:
# gold tokens (PAXG, XAUT) are NOT gold spot, so they stay separate instruments.
BROKERS = [("deriv", "Deriv"), ("bybit", "Bybit"), ("binance", "Binance")] + ([("arm", "Remote MT5")] if REMOTE_ON else []) + ([("mt5", "Local MT5")] if LOCAL_MT5 else [])
DERIV_INDEX = {"OTC_NDX": "USTEC", "OTC_DJI": "US30", "OTC_SPC": "US500", "OTC_FTSE": "UK100", "OTC_GDAXI": "DE30",
               "OTC_FCHI": "FR40", "OTC_N225": "JP225", "OTC_HSI": "HK50", "OTC_AS51": "AUS200", "OTC_SX5E": "STOXX50"}
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
    if src == "arm":
        return t[4:], None
    if src == "deriv":
        if t in DERIV_INDEX: return DERIV_INDEX[t], None
        if t[:3] in ("frx", "cry"): return t[3:], None
        return t, None
    if src == "mt5":
        return t, None
    if src == "binance":
        return t, None
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

def build_instruments():
    syms = arm_symbols_cache + deriv_symbols_cache + bybit_symbols_cache + mt5_symbol_list()[0] + \
        [{"ticker": t, "name": n, "source": "binance", "group": "crypto"} for t, n in CRYPTO]
    if _instr_cache["n"] == len(syms):
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
            i["name"] = s["name"].replace("Remote MT5 - ", "").split(" (")[0]
            if s["source"] == "arm":
                i["group"] = s["group"].replace("arm-", "")
    out = []
    for i in inst.values():
        for src, opts in i["brokers"].items():
            opts.sort(key=lambda o: (o["_o"], o["label"]))
            for o in opts: o.pop("_o")
        out.append(i)
    rank = {"metal": 0, "crypto": 1, "forex": 2, "index": 3, "energy": 4, "stock": 6}
    out.sort(key=lambda i: (-len(i["brokers"]), rank.get(i["group"], 5), i["key"]))
    data = {"brokers": [{"id": b, "name": n} for b, n in BROKERS], "instruments": out}
    _instr_cache.update(n=len(syms), data=data)
    return data

_status_cache = {"at": 0, "data": None}

def feed_status():
    """Small, cached (5 s) health of each broker feed for the Chart Hub status lights."""
    if time.time() - _status_cache["at"] < 5:
        return _status_cache["data"]
    data = {"deriv": {"ok": bool(deriv_symbols_cache), "msg": f"{len(deriv_symbols_cache)} symbols"},
            "bybit": {"ok": bool(bybit_symbols_cache), "msg": f"{len(bybit_symbols_cache)} charts"},
            "binance": {"ok": True, "msg": "public feed"}}
    if REMOTE_ON:
        try:
            h = arm_get("/health", timeout=3)
            data["arm"] = {"ok": True, "msg": f"connected ({h.get('server') or 'MT5'})"}
        except Exception as e:
            data["arm"] = {"ok": False, "msg": f"link down ({e.__class__.__name__}); it reconnects by itself"}
    if LOCAL_MT5:
        data["mt5"] = {"ok": bool(mt5_ready), "msg": "connected" if mt5_ready else (mt5_fail.get("msg") or "not connected")}
    for k, v in data.items():  # one log line per change of a status light
        logbus.state("feed." + k, "ok", v["ok"], "info" if v["ok"] else "error" if k == "arm" else "warn", ("up: " if v["ok"] else "DOWN: ") + v["msg"])
        logbus.problem("feed." + k, None if v["ok"] else v["msg"])
    _status_cache.update(at=time.time(), data=data)
    return data

# Only these files are served. Everything else (.env, logs, caches, tests, Python sources) answers 404,
# so a secret placed in this folder can never be read through the browser port.
STATIC_EXT = (".html", ".js", ".css", ".png", ".svg", ".ico", ".woff", ".woff2")

def static_allowed(url_path):
    from urllib.parse import urlparse, unquote
    p = unquote(urlparse(url_path).path)
    if p in ("/", ""):
        return True
    parts = [x for x in p.split("/") if x]
    if any(x.startswith(".") for x in parts) or "\\" in p:
        return False
    if len(parts) == 1 or (len(parts) == 2 and parts[0] == "vendor"):
        return parts[-1].lower().endswith(STATIC_EXT)
    return False

class LabHandler(SimpleHTTPRequestHandler):
    """Static files plus GET /api/bars?symbol=&tf=&limit=&to=<ms>&source=binance|mt5 and GET /api/symbols."""
    def do_GET(self):
        from urllib.parse import urlparse, parse_qs
        if self.path.startswith("/api/symbols"):
            crypto = [{"ticker": t, "name": n, "source": "binance", "group": "crypto"} for t, n in CRYPTO]
            mt5_list, err = mt5_symbol_list()
            return self._json({"symbols": arm_symbols_cache + bybit_symbols_cache + deriv_symbols_cache + mt5_list + crypto, "mt5": err or "ok", "arm": arm_status["msg"],
                               "deriv": len(deriv_symbols_cache), "bybit": len(bybit_symbols_cache), "ws_port": WS_PORT}, 200)
        if self.path.startswith("/api/restart"):
            return self._json(restart_state, 200)
        if self.path.startswith("/api/logs"):
            q = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
            since = q.get("since", "0")
            return self._json({**logbus.snapshot(int(since) if since.isdigit() else 0, brief="brief" in q), "journal_raw": False}, 200)
        if self.path.startswith("/api/status"):
            return self._json(feed_status(), 200)
        if self.path.startswith("/api/instruments"):
            return self._json(build_instruments(), 200)
        if self.path.startswith("/api/health"):
            return self._json(health_report(), 200)
        if not self.path.startswith("/api/bars"):
            if not static_allowed(self.path):
                return self._json({"error": "not found"}, 404)
            return super().do_GET()
        q = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
        try:
            fn = {"mt5": mt5_history, "deriv": deriv_history, "arm": arm_history, "bybit": bybit_history}.get(q.get("source"), binance_history)
            bars = fn(q.get("symbol", "BTCUSD"), q.get("tf", "M1"), q.get("limit", HISTORY), q.get("to"))
            body, code = json.dumps(bars).encode(), 200
            feedlog.http_bars(q.get("source"), q.get("symbol"), q.get("tf"), n=len(bars))
        except Exception as e:
            body, code = json.dumps({"error": str(e)}).encode(), 502
            feedlog.http_bars(q.get("source"), q.get("symbol"), q.get("tf"), err=f"{e.__class__.__name__}: {e}")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        """Only /api/restart (Restart button) and /api/logs/journal (log panel).
        Same-origin JSON only, so another web page open in the browser cannot act through this local server."""
        if not self.path.startswith(("/api/restart", "/api/logs/journal")):
            return self._json({"error": "not found"}, 404)
        origin = self.headers.get("Origin", "")
        if origin and origin not in (f"http://localhost:{HTTP_PORT}", f"http://127.0.0.1:{HTTP_PORT}"):
            return self._json({"error": "forbidden origin"}, 403)
        if "application/json" not in self.headers.get("Content-Type", ""):
            return self._json({"error": "JSON only"}, 415)
        n = int(self.headers.get("Content-Length") or 0)
        if n > 300000:
            return self._json({"error": "too large"}, 413)
        if self.path.startswith("/api/logs/journal"):
            self.rfile.read(n)
            return self._json({"ok": True}, 200)
        if self.path.startswith("/api/restart"):
            self.rfile.read(n)
            if not REMOTE_ON:
                return self._json({"running": False, "step": "", "ok": None}, 200)
            if not restart_state["running"]:
                restart_state["running"] = True  # set here so a double click cannot start two restarts
                threading.Thread(target=arm_restart, daemon=True).start()
            return self._json(restart_state, 200)
        return self._json({"error": "not found"}, 404)

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
    HubHTTPServer(("127.0.0.1", HTTP_PORT), h).serve_forever()

async def main():
    logbus.install(lambda: CLOCK_OFFSET_MS)
    threading.Thread(target=run_http, daemon=True).start()
    threading.Thread(target=mt5_watchdog, daemon=True).start()
    if REMOTE_ON:
        threading.Thread(target=arm_symbols_loop, daemon=True).start()
        threading.Thread(target=arm_tunnel_loop, daemon=True).start()
    print(f"Clock offset vs time.windows.com: {measure_clock_offset():+.0f} ms (corrected in lag numbers)", flush=True)
    threading.Thread(target=deriv_symbols_loop, daemon=True).start()
    threading.Thread(target=bybit_symbols_loop, daemon=True).start()
    feedlog.start(FEED_HEALTH, ARM_PORT if REMOTE_ON else None, feed_status)
    print(f"Chart Hub: open http://localhost:{HTTP_PORT}/hub.html  (data on ws://localhost:{WS_PORT})", flush=True)
    async with websockets.serve(handler, "127.0.0.1", WS_PORT, max_size=None):
        await asyncio.Future()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
