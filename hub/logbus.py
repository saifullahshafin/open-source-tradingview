"""One backend log for the Chart Hub: an in-memory ring (served by /api/logs) plus a rotating file (logs/hub.log).

Every source goes through ev(): server prints (Tee), feed events (feedlog.py), optional local log files.
Never touches the chart path: no locks shared with feeds, ev() only appends to a deque and a small file.
All times are UTC ms. Everything is redacted before it is stored.
"""
import collections, datetime as dt, json, logging, logging.handlers, os, re, stat, sys, threading, time

HERE = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(HERE, "logs")
HUB_LOG = os.path.join(LOG_DIR, "hub.log")
OFFSETS = os.path.join(LOG_DIR, "offsets.json")
BOOT = int(time.time())
LEVELS = ("debug", "info", "warn", "error")
STD_CAP = 5_000_000  # server.log / server.err.log are cut back at this size; the lines are already in hub.log

RING = collections.deque(maxlen=12000)
SOURCES = {}  # src -> {"last": ms of newest line or file write, "expect": s or None, "state": ok|silent|reader down, "note": str}
_lock = threading.Lock()
_seq = 0
_states, _throttle = {}, {}
_file = None
clock_offset_ms = lambda: 0.0  # server.py points this at its measured clock offset


def now_ms():
    return time.time() * 1000 + clock_offset_ms()

# ---- Secrets never reach a log line. ----
_REDACT = [
    (re.compile(r"(?i)\b(bearer)\s+\S+"), r"\1 [redacted]"),
    (re.compile(r"(?i)\b(authorization|token|api[_-]?key|apikey|secret|passw(?:or)?d|pwd)\b(\s*[:=]\s*|\s+)\S+"), r"\1=[redacted]"),
    (re.compile(r"\b[0-9a-fA-F]{32,}\b"), "[redacted]"),
    (re.compile(r"\b[A-Za-z0-9+_-]{40,}={0,2}"), "[redacted]"),
]


def redact(text):
    for rx, sub in _REDACT:
        text = rx.sub(sub, text)
    return text


def _open_file():
    global _file
    os.makedirs(LOG_DIR, exist_ok=True)
    h = logging.handlers.RotatingFileHandler(HUB_LOG, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    h.setFormatter(logging.Formatter("%(message)s"))
    _file = logging.getLogger("hub.backend")
    _file.propagate = False
    _file.setLevel(logging.INFO)
    _file.addHandler(h)


def iso(ms):
    return dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def ev(src, level, msg, t_ms=None, every=0, persist=True, alive=True, **facts):
    """Add one line. every=N drops the same src+msg for N seconds (retry loops log once, not forever).
    alive=False: the line is about the source, not from it, so it does not count as the source writing."""
    global _seq
    if facts:
        msg = f"{msg} " + " ".join(f"{k}={v}" for k, v in facts.items())
    msg = redact(str(msg))[:2000]
    now = now_ms()
    t = t_ms if t_ms is not None else now
    with _lock:
        if every:
            k = (src, msg)
            if now - _throttle.get(k, 0) < every * 1000:
                return
            if len(_throttle) > 2000:
                _throttle.clear()
            _throttle[k] = now
        _seq += 1
        RING.append({"id": _seq, "t": int(t), "src": src, "lvl": level, "msg": msg})
        s = SOURCES.setdefault(src, {"last": 0, "expect": None, "state": "ok", "note": ""})
        if alive:
            s["last"] = max(s["last"], t)
    if persist and _file:
        try:
            _file.info("%s %-5s %s %s", iso(t), level, src, msg.replace("\n", " "))
        except Exception:
            pass


def state(src, key, value, level, msg, **facts):
    """Log only when value changes: one line per state change, never one per retry."""
    k = (src, key)
    if _states.get(k) == value:
        return False
    _states[k] = value
    ev(src, level, msg, **facts)
    return True


def reload_history(limit=4000):
    """After a server restart the terminal still shows what happened before it (ids are new, BOOT tells the page)."""
    global _seq
    try:
        with open(HUB_LOG, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 1_000_000))
            rows = f.read().decode("utf-8", "replace").splitlines()[1:][-limit:]
    except OSError:
        return
    rx = re.compile(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3})Z (\w+)\s+(\S+) (.*)$")
    for r in rows:
        m = rx.match(r)
        if m:
            t = dt.datetime.strptime(m[1], "%Y-%m-%dT%H:%M:%S.%f").replace(tzinfo=dt.timezone.utc).timestamp() * 1000
            _seq += 1
            RING.append({"id": _seq, "t": int(t), "src": m[3], "lvl": m[2], "msg": m[4]})
            s = SOURCES.setdefault(m[3], {"last": 0, "expect": None, "state": "ok", "note": ""})
            s["last"] = max(s["last"], t)

# ---- Parsing the formats the backend logs use. ----
_TS = re.compile(r"^(?:-- )?(\d{4}-\d\d-\d\d)[ T](\d\d:\d\d:\d\d)(\.\d+)?\s*(?:UTC|Z)?\s*(.*)$")
_ERR = re.compile(r"(?i)\b(\w*error|errors|failed|failure|traceback|exception|fatal|refused|crash\w*)\b")
_WARN = re.compile(r"(?i)\b(warn\w*|watchdog|restart\w*|silent|timeout|timed out|retry\w*|unreachable|denied|lost)\b")


def to_ms(day, hms, frac=""):
    t = dt.datetime.strptime(f"{day} {hms}", "%Y-%m-%d %H:%M:%S").replace(tzinfo=dt.timezone.utc).timestamp()
    return (t + float(frac or 0)) * 1000


def classify(text):
    return "error" if _ERR.search(text) else "warn" if _WARN.search(text) else "info"


def parse_line(line):
    """Returns (t_ms or None, text)."""
    m = _TS.match(line)
    return (to_ms(m[1], m[2], m[3]), m[4]) if m else (None, line)

# ---- Offsets survive a server restart, so old lines are not read twice. ----
_offsets = None


def offsets():
    global _offsets
    if _offsets is None:
        try:
            _offsets = json.load(open(OFFSETS, encoding="utf-8"))
        except (OSError, ValueError):
            _offsets = {}
    return _offsets


def save_offsets():
    try:
        tmp = OFFSETS + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_offsets or {}, f)
        os.replace(tmp, OFFSETS)
    except OSError:
        pass

# ---- Local log files to tail into the panel: HUB_TAIL_FILES="name=path;name2=path2" in .env ----
LOCAL_FILES = dict(x.split("=", 1) for x in os.environ.get("HUB_TAIL_FILES", "").split(";") if "=" in x)


def tail_local():
    off = offsets()
    for src, path in LOCAL_FILES.items():
        try:
            st = os.stat(path)
            key = "local:" + src
            pos = off.get(key)
            if pos is None or pos > st.st_size:
                pos = max(0, st.st_size - 8000) if pos is None else 0
            SOURCES.setdefault(src, {"last": 0, "expect": None, "state": "ok", "note": ""})
            SOURCES[src]["last"] = max(SOURCES[src]["last"], st.st_mtime * 1000)
            if st.st_size == pos:
                off[key] = pos
                continue
            with open(path, "rb") as f:
                f.seek(pos)
                data = f.read(256000)
            end = data.rfind(b"\n") + 1
            head_cut = pos > 0 and key not in off  # first look started mid-file: drop the partial first line
            off[key] = pos + end
            lines = data[:end].decode("utf-8", "replace").splitlines()[1 if head_cut else 0:]
            last_t = st.st_mtime * 1000
            for ln in lines:  # these two logs put the time in a "-- ...Z" header line; body lines carry it forward
                if ln.startswith("-- "):
                    last_t = parse_line(ln)[0] or last_t
                    continue
                t, text = parse_line(ln)
                if text.strip():
                    ev(src, classify(text), text, t_ms=t or last_t)
        except OSError as e:
            ev(src, "error", f"cannot read {os.path.basename(path)}: {e}", every=300, alive=False)

# ---- Silence: a dead source writes nothing, so its silence is the problem. ----
PROBLEMS = {}  # src -> text


def problem(src, text=None):
    if text:
        PROBLEMS[src] = text
    else:
        PROBLEMS.pop(src, None)


def check_silence():
    now = now_ms()
    for src, s in list(SOURCES.items()):
        if not s.get("expect") or s.get("state") == "reader down":
            continue
        age = (now - s["last"]) / 1000 if s["last"] else None
        if age is not None and age > s["expect"]:
            if s["state"] != "silent":
                s["state"] = "silent"
                ev(src, "error", f"SILENT: nothing written for {int(age)} s (expected at least every {s['expect']} s)", alive=False)
            problem(src, f"silent for {int(age)} s")
        elif s["state"] == "silent":
            s["state"] = "ok"
            problem(src)
            ev(src, "info", "writing again", alive=False)


def expect(src, seconds, note=""):
    s = SOURCES.setdefault(src, {"last": 0, "expect": None, "state": "ok", "note": ""})
    s["expect"], s["note"] = seconds, note


def watch_loop():
    while True:
        try:
            tail_local()
            check_silence()
            save_offsets()
        except Exception as e:
            ev("hub.log", "error", f"log watcher: {e.__class__.__name__}: {e}", every=60)
        time.sleep(5)

# ---- /api/logs ----


def snapshot(since=0, limit=3000, brief=False):
    now = now_ms()
    with _lock:
        rows = [r for r in RING if r["id"] > since] if not brief else []
        last = _seq
        recent = [r for r in RING if r["lvl"] == "error" and now - r["t"] < 900_000]
        srcs = {k: dict(v) for k, v in SOURCES.items()}
    latest = recent[-1] if recent else None
    out = {"boot": BOOT, "last": last, "now": int(now), "errors_15m": len(recent), "latest_error": latest,
           "problems": dict(PROBLEMS), "sources": srcs}
    if not brief:
        out["lines"] = rows[-limit:]
    return out

# ---- Server prints: same stream as before, every line also goes in the ring. ----


class Tee:
    """Wraps sys.stdout/sys.stderr. Passes writes through unchanged; a redirected file is cut back at STD_CAP."""

    def __init__(self, stream, src, default=None):
        self.stream, self.src, self.default, self.buf, self.lock = stream, src, default, "", threading.Lock()

    def write(self, s):
        try:
            if self.stream:
                self.stream.write(s)
        except Exception:
            pass
        with self.lock:
            self.buf += s
            lines = self.buf.split("\n")
            self.buf = lines.pop()
        for ln in lines:
            if ln.strip():
                ev(self.src, self.default or classify(ln), ln)
        return len(s)

    def flush(self):
        try:
            if self.stream:
                self.stream.flush()
                self._cap()
        except Exception:
            pass

    def _cap(self):
        fd = self.stream.fileno()
        st = os.fstat(fd)
        if stat.S_ISREG(st.st_mode) and st.st_size > STD_CAP:
            os.ftruncate(fd, 0)
            os.lseek(fd, 0, 0)

    def __getattr__(self, name):
        return getattr(self.stream, name)


def install(clock=None):
    """Call once at server start."""
    global clock_offset_ms
    if clock:
        clock_offset_ms = clock
    _open_file()
    reload_history()
    sys.stdout = Tee(sys.stdout, "server")
    sys.stderr = Tee(sys.stderr, "server.err", default="error")
    threading.excepthook = lambda a: ev("server.err", "error", f"thread {a.thread.name if a.thread else '?'}: {a.exc_type.__name__}: {a.exc_value}")
    ev("server", "info", f"Chart server started (pid {os.getpid()})")
    threading.Thread(target=watch_loop, daemon=True, name="log-watch").start()
