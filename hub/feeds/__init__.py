"""TradeX chart-source plugins.

A plugin is one file in this folder with:
  MANIFEST = {"id", "name", "short" (3 letters for the status lights), "needs": "none"|"key"|"account",
              "official": bool, "default_on": bool, "markets", "terms", "notice" (shown before turning it on)}
  symbols()                       -> [{"ticker", "name", "group", "key"}]   key = instrument key, e.g. "BTCUSD"
  history(symbol, tf, limit, to_ms) -> bars oldest first; to_ms = newest open time wanted (scroll-back), or None
  selftest()                      -> (ok, message)
  stream(ws, symbol, tf)          optional; default = poll history every 2 s (feeds.base.poll_stream)
  api(action, payload)            optional; a source's own routes under /api/feeds/<id>/<action> (e.g. the
                                  Angel One login window: status, save, login, logout, clear). Set
                                  MANIFEST["login"] so the hub shows a Log in button for it.
  start()                         optional; called once at hub start while the source is on (background jobs)
Tickers must be unique across all sources, so each plugin prefixes its own (OKX_, CB_, KRK_ ...).
Which plugins are on lives in <data>/sources.json; data folder = env TRADEX_DATA or <repo>/data.
"""
import importlib, json, os, pkgutil, threading, time
from . import base

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.environ.get("TRADEX_DATA") or os.path.join(HERE, "..", "..", "data")
CFG = os.path.join(DATA, "sources.json")
_plugins, _syms, _status = {}, {}, {}
_lock = threading.Lock()

def _load_all():
    if _plugins:
        return
    for m in pkgutil.iter_modules([HERE]):
        if m.name.startswith("_") or m.name == "base":
            continue
        try:
            mod = importlib.import_module(f"{__name__}.{m.name}")
            _plugins[mod.MANIFEST["id"]] = mod
        except Exception as e:  # a broken plugin must never stop the hub
            print(f"feeds: plugin {m.name} not loaded: {e}", flush=True)

def config():
    _load_all()
    try:
        saved = json.load(open(CFG, encoding="utf-8"))
    except (OSError, ValueError):
        saved = {}
    return {pid: bool(saved.get(pid, mod.MANIFEST.get("default_on", False))) for pid, mod in _plugins.items()}

def set_enabled(pid, on):
    cfg = config()
    if pid not in cfg:
        raise KeyError(pid)
    cfg[pid] = bool(on)
    os.makedirs(DATA, exist_ok=True)
    json.dump(cfg, open(CFG, "w", encoding="utf-8"), indent=1)
    return cfg

def enabled():
    return {pid: _plugins[pid] for pid, on in config().items() if on}

def get(pid):
    return enabled().get(pid)

def module(pid):
    """A loaded plugin even while it is switched off (so it can be set up / logged in first)."""
    _load_all()
    return _plugins.get(pid)

def plugin_api(pid, action, payload=None):
    """Call an optional plugin api() route (GET/POST /api/feeds/<pid>/<action>)."""
    mod = module(pid)
    if not mod or not hasattr(mod, "api"):
        raise KeyError(pid)
    return mod.api(action, payload or {})

def refresh_now(pid):
    """Reload one plugin's symbol list now (after a login, instead of waiting for the hourly loop)."""
    mod = module(pid)
    if mod:
        _refresh(pid, mod)

def manifests():
    cfg = config()
    return [dict(mod.MANIFEST, enabled=cfg[pid], status=_status.get(pid)) for pid, mod in sorted(_plugins.items())]

def _refresh(pid, mod):
    try:
        s = mod.symbols()
        for x in s:
            x["source"] = pid
        with _lock:
            _syms[pid] = s
        _status[pid] = {"ok": True, "msg": f"{len(s)} charts"}
    except Exception as e:
        _status[pid] = {"ok": False, "msg": f"symbol list failed: {str(e)[:160] or e.__class__.__name__}"}

def symbols_loop():
    """Background: refresh every enabled plugin's symbol list each hour (and soon after one is switched on)."""
    done = {}
    while True:
        for pid, mod in enabled().items():
            if time.time() - done.get(pid, 0) > 3600:
                _refresh(pid, mod)
                done[pid] = time.time()
        time.sleep(5)

def all_symbols():
    on = enabled()
    with _lock:
        return [x for pid, lst in _syms.items() if pid in on for x in lst]

def instrument_key(ticker, source):
    with _lock:
        for x in _syms.get(source, []):
            if x["ticker"] == ticker:
                return x.get("key")
    return None

def status():
    return {pid: _status.get(pid, {"ok": False, "msg": "starting"}) for pid in enabled()}

def probe():
    """Self-test every default-on plugin from this network. Used by setup to switch on only what answers here."""
    _load_all()
    out = {}
    for pid, mod in _plugins.items():
        if not mod.MANIFEST.get("default_on"):
            out[pid] = {"on": False, "msg": "off by default"}
            continue
        try:
            ok, msg = mod.selftest()
        except Exception as e:
            ok, msg = False, f"{e.__class__.__name__}: {str(e)[:80]}"
        out[pid] = {"on": bool(ok), "msg": msg}
    return out

def stream_fn(pid):
    mod = get(pid)
    if not mod:
        return None
    if hasattr(mod, "stream"):
        return mod.stream
    async def _poll(ws, symbol, tf):
        await base.poll_stream(ws, mod.history, symbol, tf, mod.MANIFEST["name"])
    return _poll
