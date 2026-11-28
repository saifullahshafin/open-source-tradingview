"""Shared helpers for chart-source plugins (see feeds/__init__.py for the plugin contract)."""
import asyncio, json, ssl, time, urllib.request

TF_SEC = {"M1": 60, "M5": 300, "M15": 900, "M30": 1800, "H1": 3600, "H4": 14400, "D1": 86400}
UA = {"User-Agent": "TradeX/1.0 (+https://github.com/saifullahshafin/open-source-tradingview)"}

def _ssl_context():
    """Use the operating system's certificate store when truststore is installed (fixes TLS errors on some PCs)."""
    try:
        import truststore
        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except Exception:
        return ssl.create_default_context()

_CTX = _ssl_context()

def get_json(url, timeout=15, data=None, headers=None):
    h = dict(UA, **(headers or {}))
    body = None
    if data is not None:
        body = json.dumps(data).encode()
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=h)
    with urllib.request.urlopen(req, timeout=timeout, context=_CTX) as r:
        return json.loads(r.read())

def bar(t, o, h, l, c, v=0.0):
    return {"time": int(t), "open": float(o), "high": float(h), "low": float(l), "close": float(c), "volume": float(v)}

def resample(bars, src_tf, dst_tf):
    """Join bars of src_tf into dst_tf (for exchanges without 30m or 4h candles). Bars must be oldest first."""
    span, out = TF_SEC[dst_tf], []
    for b in bars:
        start = b["time"] - b["time"] % span
        if out and out[-1]["time"] == start:
            x = out[-1]
            x["high"] = max(x["high"], b["high"]); x["low"] = min(x["low"], b["low"])
            x["close"] = b["close"]; x["volume"] += b["volume"]
        else:
            out.append(dict(b, time=start))
    return out

def poll_interval(tf, every=None):
    """How often a REST source has to be asked again for one symbol. A candle only changes when its
    own timeframe closes, so polling M5 every 2 s (the old default) spent 30 calls a minute per chart
    and hit the broker's per-minute cap with three charts open. Cap at 30 s."""
    if every:
        return float(every)
    return max(2.0, min(30.0, TF_SEC.get(tf, 60) / 20.0))


class _Shared:
    """One poller and one upstream request stream per (source, symbol, timeframe, bars), shared by
    every chart watching it. Charts subscribe, get the same bars, and cost no extra API calls."""

    def __init__(self, key, history_fn, symbol, tf, source, history, every):
        self.key, self.history_fn, self.symbol, self.tf = key, history_fn, symbol, tf
        self.source, self.history, self.every = source, history, every
        self.clients = set()          # one asyncio.Queue per open chart
        self.ready = asyncio.Event()
        self.error = None
        self.bars = []
        self.last = None
        self.task = None

    def send(self, msg):
        for q in list(self.clients):
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                pass                  # a chart that stopped reading must not stall the others


_shared = {}


async def _pump(sh):
    """Fetch history once, then poll the newest bars on this channel's interval and fan them out."""
    try:
        bars = await asyncio.to_thread(sh.history_fn, sh.symbol, sh.tf, sh.history, None)
    except Exception as e:
        sh.error = f"{sh.source} history failed: {e}"
        sh.ready.set()
        return
    if not bars:
        sh.error = f"{sh.source}: no candles for {sh.symbol} {sh.tf}"
        sh.ready.set()
        return
    sh.bars = bars
    sh.last = dict(bars[-1])
    sh.ready.set()
    every = poll_interval(sh.tf, sh.every)
    fails = 0
    while True:
        await asyncio.sleep(every)
        try:
            new = await asyncio.to_thread(sh.history_fn, sh.symbol, sh.tf, 3, None)
            fails = 0
        except Exception as e:
            fails += 1
            if fails in (3, 30):
                sh.send(json.dumps({"type": "status", "msg": f"{sh.source} not answering ({e.__class__.__name__}); retrying"}))
            await asyncio.sleep(min(30, every * fails))
            continue
        span = TF_SEC.get(sh.tf, 60) * 1000
        for b in new or []:
            if b["time"] < sh.last["time"] or b == sh.last:
                continue
            if b["time"] - sh.last["time"] > 2 * span:
                # Candles were missed (the market was closed, or a fetch failed): resend the whole
                # window instead of a bar that would leave a hole in the chart.
                await _rebroadcast(sh)
                break
            sh.last = dict(b)
            now = int(time.time() * 1000)
            sh.send(json.dumps({"type": "bar", "bar": b, "sent": now, "trade_ms": now, "lag_ms": None}))


async def _rebroadcast(sh):
    try:
        bars = await asyncio.to_thread(sh.history_fn, sh.symbol, sh.tf, sh.history, None)
    except Exception:
        return
    if not bars:
        return
    sh.bars = bars
    sh.last = dict(bars[-1])
    sh.send(json.dumps({"type": "history", "bars": bars, "source": sh.source, "symbol": sh.symbol, "tf": sh.tf}))


async def poll_stream(ws, history_fn, symbol, tf, source, history=1000, every=None):
    """Send history, then the live bars of the shared poller for this symbol. Every chart on the same
    symbol and timeframe subscribes to one poller, so twenty tabs cost one upstream request stream,
    and the interval follows the timeframe (M1 3 s, M5 15 s, H1 30 s) instead of a flat 2 s.
    Works for any REST-only source; plugins with a websocket can provide their own stream()."""
    key = (source, symbol, tf, int(history))
    sh = _shared.get(key)
    if sh is None or sh.task is None or sh.task.done():
        sh = _Shared(key, history_fn, symbol, tf, source, history, every)
        _shared[key] = sh
        sh.task = asyncio.create_task(_pump(sh))
    q = asyncio.Queue(maxsize=256)
    sh.clients.add(q)
    try:
        await sh.ready.wait()
        if sh.error:
            await ws.send(json.dumps({"type": "error", "msg": sh.error}))
            return
        await ws.send(json.dumps({"type": "history", "bars": sh.bars, "source": source, "symbol": symbol, "tf": tf}))
        while True:
            await ws.send(await q.get())
    finally:
        sh.clients.discard(q)
        if not sh.clients:
            if sh.task is not None:
                sh.task.cancel()
            if _shared.get(key) is sh:
                _shared.pop(key, None)


# ---- testable surface ----

async def _subscribe(sh):
    """Return a queue joined to shared poller `sh`, as poll_stream would for one chart."""
    q = asyncio.Queue(maxsize=256)
    sh.clients.add(q)
    await sh.ready.wait()
    return q


def _stage_history(sh, bars):
    """Bypass the network: seed a shared poller with history and set it ready. The poller's task still
    runs and will poll sh.history_fn on its interval, so this is only for tests that also override the
    poller's history_fn to a controlled no-op or fake."""
    sh.bars = list(bars)
    sh.last = dict(bars[-1]) if bars else None
    sh.ready.set()


def _clear_shared():
    """Reset the shared poller registry between tests."""
    _shared.clear()
