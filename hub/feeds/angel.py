"""Angel One SmartAPI: Indian equities and indices on your own Angel One account.

Read-only market data. Nothing here places orders.

Credentials come from two places, .env first (never committed):
    ANGEL_API_KEY=...          # SmartAPI app key (smartapi.angelbroking.com > My Profile > API)
    ANGEL_CLIENT_ID=...        # Angel One client code, e.g. S1234567
    ANGEL_PIN=...              # 4-digit trading PIN
    ANGEL_TOTP_SECRET=...      # optional: base32 secret behind the "Angel One SmartAPI TOTP" QR.
                               # With it the daily 2FA login happens automatically.

or, without editing .env, the login window in the hub (Feeds > Angel One > Log in). It saves
the API key, client id and PIN in data/angel_auth.json (0600, git-ignored) so the only thing
needed each trading day is the 6-digit code from the authenticator app. A saved value wins
over .env ("Forget" in the window drops it again). The session itself is cached until midnight
IST, and is renewed automatically when ANGEL_TOTP_SECRET is known.

Why this plugin talks to the REST API directly instead of using the SmartApi SDK:
one reused HTTPS connection, a 3 req/s token bucket, single-flight caching of the
"newest bars" request so every open chart of a symbol shares one call, and a daily
session cache - so normal charting costs a handful of requests per second instead
of a new TLS handshake and SDK call per candle poll. No pandas, no SDK, no orders.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import http.client
import json
import os
import random
import re
import ssl
import struct
import threading
import time
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
IST = timezone(timedelta(hours=5, minutes=30))


def _load_env():
    """Standalone runs (CLI) do not go through server.py, so read the repo .env here.
    Real environment variables always win."""
    for path in (os.path.join(ROOT, ".env"), os.path.join(HERE, "..", ".env")):
        try:
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
        except OSError:
            pass


_load_env()

DATA = os.environ.get("TRADEX_DATA") or os.path.join(ROOT, "data")
UNIVERSE_FILE = os.path.join(DATA, "angel_universe.json")
SESSION_FILE = os.path.join(DATA, "angel_session.json")
AUTH_FILE = os.path.join(DATA, "angel_auth.json")   # saved by the hub login window, 0600
UNIVERSE_TTL = 7 * 86400           # Angel republishes the master daily; weekly is plenty

MANIFEST = {"id": "angel", "name": "Angel One", "short": "ANG", "needs": "account", "official": True,
            "default_on": False, "login": "totp",
            "markets": "Indian equities and indices (NSE, BSE indices) on your own Angel One account",
            "terms": "https://smartapi.angelbroking.com/docs",
            "notice": "Read-only candles through your Angel One SmartAPI key. "
                      "Needs ANGEL_API_KEY, ANGEL_CLIENT_ID, ANGEL_PIN in .env and a daily TOTP login."}

API_HOST = "apiconnect.angelone.in"
LOGIN_PATH = "/rest/auth/angelbroking/user/v1/loginByPassword"
CANDLE_PATH = "/rest/secure/angelbroking/historical/v1/getCandleData"
MASTER_URL = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
UA = "TradeX/1.0 (Angel One SmartAPI)"

TF_SEC = {"M1": 60, "M5": 300, "M15": 900, "M30": 1800, "H1": 3600, "H4": 14400, "D1": 86400}
INTERVAL = {"M1": "ONE_MINUTE", "M5": "FIVE_MINUTE", "M15": "FIFTEEN_MINUTE", "M30": "THIRTY_MINUTE",
            "H1": "ONE_HOUR", "D1": "ONE_DAY"}
RATE_INTERVAL = 0.36               # SmartAPI allows ~3 requests/second; 0.36 s spacing stays under it
# getCandleData is capped at 3/s, 120/min and 5000/h, and the cap is counted per client code,
# not per process: the hub and a backfill on the same account share one budget.
MAX_PER_SEC = 2                    # stay under 3/s even when both processes burst at once
MAX_PER_MIN = 110                  # under the 120/min cap
MAX_PER_HOUR = 4500                # under the 5000/h cap; without this an idle hour spent on charts
                                   # could still run the account into the hourly wall
MAX_WAIT_S = 20                    # longer than this and a chart request gets an answer, not a hang
RATE_FILE = os.path.join(DATA, "angel_rate.json")   # recent call times, shared by every process
MINUTE_RETENTION_DAYS = 100        # Angel serves roughly the last 100 days of intraday candles
DAY_RETENTION_DAYS = 2000
CHUNK_DAYS = 30

# Fallback list when the instrument master has not been downloaded yet. Tokens are
# Angel's 999-prefixed index tokens (the legacy 5-digit tokens return no candles).
FALLBACK_INDICES = [
    ("99926000", "NIFTY50", "NIFTY 50"),
    ("99926009", "NIFTYBANK", "NIFTY BANK"),
    ("99926008", "NIFTYIT", "NIFTY IT"),
    ("99926004", "NIFTY500", "NIFTY 500"),
    ("99926011", "NIFTYMIDCAP100", "NIFTY MIDCAP 100"),
    ("99926017", "INDIAVIX", "INDIA VIX"),
    ("99919000", "SENSEX", "SENSEX"),
]

# The NIFTY 50 constituents, so the default list is useful before the master lands
# and so these get their own group in the Symbol list.
NIFTY50 = {
    "ADANIENT", "ADANIPORTS", "APOLLOHOSP", "ASIANPAINT", "AXISBANK", "BAJAJ-AUTO", "BAJFINANCE",
    "BAJAJFINSV", "BEL", "BHARTIARTL", "CIPLA", "COALINDIA", "DRREDDY", "EICHERMOT", "ETERNAL",
    "GRASIM", "HCLTECH", "HDFCBANK", "HDFCLIFE", "HEROMOTOCO", "HINDALCO", "HINDUNILVR", "ICICIBANK",
    "INDUSINDBK", "INFY", "ITC", "JIOFIN", "JSWSTEEL", "KOTAKBANK", "LT", "M&M", "MARUTI", "MAXHEALTH",
    "NESTLEIND", "NTPC", "ONGC", "POWERGRID", "RELIANCE", "SBILIFE", "SBIN", "SHRIRAMFIN", "SUNPHARMA",
    "TATACONSUM", "TATAMOTORS", "TATASTEEL", "TCS", "TECHM", "TITAN", "TRENT", "ULTRACEMCO", "WIPRO",
}


class AngelError(RuntimeError):
    """Any failure surfaced to the chart (message is shown in the hub)."""


# --------------------------------------------------------------------------- config
_FIELDS = (("ANGEL_API_KEY", "api_key"), ("ANGEL_CLIENT_ID", "client_id"),
           ("ANGEL_PIN", "pin"), ("ANGEL_TOTP_SECRET", "totp_secret"))


def _saved_cfg():
    """Credentials entered in the hub login window (data/angel_auth.json, 0600)."""
    d = _read_json(AUTH_FILE, {})
    return {key: str(d.get(key) or "").strip() for _env, key in _FIELDS}


def _cfg():
    """Saved credentials win (the hub window is the newest word), .env fills the gaps."""
    saved = _saved_cfg()
    return {env: (saved[key] or os.environ.get(env, "").strip()) for env, key in _FIELDS}


def configured():
    c = _cfg()
    return bool(c["ANGEL_API_KEY"] and c["ANGEL_CLIENT_ID"] and c["ANGEL_PIN"])


def save_credentials(api_key="", client_id="", pin="", totp_secret=None):
    """Save what the hub login window typed. Blank api_key/client_id/pin keep the saved value;
    totp_secret None keeps it, "" clears it. Changing the account forgets the cached session.
    Only typed values are stored: .env still fills any field the file leaves empty."""
    old = _cfg()
    saved = _saved_cfg()
    new = {
        "api_key": (api_key or "").strip() or saved["api_key"],
        "client_id": (client_id or "").strip() or saved["client_id"],
        "pin": (pin or "").strip() or saved["pin"],
        "totp_secret": saved["totp_secret"] if totp_secret is None else (totp_secret or "").strip(),
    }
    env = {key: os.environ.get(name, "").strip() for name, key in _FIELDS}
    effective = {k: (new[k] or env[k]) for k in ("api_key", "client_id", "pin")}
    if not all(effective.values()):
        raise AngelError("The API key, client ID and PIN are all needed (a blank field keeps the saved value)")
    if totp_secret:
        totp_now(totp_secret)   # reject a mistyped secret now, not at midnight
    changed = (effective["api_key"], effective["client_id"], effective["pin"]) != \
              (old["ANGEL_API_KEY"], old["ANGEL_CLIENT_ID"], old["ANGEL_PIN"])
    _write_json(AUTH_FILE, new, private=True)
    if changed:
        forget_session()
    return {k: v for k, v in new.items() if k != "pin" and v}


def _today():
    return datetime.now(IST).date().isoformat()


def _mask(value, keep=4):
    value = value or ""
    return value if len(value) <= keep else "*" * (len(value) - keep) + value[-keep:]


def _read_json(path, default):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def _write_json(path, data, private=False):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, separators=(",", ":"))
    if private:
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
    os.replace(tmp, path)


# --------------------------------------------------------------------------- TOTP
def totp_now(secret):
    """RFC 6238 TOTP (SHA-1, 30 s, 6 digits) with the standard library only."""
    clean = re.sub(r"[\s-]", "", secret or "").upper()
    if not clean:
        raise AngelError("ANGEL_TOTP_SECRET is empty")
    clean += "=" * (-len(clean) % 8)
    try:
        key = base64.b32decode(clean)
    except Exception as exc:
        raise AngelError(f"ANGEL_TOTP_SECRET is not valid base32: {exc}") from None
    counter = int(time.time()) // 30
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = (struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF) % 1_000_000
    return f"{code:06d}"


# --------------------------------------------------------------------------- HTTP
_ctx = ssl.create_default_context()
try:  # honour the operating system certificate store when available
    import truststore
    _ctx = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
except Exception:
    pass

_conn = None
_conn_lock = threading.Lock()
_rate_lock = threading.Lock()
_next_call = 0.0


def _file_lock(path):
    """Exclusive lock across processes (the hub and a running backfill share one account)."""
    fh = open(path, "a+b")
    if os.name == "nt":
        import msvcrt
        while True:
            try:
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)
                break
            except OSError:
                time.sleep(0.05)
    else:
        import fcntl
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
    return fh


def _file_unlock(fh):
    try:
        if os.name == "nt":
            import msvcrt
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    finally:
        fh.close()


def _rate_budget():
    """Reserve a call inside Angel's per-second, per-minute and per-hour caps, counting every process
    on this PC that uses the same client code. Without it the hub and a backfill can each stay under
    the cap and still get 403 together (a plain-text "Access denied", not JSON).
    A short shortage is waited out; a long one fails fast with a readable message instead of hanging
    a chart request for a minute."""
    windows = ((1.0, MAX_PER_SEC), (60.0, MAX_PER_MIN), (3600.0, MAX_PER_HOUR))
    while True:
        try:
            fh = _file_lock(RATE_FILE + ".lock")
        except OSError:
            return                      # no writable data folder: fall back to the per-second spacing
        try:
            now = time.time()
            try:
                with open(RATE_FILE, encoding="utf-8") as src:
                    recent = [t for t in json.load(src) if isinstance(t, (int, float)) and now - t < 3600]
            except (OSError, ValueError):
                recent = []
            wait = 0.0
            for span, cap in windows:
                hits = [t for t in recent if now - t < span]
                if len(hits) >= cap:
                    wait = max(wait, span - (now - hits[0]) + 0.02)
            if wait <= 0:
                recent.append(now)
                tmp = RATE_FILE + ".tmp"
                try:
                    with open(tmp, "w", encoding="utf-8") as dst:
                        json.dump(recent, dst)
                    os.replace(tmp, RATE_FILE)
                except OSError:
                    pass
                return
        finally:
            _file_unlock(fh)
        if wait > MAX_WAIT_S:
            raise AngelError(f"Angel One call budget used up ({len(recent)} calls in the last hour, "
                             f"cap {MAX_PER_HOUR}); showing saved candles until it frees up")
        time.sleep(min(max(wait, 0.02), 1.0))


def _throttle():
    global _next_call
    _rate_budget()
    with _rate_lock:
        now = time.monotonic()
        wait = _next_call - now
        _next_call = max(now, _next_call) + RATE_INTERVAL
    if wait > 0:
        time.sleep(wait)


def _post(path, payload, jwt=None, api_key=None):
    """One POST on a reused TLS connection. Raises AngelError on a transport failure."""
    global _conn
    body = json.dumps(payload).encode()
    headers = {
        "Content-Type": "application/json", "Accept": "application/json",
        "X-UserType": "USER", "X-SourceID": "WEB",
        "X-ClientLocalIP": "127.0.0.1", "X-ClientPublicIP": "127.0.0.1",
        "X-MACAddress": "00:00:00:00:00:00", "X-PrivateKey": api_key or _cfg()["ANGEL_API_KEY"],
        "User-Agent": UA, "Connection": "keep-alive",
    }
    if jwt:
        headers["Authorization"] = f"Bearer {jwt}"
    last = None
    for attempt in range(2):
        _throttle()
        with _conn_lock:
            try:
                if _conn is None:
                    _conn = http.client.HTTPSConnection(API_HOST, timeout=25, context=_ctx)
                _conn.request("POST", path, body=body, headers=headers)
                resp = _conn.getresponse()
                raw = resp.read()
                if resp.will_close:
                    _conn.close()
                    _conn = None
            except Exception as exc:
                last = exc
                try:
                    if _conn is not None:
                        _conn.close()
                finally:
                    _conn = None
                if attempt == 0:
                    continue
                raise AngelError(f"Angel One connection failed: {exc.__class__.__name__}: {exc}") from None
        try:
            return json.loads(raw)
        except ValueError:
            # Angel answers its rate limit (3/s, 120/min, 5000/h per client code) with a plain
            # "Access denied because of exceeding access rate" page instead of the JSON error codes
            # handled below, so charts used to fail outright while a background download was running.
            # Say "rate" so _candle_rows backs off and retries.
            if resp.status in (403, 429):
                raise AngelError(f"Angel One rate limited the request (HTTP {resp.status}); retrying") from None
            raise AngelError(f"Angel One sent a non-JSON reply (HTTP {resp.status})") from None
    raise AngelError(f"Angel One connection failed: {last}")


# --------------------------------------------------------------------------- session
_sess_lock = threading.Lock()
_session = None  # {"jwt","feed","date"}


def _load_session():
    global _session
    if _session is not None and _session.get("date") != _today():
        _session = None   # midnight IST passed while the hub was running: the session is dead
    if _session is None:
        saved = _read_json(SESSION_FILE, {})
        if saved.get("jwt") and saved.get("date") == _today() and saved.get("client_id") == _cfg()["ANGEL_CLIENT_ID"]:
            _session = {"jwt": saved["jwt"], "feed": saved.get("feed", ""), "date": saved["date"]}
    return _session


def _save_session(jwt, feed):
    global _session
    _session = {"jwt": jwt, "feed": feed, "date": _today()}
    _write_json(SESSION_FILE, {**_session, "client_id": _cfg()["ANGEL_CLIENT_ID"]}, private=True)


def forget_session():
    global _session
    with _sess_lock:
        _session = None
        try:
            os.remove(SESSION_FILE)
        except OSError:
            pass


def session_status():
    c = _cfg()
    s = _load_session() if configured() else None
    midnight = (datetime.now(IST) + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return {"configured": configured(), "totp_secret": bool(c["ANGEL_TOTP_SECRET"]),
            "logged_in": bool(s), "login_date": (s or {}).get("date"),
            "api_key": _mask(c["ANGEL_API_KEY"]), "client_id": c["ANGEL_CLIENT_ID"],
            "pin_set": bool(c["ANGEL_PIN"]), "saved": bool(_saved_cfg()["api_key"]),
            "expires_ms": int(midnight.timestamp() * 1000)}


def login(code=None):
    """TOTP 2FA login. `code` = a 6-digit code from the authenticator app; omitted = generate from
    ANGEL_TOTP_SECRET. Returns the session dict; raises AngelError with a readable reason."""
    c = _cfg()
    missing = [name for name, val in (("ANGEL_API_KEY", c["ANGEL_API_KEY"]), ("ANGEL_CLIENT_ID", c["ANGEL_CLIENT_ID"]),
                                      ("ANGEL_PIN", c["ANGEL_PIN"])) if not val]
    if missing:
        raise AngelError("Missing " + ", ".join(missing) + " in .env")
    code = (code or os.environ.get("ANGEL_TOTP", "")).strip()
    if not code:
        if c["ANGEL_TOTP_SECRET"]:
            code = totp_now(c["ANGEL_TOTP_SECRET"])
        else:
            raise AngelError("A 6-digit TOTP is needed: open Feeds > Angel One > Log in in the hub "
                             "(or save the TOTP secret there for automatic daily login)")
    if not (code.isdigit() and len(code) == 6):
        raise AngelError("The TOTP must be the 6-digit code from your authenticator app (not an OAuth code)")
    with _sess_lock:
        d = _post(LOGIN_PATH, {"clientcode": c["ANGEL_CLIENT_ID"], "password": c["ANGEL_PIN"], "totp": code},
                  api_key=c["ANGEL_API_KEY"])
        if not isinstance(d, dict) or not d.get("status"):
            msg = d.get("message", "login failed") if isinstance(d, dict) else "login failed"
            raise AngelError(f"Angel One rejected the login: {msg}")
        data = d.get("data") or {}
        jwt = (data.get("jwtToken") or "").removeprefix("Bearer ").strip()
        if not jwt:
            raise AngelError("Angel One login reply had no jwtToken")
        _save_session(jwt, data.get("feedToken", ""))
        return dict(_session)


def _jwt():
    s = _load_session()
    if s:
        return s["jwt"]
    if not configured():
        raise AngelError("Angel One is not configured: open Feeds > Angel One > Log in in the hub, "
                         "or set ANGEL_API_KEY, ANGEL_CLIENT_ID and ANGEL_PIN in .env")
    return login()["jwt"]


# --------------------------------------------------------------------------- hub login window
def _online_after_login():
    """A new session means the chart list can load: refresh it now instead of waiting for the hourly loop."""
    try:
        from . import refresh_now
        refresh_now(MANIFEST["id"])
    except Exception as exc:
        print(f"Angel One: symbol list refresh after login failed: {exc}", flush=True)


def api(action, payload=None):
    """Routes for the hub login window (POST /api/feeds/angel/<action>).
    status: masked config + session | save: keep credentials | login: save and log in with the daily TOTP
    | logout: forget the cached session | clear: forget credentials and session."""
    p = payload or {}
    if action == "status":
        return {"ok": True, **session_status()}
    if action == "save":
        save_credentials(p.get("api_key", ""), p.get("client_id", ""), p.get("pin", ""),
                         totp_secret=p.get("totp_secret") if "totp_secret" in p else None)
        return {"ok": True, "msg": "Credentials saved on this PC.", **session_status()}
    if action == "login":
        if any(p.get(k) for k in ("api_key", "client_id", "pin", "totp_secret")):
            save_credentials(p.get("api_key", ""), p.get("client_id", ""), p.get("pin", ""),
                             totp_secret=p.get("totp_secret") if "totp_secret" in p else None)
        s = login((p.get("totp") or p.get("code") or "").strip() or None)
        _online_after_login()
        return {"ok": True, "msg": f"Logged in to Angel One for {s['date']}.", **session_status()}
    if action == "logout":
        forget_session()
        return {"ok": True, "msg": "Session cleared. Charts will ask for today's TOTP.", **session_status()}
    if action == "clear":
        forget_session()
        try:
            os.remove(AUTH_FILE)
        except OSError:
            pass
        return {"ok": True, "msg": "Saved credentials and session removed from this PC.", **session_status()}
    raise AngelError(f"unknown Angel One action {action!r}")


# --------------------------------------------------------------------------- automatic daily login
_auto_started = False


def start():
    """Called once by the hub for enabled plugins. With a saved TOTP secret the session is renewed
    by itself every day after midnight IST, so the only manual case is a secret-less setup."""
    global _auto_started
    if _auto_started:
        return
    _auto_started = True
    threading.Thread(target=_auto_login_loop, daemon=True, name="angel-autologin").start()


def _auto_login_loop():
    last = None
    while True:
        try:
            if configured() and _cfg()["ANGEL_TOTP_SECRET"] and not _load_session():
                login()
                print("Angel One: automatic daily login done (session valid until midnight IST)", flush=True)
                _online_after_login()
        except Exception as exc:
            msg = str(exc)
            if msg != last:   # say each different problem once, not every minute
                print(f"Angel One: automatic login waiting: {msg}", flush=True)
                last = msg
        time.sleep(60)


# --------------------------------------------------------------------------- candles
def _ts(value):
    if isinstance(value, (int, float)):
        return int(value / 1000) if value > 100_000_000_000 else int(value)
    s = str(value).strip()
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        dt = datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)
    return int(dt.timestamp())


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _candle_rows(token, exchange, interval, start_s, end_s):
    payload = {
        "exchange": exchange, "symboltoken": str(token), "interval": interval,
        "fromdate": datetime.fromtimestamp(start_s, IST).strftime("%Y-%m-%d %H:%M"),
        "todate": datetime.fromtimestamp(end_s, IST).strftime("%Y-%m-%d %H:%M"),
    }
    d = None
    attempts = 5
    for attempt in range(attempts):
        try:
            d = _post(CANDLE_PATH, payload, jwt=_jwt())
        except AngelError as exc:
            # A rate-limited 403 arrives as an exception, not a JSON error code: back off and retry.
            if "rate" in str(exc).lower() and attempt + 1 < attempts:
                time.sleep(min(2.0 ** attempt, 6.0) * random.uniform(0.9, 1.2))
                continue
            raise
        if isinstance(d, dict) and d.get("status") is True:
            break
        code = str(d.get("errorcode", "")) if isinstance(d, dict) else ""
        msg = str(d.get("message", "")) if isinstance(d, dict) else ""
        if code in ("AG8001", "AG8002", "AG8003") or "Invalid Token" in msg or "Invalid API Key" in msg:
            login()  # expired/absent session: one fresh 2FA login (needs the TOTP secret)
            continue
        if code in ("AB1021", "AB1010") or "Too many requests" in msg or "rate" in msg.lower():
            if attempt + 1 < attempts:
                time.sleep(1.0 * (attempt + 1))
            continue
        raise AngelError(f"Angel One {code or 'error'}: {msg or 'request failed'}")
    if not isinstance(d, dict) or d.get("status") is not True:
        msg = d.get("message", "request failed") if isinstance(d, dict) else "request failed"
        raise AngelError(f"Angel One candle request failed: {msg}")
    rows = d.get("data") or []
    out = []
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) < 6:
            continue
        t, o, h, l, c, v = row[0], _num(row[1]), _num(row[2]), _num(row[3]), _num(row[4]), _num(row[5])
        if o <= 0 or h <= 0 or l <= 0 or c <= 0 or h < l:
            continue
        out.append({"time": _ts(t), "open": o, "high": h, "low": l, "close": c, "volume": v})
    out.sort(key=lambda b: b["time"])
    return out


# --------------------------------------------------------------------------- instrument universe
_uni_lock = threading.Lock()
_uni_cache = {"at": 0.0, "data": None}
_uni_refreshing = False
_tokens = {}   # ticker -> {"token","exchange","name","group"}


def _slug(text):
    return re.sub(r"[^A-Z0-9]+", "_", (text or "").upper()).strip("_")


def _islug(text):
    """Index ticker slug without separators: 'Nifty 50' and 'NIFTY 50' both give NIFTY50."""
    return re.sub(r"[^A-Z0-9]+", "", (text or "").upper())


def _fallback_universe():
    # Indices only: equity tokens live in the master, so listing equities before it lands
    # would offer charts that cannot load. The master arrives in the background.
    return [{"ticker": f"ANG_{slug}", "name": f"{label} (Angel One)", "group": "index", "key": slug,
             "token": token, "exchange": "NSE"} for token, slug, label in FALLBACK_INDICES]


def _build_universe():
    """Download Angel's OpenAPIScripMaster and keep only chartable NSE equities and NSE/BSE indices."""
    import urllib.request
    req = urllib.request.Request(MASTER_URL, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=180, context=_ctx) as resp:
        rows = json.load(resp)
    indices, equities, seen = [], [], set()
    for r in rows:
        seg = r.get("exch_seg")
        inst = (r.get("instrumenttype") or "").upper()
        token = str(r.get("token") or "")
        if not token:
            continue
        if inst in ("AMXIDX", "INDEX") and seg in ("NSE", "BSE"):
            label = (r.get("symbol") or r.get("name") or "").strip()
            slug = _islug(label)
            if not slug or slug in seen:
                continue
            seen.add(slug)
            indices.append({"ticker": f"ANG_{slug}", "name": f"{label} (Angel One)", "group": "index",
                            "key": slug, "token": token, "exchange": seg})
        elif seg == "NSE" and (inst in ("", "EQ") and str(r.get("symbol", "")).endswith("-EQ")):
            name = (r.get("name") or "").strip()
            if not name or name in seen:
                continue
            seen.add(name)
            equities.append({"ticker": f"ANG_{_slug(name)}", "name": f"{name} (Angel One)",
                             "group": "nifty50" if name.upper() in NIFTY50 else "equity",
                             "key": name, "token": token, "exchange": "NSE"})
    indices.sort(key=lambda x: (x["key"] != "NIFTY50", x["key"] != "NIFTYBANK", x["name"]))
    equities.sort(key=lambda x: (x["group"] != "nifty50", x["key"]))
    if not indices and not equities:
        raise AngelError("instrument master had no NSE rows")
    return {"built": int(time.time()), "indices": indices, "equities": equities}


def _refresh_universe_async():
    global _uni_refreshing
    if _uni_refreshing:
        return
    _uni_refreshing = True

    def work():
        global _uni_refreshing
        try:
            uni = _build_universe()
            _write_json(UNIVERSE_FILE, uni)
            with _uni_lock:
                _uni_cache.update(at=time.time(), data=uni)
            _index_tokens(uni)
        except Exception as exc:
            print(f"Angel One: instrument master refresh failed: {exc}", flush=True)
        finally:
            _uni_refreshing = False

    threading.Thread(target=work, daemon=True, name="angel-universe").start()


def _index_tokens(uni):
    with _uni_lock:
        _tokens.clear()
        for item in uni.get("indices", []) + uni.get("equities", []):
            if item.get("token"):
                _tokens[item["ticker"]] = item


def _universe():
    with _uni_lock:
        if _uni_cache["data"] is not None:
            return _uni_cache["data"]
    uni = _read_json(UNIVERSE_FILE, None)
    if uni and (uni.get("indices") or uni.get("equities")):
        with _uni_lock:
            _uni_cache.update(at=time.time(), data=uni)
        _index_tokens(uni)
        if time.time() - uni.get("built", 0) > UNIVERSE_TTL:
            _refresh_universe_async()
        return uni
    _refresh_universe_async()   # first run: serve the curated list now, the full master lands in the background
    return None


def symbols():
    if not configured():
        raise AngelError("Angel One: open Feeds > Angel One > Log in in the hub (or set ANGEL_API_KEY, "
                         "ANGEL_CLIENT_ID and ANGEL_PIN in .env)")
    uni = _universe()
    if not uni:
        return _fallback_universe()
    return [{k: v for k, v in item.items() if k in ("ticker", "name", "group", "key")}
            for item in uni.get("indices", []) + uni.get("equities", [])]


def _resolve(ticker):
    """ticker -> (token, exchange, key). Falls back to NIFTY 50 names while the master downloads."""
    if ticker in _tokens:
        item = _tokens[ticker]
        return str(item["token"]), item.get("exchange", "NSE"), item.get("key", ticker[4:])
    uni = _universe()
    if uni:
        for item in uni.get("indices", []) + uni.get("equities", []):
            if item.get("ticker") == ticker and item.get("token"):
                return str(item["token"]), item.get("exchange", "NSE"), item.get("key", ticker[4:])
    short = ticker.removeprefix("ANG_")
    for token, slug, label in FALLBACK_INDICES:
        if short == slug:
            return token, "NSE", label
    raise AngelError(f"Angel One: unknown symbol {ticker}. Refresh the list: python hub/feeds/angel.py --refresh")


# --------------------------------------------------------------------------- live request coalescing
_live_lock = threading.Lock()
_live = {}          # (token, tf) -> (monotonic, bars)
_live_busy = {}
LIVE_TTL = 1.6      # seconds: every chart of the same symbol within this window shares one request


def _coalesce(key, fetch):
    now = time.monotonic()
    with _live_lock:
        hit = _live.get(key)
        if hit and now - hit[0] < LIVE_TTL:
            return hit[1]
        event = _live_busy.get(key)
        owner = event is None
        if owner:
            event = _live_busy[key] = threading.Event()
    if not owner:
        event.wait(10)
        with _live_lock:
            hit = _live.get(key)
        if hit:
            return hit[1]
    try:
        bars = fetch()
        with _live_lock:
            if len(_live) > 512:
                _live.clear()
            _live[key] = (time.monotonic(), bars)
        return bars
    finally:
        with _live_lock:
            _live_busy.pop(key, None)
        event.set()


# --------------------------------------------------------------------------- history
def _span_bars(token, exchange, tf, want, anchor_s):
    """Fetch `want` bars ending at anchor_s, walking backwards in chunks inside Angel's retention."""
    native_tf = "H1" if tf == "H4" else tf
    interval = INTERVAL[native_tf]
    step = TF_SEC[native_tf]
    intraday = native_tf != "D1"
    max_days = MINUTE_RETENTION_DAYS if intraday else DAY_RETENTION_DAYS
    chunk_s = CHUNK_DAYS * 86400 if intraday else 365 * 86400
    # Calendar padding: an intraday bar only exists for ~6.25 h of a 24 h day and 5/7 of the week.
    bars_per_day = (375 * 60 / step) if intraday else 1.0
    days = max(2, int(want / bars_per_day * 1.45) + 2)
    oldest = anchor_s - min(max_days, days) * 86400
    end = anchor_s
    frames = {}
    for _widening in range(4):
        while True:
            start = max(oldest, end - chunk_s)
            try:
                rows = _candle_rows(token, exchange, interval, start, end)
            except AngelError as exc:
                if frames:   # keep what we already have; a deep window may simply be outside retention
                    break
                raise AngelError(f"{exc} (symbol {token} on {exchange})") from None
            for b in rows:
                frames[b["time"]] = b
            if len(frames) >= want or start <= oldest:
                break
            end = start
        # A weekend, holiday or long market closure can leave the whole window empty: look further
        # back so the chart shows the last real bars instead of nothing. Bounded to 3 extra windows.
        if frames or oldest <= anchor_s - max_days * 86400:
            break
        end = oldest
        oldest = max(anchor_s - max_days * 86400, oldest - 14 * 86400)
    bars = [frames[t] for t in sorted(frames)]
    bars = [b for b in bars if b["time"] <= anchor_s]
    return bars


def _local_bars(symbol, tf, limit, to_ms=None):
    """Candles that scripts/angel_backfill.py already saved (data/history/<tf>/<TICKER>.csv).
    Used when Angel refuses after its retries, so a chart shows the stored session instead of an error."""
    import csv
    path = os.path.join(DATA, "history", tf, f"{symbol}.csv")
    try:
        with open(path, newline="", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
    except OSError:
        return None
    ceil_s = int(to_ms) // 1000 if to_ms else None
    out = []
    for row in rows:
        try:
            t = int(float(row["time"]))
        except (KeyError, TypeError, ValueError):
            continue
        if ceil_s is not None and t > ceil_s:
            continue
        try:
            out.append({"time": t, "open": float(row["open"]), "high": float(row["high"]),
                        "low": float(row["low"]), "close": float(row["close"]),
                        "volume": float(row.get("volume") or 0)})
        except (KeyError, TypeError, ValueError):
            continue
    if not out:
        return None
    out.sort(key=lambda b: b["time"])
    return out[-max(1, int(limit or 1000)):]


# --------------------------------------------------------------------------- candle cache
# Every online fetch is merged into data/cache/angel/<tf>/<TICKER>.csv. Opening a chart again, or
# scrolling back over ground already covered, then costs zero API calls instead of one per pane,
# which is what keeps the account off Angel's 3/s and 120/min walls.
CACHE_DIR = os.path.join(DATA, "cache", "angel")
CACHE_MAX = 20000        # bars kept per symbol and timeframe
CACHE_WRITE_S = 60       # at most one rewrite per symbol and timeframe per minute while polling
CACHE_GRACE_S = 120      # a cache filled this recently is shown without asking Angel again
_cached = {}             # (symbol, tf) -> {"mtime": float, "bars": [...]}
_cached_lock = threading.Lock()
_cache_written = {}      # (symbol, tf) -> monotonic time of the last write
_last_fetch = {}         # (symbol, tf) -> monotonic time of the last successful online fetch
_refreshing = set()      # (symbol, tf) with a stale-while-revalidate fetch running
_refresh_lock = threading.Lock()


def _cache_path(symbol, tf):
    return os.path.join(CACHE_DIR, tf, re.sub(r"[^A-Za-z0-9._-]+", "_", str(symbol)) + ".csv")


def _cache_read(symbol, tf):
    path = _cache_path(symbol, tf)
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return None
    with _cached_lock:
        hit = _cached.get((symbol, tf))
    if hit and hit["mtime"] == mtime:
        return hit["bars"]
    import csv
    bars = []
    try:
        with open(path, newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                try:
                    bars.append({"time": int(float(row["time"])), "open": float(row["open"]),
                                 "high": float(row["high"]), "low": float(row["low"]),
                                 "close": float(row["close"]), "volume": float(row.get("volume") or 0)})
                except (KeyError, TypeError, ValueError):
                    continue
    except OSError:
        return None
    bars.sort(key=lambda b: b["time"])
    with _cached_lock:
        if len(_cached) > 256:
            _cached.clear()
        _cached[(symbol, tf)] = {"mtime": mtime, "bars": bars}
    return bars


def _cache_write(symbol, tf, bars, force=False):
    if not bars:
        return
    now = time.monotonic()
    if not force and now - _cache_written.get((symbol, tf), 0) < CACHE_WRITE_S:
        return
    _cache_written[(symbol, tf)] = now
    merged = {b["time"]: b for b in (_cache_read(symbol, tf) or [])}
    for b in bars:
        merged[b["time"]] = b
    out = [merged[t] for t in sorted(merged)][-CACHE_MAX:]
    path = _cache_path(symbol, tf)
    import csv
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
        with open(tmp, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["time", "open", "high", "low", "close", "volume"])
            w.writerows((b["time"], b["open"], b["high"], b["low"], b["close"], b["volume"]) for b in out)
        os.replace(tmp, path)
        with _cached_lock:
            _cached[(symbol, tf)] = {"mtime": os.path.getmtime(path), "bars": out}
    except OSError:
        pass


def _cache_slice(bars, want, anchor_s):
    """The newest `want` cached bars that opened at or before anchor_s (may be fewer)."""
    if not bars:
        return []
    out = [b for b in bars if b["time"] <= anchor_s]
    return out[-want:]


def _refresh_async(symbol, tf, want, to_ms):
    """One background fetch per symbol while charts keep painting from the cache. The live poller
    rebroadcasts history if the gap it finds is bigger than one candle, so nothing is missed."""
    key = (symbol, tf)
    with _refresh_lock:
        if key in _refreshing:
            return
        _refreshing.add(key)

    def work():
        try:
            bars = _history_online(symbol, tf, want, to_ms)
            _cache_write(symbol, tf, bars, force=True)
            _last_fetch[key] = time.monotonic()
        except Exception:
            pass
        finally:
            with _refresh_lock:
                _refreshing.discard(key)

    threading.Thread(target=work, daemon=True, name="angel-refresh").start()


def _cache_fresh(bars, tf, anchor_s):
    """Cached data is usable without a call when it reaches the requested time. The tail is allowed
    to be two candles old: polling fills it faster than a chart can be reopened."""
    if not bars:
        return False
    return bars[-1]["time"] >= anchor_s - max(2 * TF_SEC.get(tf, 60), 120)


def history(symbol, tf, limit=1000, to_ms=None):
    """Angel candles, served from the local cache when it already covers the request (no API call),
    else fetched online and merged back into the cache. Candles saved by scripts/angel_backfill.py
    are the last resort when Angel keeps refusing (403)."""
    want = max(1, min(int(limit or 1000), 5000))
    anchor_s = int(to_ms) // 1000 if to_ms else int(time.time())
    live_tail = not to_ms and want <= 6     # the live poll: always ask for the newest bars
    cached = None if live_tail else _cache_read(symbol, tf)
    if cached:
        window = _cache_slice(cached, want, anchor_s)
        span = TF_SEC.get(tf, 60)
        recent = time.monotonic() - _last_fetch.get((symbol, tf), 0.0)
        # "Covered" means the cache reaches back far enough for the whole window. Fewer bars than
        # asked is normal (weekend, holiday, a young listing) as long as nothing before them exists.
        covered = bool(window) and (len(window) >= want or cached[0]["time"] <= anchor_s - want * span + span)
        if covered and (_cache_fresh(cached, tf, anchor_s) or recent < max(CACHE_GRACE_S, span)):
            return window                   # the chart paints now, no API call
        if covered:
            _refresh_async(symbol, tf, want, to_ms)   # stale-while-revalidate: paint now, update behind it
            return window
    try:
        bars = _history_online(symbol, tf, want, to_ms)
        _last_fetch[(symbol, tf)] = time.monotonic()
    except AngelError:
        saved = _cache_slice(cached, want, anchor_s) if cached else []
        if not saved:
            saved = _local_bars(symbol, tf, want, to_ms)
        if saved:
            return saved
        raise
    _cache_write(symbol, tf, bars)
    return bars


def _history_online(symbol, tf, limit=1000, to_ms=None):
    if tf not in TF_SEC:
        raise AngelError(f"Angel One: unsupported timeframe {tf}")
    want = max(1, min(int(limit or 1000), 5000))
    anchor_s = int(to_ms) // 1000 if to_ms else int(time.time())
    token, exchange, _key = _resolve(symbol)

    if tf == "H4":   # Angel has no 4-hour interval: join 1-hour candles
        src_want = min(want * 4 + 4, 4000)
        src = _span_bars(token, exchange, "H1", src_want, anchor_s)
        from .base import resample
        bars = resample(src, "H1", "H4")
        return [b for b in bars if b["time"] <= anchor_s][-want:]

    if not to_ms and want <= 6:
        bars = _coalesce((token, tf), lambda: _span_bars(token, exchange, tf, 6, int(time.time())))
        return bars[-want:]

    return _span_bars(token, exchange, tf, want, anchor_s)[-want:]


# --------------------------------------------------------------------------- stream
async def stream(ws, symbol, tf):
    """Live updates over the REST path, one shared poller per symbol (base.poll_stream), on an
    interval that follows the timeframe. Angel caps are per client code, so two charts of the same
    symbol must cost one request stream, not two."""
    from .base import poll_stream
    await poll_stream(ws, history, symbol, tf, MANIFEST["name"], history=300)


def selftest():
    if not configured():
        return False, "log in from Feeds > Angel One in the hub"
    try:
        b = history("ANG_NIFTY50", "M5", 3)
    except Exception as exc:
        return False, str(exc)[:140]
    return bool(b), f"{len(b)} candles, last close {b[-1]['close'] if b else '-'}"


# --------------------------------------------------------------------------- CLI
def _cli(argv):
    import argparse
    p = argparse.ArgumentParser(description="Angel One SmartAPI chart source for TradeX (read-only).")
    p.add_argument("--status", action="store_true", help="show credentials/session status")
    p.add_argument("--login", nargs="?", const="", metavar="TOTP", help="log in now (6-digit code), or generate it from ANGEL_TOTP_SECRET")
    p.add_argument("--logout", action="store_true", help="forget the cached session")
    p.add_argument("--refresh", action="store_true", help="download the instrument master now")
    p.add_argument("--test", nargs="?", const="ANG_NIFTY50", metavar="TICKER", help="fetch a few candles")
    p.add_argument("--tf", default="M5", choices=list(TF_SEC), help="timeframe for --test")
    p.add_argument("--limit", type=int, default=5, help="candles for --test")
    a = p.parse_args(argv)

    if a.status:
        st = session_status()
        print(f"configured:    {st['configured']}")
        print(f"totp secret:   {st['totp_secret']}")
        print(f"logged in:     {st['logged_in']}  (login date: {st['login_date']})")
        if configured():
            uni = _read_json(UNIVERSE_FILE, None)
            n = len((uni or {}).get("indices", [])) + len((uni or {}).get("equities", []))
            print(f"symbols:       {n or 'not downloaded yet (run --refresh)'}")
        return 0
    if a.logout:
        forget_session()
        print("Cached Angel One session removed.")
        return 0
    if a.login is not None:
        s = login(a.login or None)
        print(f"Logged in to Angel One for {s['date']} (session cached).")
        return 0
    if a.refresh:
        uni = _build_universe()
        _write_json(UNIVERSE_FILE, uni)
        _index_tokens(uni)
        print(f"Instrument master saved: {len(uni['indices'])} indices, {len(uni['equities'])} equities.")
        return 0
    if a.test:
        bars = history(a.test, a.tf, a.limit)
        print(json.dumps(bars[-a.limit:], indent=2))
        return 0
    p.print_help()
    return 0


if __name__ == "__main__":
    import sys
    try:
        raise SystemExit(_cli(sys.argv[1:]))
    except AngelError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2)
