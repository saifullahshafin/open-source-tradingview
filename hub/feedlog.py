"""Per-feed facts for the hub log, without editing each stream loop.

A chart stream sends everything through its WebSocket, so a thin wrapper on that WebSocket sees bars, reconnect
messages and errors for every provider. It only counts; a ~25 char prefix check per send, nothing that can block.
"""
import asyncio, json, socket, threading, time
import logbus

SUMMARY_S = 60
STREAM_SILENT_S = 120
STATUS_S = 30
FEEDS = {}  # provider -> counters since the last summary
ACTIVE = {}  # id -> Stream


def _feed(src):
    return FEEDS.setdefault(src, {"updates": 0, "history_bars": 0, "http_bars": 0, "reconnects": 0, "errors": 0})


class CountingWS:
    def __init__(self, ws, stream):
        self._ws, self._s = ws, stream

    async def send(self, text):
        await self._ws.send(text)
        self._s.seen(text)

    def __getattr__(self, name):
        return getattr(self._ws, name)


class Stream:
    def __init__(self, src, symbol, tf):
        self.src, self.symbol, self.tf = src, symbol, tf
        self.label = f"{symbol} {tf}"
        self.tag = "feed." + src
        self.started = self.last_data = time.time()
        self.silent = False

    def seen(self, text):
        f = _feed(self.src)
        if text.startswith('{"type": "bar"'):
            f["updates"] += 1
            self.last_data = time.time()
            if self.silent:
                self.silent = False
                logbus.problem(f"{self.tag}:{self.label}")
                logbus.ev(self.tag, "info", f"{self.label}: data flowing again")
            return
        try:
            m = json.loads(text)
        except ValueError:
            return
        kind = m.get("type")
        if kind == "history":
            n = len(m.get("bars") or [])
            f["history_bars"] += n
            self.last_data = time.time()
            logbus.ev(self.tag, "info", f"{self.label}: history served, {n} bars")
        elif kind == "status":
            f["reconnects"] += 1
            logbus.ev(self.tag, "warn", f"{self.label}: {m.get('msg')}", every=30)
        elif kind == "error":
            f["errors"] += 1
            logbus.ev(self.tag, "error", f"{self.label}: {m.get('msg')}", every=30)


def start_stream(fn, ws, src, symbol, tf):
    """Used by server.handler in place of create_task(fn(ws, symbol, tf))."""
    st = Stream(src, symbol, tf)
    ACTIVE[id(st)] = st
    logbus.ev(st.tag, "info", f"{st.label}: chart stream opened")

    def done(task):
        ACTIVE.pop(id(st), None)
        logbus.problem(f"{st.tag}:{st.label}")
        if task.cancelled():
            logbus.ev(st.tag, "info", f"{st.label}: chart stream closed")
            return
        e = task.exception()
        if e and e.__class__.__name__ not in ("ConnectionClosedOK", "ConnectionClosedError", "ConnectionClosed"):
            _feed(src)["errors"] += 1
            logbus.ev(st.tag, "error", f"{st.label}: stream died: {e.__class__.__name__}: {e}")
        else:
            logbus.ev(st.tag, "info", f"{st.label}: chart stream ended")

    task = asyncio.create_task(fn(CountingWS(ws, st), symbol, tf))
    task.add_done_callback(done)
    return task


def http_bars(src, symbol, tf, n=None, err=None):
    src = src or "binance"
    if err is None:
        _feed(src)["http_bars"] += n or 0
    else:
        _feed(src)["errors"] += 1
        logbus.ev("feed." + src, "error", f"{symbol} {tf}: bars request failed: {err}", every=30)


def _check_silent():
    now = time.time()
    for st in list(ACTIVE.values()):
        age = now - st.last_data
        if age > STREAM_SILENT_S and not st.silent:
            st.silent = True
            logbus.ev(st.tag, "warn", f"{st.label}: no data for {int(age)} s (market closed, or the feed is dead)")
            logbus.problem(f"{st.tag}:{st.label}", f"no data for over {STREAM_SILENT_S} s")


def _summary(health, seconds):
    """One line per feed that did anything. health = server.FEED_HEALTH; ticks come from record_tick."""
    streams = {}
    for st in list(ACTIVE.values()):
        streams.setdefault(st.src, []).append(st)
    for src in sorted(set(FEEDS) | set(streams)):
        f, sts = _feed(src), streams.get(src, [])
        if not sts and not any(f.values()):
            continue
        ticks, lags = 0, []
        for k, h in list(health.items()):
            if k.startswith(src + ":"):
                ticks += h["ticks"] - h.get("_logged", 0)
                h["_logged"] = h["ticks"]
                lags += h["lags"][-300:]
        lags.sort()
        lag = f"lag_med={round(lags[len(lags) // 2])}ms lag_p95={round(lags[int(len(lags) * .95)])}ms" if lags else "lag=n/a"
        last = f"last_data={int(time.time() - max(s.last_data for s in sts))}s" if sts else "last_data=n/a"
        names = ",".join(s.label.replace(" ", ":") for s in sts) or "none"
        logbus.ev("feed." + src, "info", f"{int(seconds)} s: streams={len(sts)} [{names}] updates={f['updates']} ticks/s={ticks / seconds:.1f} {lag} "
                                         f"bars_served={f['history_bars'] + f['http_bars']} reconnects={f['reconnects']} errors={f['errors']} {last}")
        f.update(updates=0, history_bars=0, http_bars=0, reconnects=0, errors=0)


def _port_open(port):
    try:
        socket.create_connection(("127.0.0.1", port), timeout=1).close()
        return True
    except OSError:
        return False


def loop(health, link_port, status_fn):
    """Link state, silent streams, and the once-a-minute summary. Runs in its own thread."""
    last_summary = last_status = time.time()
    while True:
        try:
            up = _port_open(link_port) if link_port else None
            if link_port and logbus.state("link", "port", up, "info" if up else "error",
                            f"SSH link UP: tunnel answers on 127.0.0.1:{link_port}" if up else f"SSH link DOWN: nothing listening on 127.0.0.1:{link_port}"):
                logbus.problem("link", None if up else "SSH link to the remote MT5 bridge is down")
            _check_silent()
            if time.time() - last_status >= STATUS_S:  # so status light changes are logged with no hub tab open
                last_status = time.time()
                threading.Thread(target=status_fn, daemon=True, name="feed-status").start()
            if time.time() - last_summary >= SUMMARY_S:
                _summary(health, time.time() - last_summary)
                last_summary = time.time()
        except Exception as e:
            logbus.ev("hub.log", "error", f"feed log: {e.__class__.__name__}: {e}", every=60)
        time.sleep(2)


def start(health, link_port, status_fn):
    threading.Thread(target=loop, args=(health, link_port, status_fn), daemon=True, name="feed-log").start()
