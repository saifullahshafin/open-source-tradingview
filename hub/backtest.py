"""Options backtest engine for TradeX (research only: nothing here places orders).

Why it does not just call the Angel One history API
----------------------------------------------------
Angel's SmartAPI keeps candles only for contracts that are still in its instrument
master. NIFTY option contracts that expired weeks ago are gone from the master, so
their historical candles cannot be requested at all. This engine therefore uses:

  * NSE F&O bhavcopy (public daily archive)  -> real settlement/close prices for every
    NIFTY option contract that traded on a day, including contracts that have expired.
  * Angel One SmartAPI (H1 candles)          -> the intraday NIFTY / BANKNIFTY spot path,
    which is what "hourly basis" marks are drawn on.

Intraday option marks are Black-Scholes values whose implied volatility is recalibrated
each day to that day's real bhavcopy close, so the curve is anchored to real prices at
every day end and interpolated hourly in between. Entry is a Monday 15:10 fill and is
priced at that day's bhavcopy close (15:30) - the closest real price available; this is
stated in every result so the numbers are not oversold.

Strategy (defaults, all configurable):
  Monday 15:10 -> sell CE and PE of the expiry about `target_dte` days away at about
  `delta` (0.25) delta, `lots_sell` lots each; buy the same strikes in the monthly
  expiry (`lots_buy_same`); buy `wing_points` away wings (`wing_lots`). Exit the same
  week Friday 15:10. If the near expiry is the monthly expiry itself, the monthly hedge
  uses the next month's expiry.
"""
from __future__ import annotations

import csv
import gzip
import io
import json
import math
import os
import threading
import time
import urllib.error
import urllib.request
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
IST = timezone(timedelta(hours=5, minutes=30))

DATA = os.environ.get("TRADEX_DATA") or os.path.join(ROOT, "data")
BT_DIR = os.path.join(DATA, "backtest")
BHAV_DIR = os.path.join(BT_DIR, "bhavcopy")
SPOT_DIR = os.path.join(BT_DIR, "spot")
RUNS_DIR = os.path.join(BT_DIR, "runs")
HOLIDAY_FILE = os.path.join(BT_DIR, "holidays.json")

BHAV_URL = "https://nsearchives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_{day}_F_0000.csv.zip"
UA = "Mozilla/5.0 (TradeX backtest; research only)"

SYMBOLS = {
    "NIFTY": {"bhav": "NIFTY", "angel": "ANG_NIFTY50", "step": 50},
    "BANKNIFTY": {"bhav": "BANKNIFTY", "angel": "ANG_NIFTYBANK", "step": 100},
}

DEFAULTS = {
    "symbol": "NIFTY",
    "start": "2026-03-01",
    "end": None,                 # None = today
    "entry_weekday": 0,          # Monday
    "entry_time": "15:10",
    "exit_weekday": 4,           # Friday
    "exit_time": "15:10",
    "target_dte": 8,
    "delta": 0.25,
    "lots_sell": 2,              # per side (CE and PE)
    "lots_buy_same": 1,          # same strike, monthly expiry
    "wing_points": 500,
    "wing_lots": 1,
    "wing_expiry": "near",       # near | monthly
    "monthly_hedge": True,
    "lot_size": None,            # None = read from bhavcopy (NIFTY 2026: 65)
    "costs": True,
    "risk_free": 0.065,
}

# ------------------------------------------------------------------ small helpers
def _iso(d):
    return d.isoformat() if isinstance(d, date) else str(d)


def _d(s):
    return s if isinstance(s, date) else datetime.strptime(str(s)[:10], "%Y-%m-%d").date()


def _dt(day, hhmm):
    h, m = (int(x) for x in str(hhmm).split(":"))
    return datetime(day.year, day.month, day.day, h, m, tzinfo=IST)


def _years(a, b):
    return max(0.0, (b - a).total_seconds()) / (365.0 * 86400.0)


def _read_json(path, default):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def _write_json(path, obj, gz=False):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    if gz:
        with gzip.open(tmp, "wt", encoding="utf-8") as fh:
            json.dump(obj, fh, separators=(",", ":"))
    else:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, separators=(",", ":"))
    os.replace(tmp, path)


# ------------------------------------------------------------------ pricing
_SQRT2 = math.sqrt(2.0)


def _ncdf(x):
    return 0.5 * (1.0 + math.erf(x / _SQRT2))


def bs_price(S, K, T, vol, kind, r=0.0):
    """Black-Scholes price with continuous yield 0. T in years."""
    if S <= 0 or K <= 0:
        return 0.0
    if T <= 0:
        return max(0.0, S - K) if kind == "CE" else max(0.0, K - S)
    if vol <= 1e-6:
        return max(0.0, S - K * math.exp(-r * T)) if kind == "CE" else max(0.0, K * math.exp(-r * T) - S)
    sd = vol * math.sqrt(T)
    d1 = (math.log(S / K) + (r + 0.5 * vol * vol) * T) / sd
    d2 = d1 - sd
    if kind == "CE":
        return S * _ncdf(d1) - K * math.exp(-r * T) * _ncdf(d2)
    return K * math.exp(-r * T) * _ncdf(-d2) - S * _ncdf(-d1)


def bs_delta(S, K, T, vol, kind, r=0.0):
    if S <= 0 or K <= 0 or T <= 0 or vol <= 1e-6:
        if kind == "CE":
            return 1.0 if S > K else 0.0
        return -1.0 if S < K else 0.0
    sd = vol * math.sqrt(T)
    d1 = (math.log(S / K) + (r + 0.5 * vol * vol) * T) / sd
    return _ncdf(d1) if kind == "CE" else _ncdf(d1) - 1.0


def implied_vol(price, S, K, T, kind, r=0.0, lo=0.02, hi=4.0):
    """Bisection: robust for deep OTM quotes where Newton wanders off."""
    if price <= 0 or S <= 0 or K <= 0 or T <= 0:
        return None
    intrinsic = max(0.0, S - K) if kind == "CE" else max(0.0, K - S)
    if price < intrinsic * 0.98:      # stale quote, not an option price
        return None
    if bs_price(S, K, T, hi, kind, r) < price:
        return None
    for _ in range(80):
        mid = 0.5 * (lo + hi)
        if bs_price(S, K, T, mid, kind, r) < price:
            lo = mid
        else:
            hi = mid
        if hi - lo < 1e-5:
            break
    return 0.5 * (lo + hi)


# ------------------------------------------------------------------ NSE bhavcopy option chain
_bhav_lock = threading.Lock()


def _bhav_path(symbol, day):
    return os.path.join(BHAV_DIR, f"{SYMBOLS[symbol]['bhav']}-{day}.json.gz")


def _holidays():
    return set(_read_json(HOLIDAY_FILE, []) or [])


def _mark_holiday(day):
    with _bhav_lock:
        hol = _holidays()
        if day not in hol:
            hol.add(day)
            _write_json(HOLIDAY_FILE, sorted(hol))


def _fetch_bhav(day_iso):
    """Bytes of one day's F&O bhavcopy zip, or None when NSE has no file (holiday/weekend)."""
    url = BHAV_URL.format(day=day_iso.replace("-", ""))
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            if exc.code in (403, 429, 500, 502, 503) and attempt < 2:
                time.sleep(1.5 * (attempt + 1))
                continue
            raise
        except Exception:
            if attempt < 2:
                time.sleep(1.5 * (attempt + 1))
                continue
            raise
    return None


def _parse_bhav(raw, symbol, day_iso):
    """Keep only this index's options: {expiry: {strike: {'ce':[...], 'pe':[...]}}}."""
    out, underlying, lot = {}, 0.0, 0
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        name = next(n for n in zf.namelist() if n.lower().endswith(".csv"))
        text = zf.read(name).decode("utf-8", "replace")
    want = SYMBOLS[symbol]["bhav"]
    for row in csv.DictReader(io.StringIO(text)):
        if row.get("TckrSymb") != want or row.get("OptnTp") not in ("CE", "PE"):
            continue
        kind = row["OptnTp"].lower()
        try:
            strike = float(row.get("StrkPric") or 0)
            close = float(row.get("ClsPric") or 0)
            vol = float(row.get("TtlTradgVol") or 0)
            oi = float(row.get("OpnIntrst") or 0)
            u = float(row.get("UndrlygPric") or 0)
        except ValueError:
            continue
        if strike <= 0:
            continue
        if u > 0:
            underlying = u
        if not lot:
            try:
                lot = int(float(row.get("NewBrdLotQty") or 0))
            except ValueError:
                lot = 0
        exp = (row.get("XpryDt") or "")[:10]
        if not exp:
            continue
        bucket = out.setdefault(exp, {}).setdefault(f"{strike:.2f}", {})
        bucket[kind] = [round(close, 2), int(vol), int(oi)]
    if not out:
        return None
    return {"d": day_iso, "u": round(underlying, 2), "lot": lot or 0, "x": out}


def chain(symbol, day, progress=None, force=False):
    """One day's option chain (cached). Returns None for a non-trading day."""
    day = _iso(day)
    path = _bhav_path(symbol, day)
    if not force:
        cached = _read_json_gz(path)
        if cached is not None:
            return cached if cached.get("x") else None
    if day in _holidays():
        return None
    raw = _fetch_bhav(day)
    if raw is None:
        # NSE publishes the file in the evening: a missing file for today/yesterday is
        # "not out yet", not a holiday. Only older gaps are remembered as holidays.
        if _d(day) < date.today() - timedelta(days=1):
            _mark_holiday(day)
        return None
    parsed = _parse_bhav(raw, symbol, day)
    if parsed is None:   # file exists but no rows for this index (should not happen)
        _mark_holiday(day)
        return None
    _write_json(path, parsed, gz=True)
    return parsed


def _read_json_gz(path):
    try:
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def trading_days(symbol, start, end, progress=None):
    """Trading days in [start, end] that have a bhavcopy for this symbol (parallel downloads)."""
    days, d = [], _d(start)
    end = _d(end)
    while d <= end:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    missing = [x for x in days if not os.path.exists(_bhav_path(symbol, x))]
    done = 0

    def work(x):
        nonlocal done
        try:
            chain(symbol, x)
        finally:
            with _bhav_lock:
                done += 1
                if progress and (done % 5 == 0 or done == len(missing)):
                    progress(f"Option chain: {done}/{len(missing)} days downloaded")

    if missing:
        with ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(work, missing))
    return [x for x in days if os.path.exists(_bhav_path(symbol, x))]


# ------------------------------------------------------------------ spot (Angel SmartAPI, H1)
_spot_lock = threading.Lock()


def _spot_path(symbol, ym):
    return os.path.join(SPOT_DIR, f"{symbol}-{ym}.json.gz")


def _angel_bars(ticker, exch_kind, start_dt, end_dt):
    """H1 candles straight from the Angel plugin's REST path (shared rate budget)."""
    from feeds import angel
    token, exchange, _key = angel._resolve(ticker)
    rows = angel._candle_rows(token, exchange, "ONE_HOUR",
                              int(start_dt.timestamp()), int(end_dt.timestamp()))
    return [{"t": r["time"], "c": r["close"], "o": r["open"], "h": r["high"], "l": r["low"]} for r in rows]


def spot_hourly(symbol, start, end, progress=None):
    """Hourly spot bars for [start, end], cached month by month."""
    ticker = SYMBOLS[symbol]["angel"]
    out, d = {}, _d(start).replace(day=1)
    end_d = _d(end)
    months = []
    while d <= end_d:
        months.append(d)
        d = (d.replace(day=28) + timedelta(days=5)).replace(day=1)
    for i, m in enumerate(months):
        ym = m.strftime("%Y-%m")
        path = _spot_path(symbol, ym)
        cached = _read_json_gz(path)
        if cached is None:
            m_end = (m.replace(day=28) + timedelta(days=5)).replace(day=1) - timedelta(days=1)
            a = datetime(m.year, m.month, 1, 0, 0, tzinfo=IST)
            b = datetime(m_end.year, m_end.month, m_end.day, 23, 59, tzinfo=IST)
            try:
                bars = _angel_bars(ticker, None, a, b)
            except Exception as exc:
                if progress:
                    progress(f"Spot {ym}: {exc.__class__.__name__} - using daily closes only")
                bars = []
            cached = {"bars": bars}
            if bars:
                _write_json(path, cached, gz=True)
        for b in cached.get("bars", []):
            out[b["t"]] = b
        if progress:
            progress(f"Spot hourly: {i + 1}/{len(months)} months")
    if not out:   # never leave the engine without a spot path
        for day in (x for x in _chain_days(symbol)):
            ch = chain(symbol, day)
            if ch and ch.get("u"):
                out[int(_dt(_d(day), "15:30").timestamp())] = {"t": int(_dt(_d(day), "15:30").timestamp()), "c": ch["u"]}
    return [out[t] for t in sorted(out)]


def _chain_days(symbol):
    if not os.path.isdir(BHAV_DIR):
        return []
    pre = SYMBOLS[symbol]["bhav"] + "-"
    return [f[len(pre):-8] for f in sorted(os.listdir(BHAV_DIR)) if f.startswith(pre) and f.endswith(".json.gz")]


# ------------------------------------------------------------------ expiries
def _monthly_expiries(expiries):
    """Last expiry of each calendar month."""
    by = {}
    for e in sorted(expiries):
        by[(e.year, e.month)] = e
    return set(by.values())


def pick_expiries(exp_iso, entry_day, target_dte):
    """(near_expiry, monthly_expiry or None). near = days-to-expiry closest to target_dte."""
    cands = [(_d(e), e) for e in exp_iso if _d(e) > entry_day]
    if not cands:
        return None, None
    near_d, near = min(cands, key=lambda x: (abs((x[0] - entry_day).days - target_dte),
                                             (x[0] - entry_day).days))
    monthly = _monthly_expiries([_d(e) for e in exp_iso])
    same_month = [m for m in monthly if (m.year, m.month) == (near_d.year, near_d.month)]
    mon = max(same_month) if same_month else near_d
    if mon == near_d:      # near expiry IS the monthly one: hedge rolls to next month
        later = sorted(m for m in monthly if m > near_d)
        mon = later[0] if later else near_d
    return near, next((e for e in exp_iso if _d(e) == mon), near)


# ------------------------------------------------------------------ strategy engine
def _strike_from(chain_day, expiry, kind, target_points, ref_strike, step):
    """Nearest listed strike to ref_strike + target_points (in the money direction of `kind`)."""
    strikes = sorted(float(k) for k, v in chain_day["x"].get(expiry, {}).items() if v.get(kind.lower()))
    if not strikes:
        return None
    want = ref_strike + target_points
    return min(strikes, key=lambda s: abs(s - want))


def _pick_delta_strike(chain_day, expiry, kind, delta, S, T, r):
    """Strike whose |delta| is closest to `delta`, from real quotes (IV inverted per strike)."""
    best = None
    for ks, kinds in chain_day["x"].get(expiry, {}).items():
        q = kinds.get(kind.lower())
        if not q:
            continue
        px = q[0]
        if px <= 0.05:
            continue
        K = float(ks)
        iv = implied_vol(px, S, K, T, kind, r)
        if not iv:
            continue
        dlt = abs(bs_delta(S, K, T, iv, kind, r))
        score = abs(dlt - delta)
        if best is None or score < best[0]:
            best = (score, K, iv, dlt, px)
    return best


def _leg_price(chain_day, leg):
    q = chain_day["x"].get(leg["expiry"], {}).get(f"{leg['strike']:.2f}", {}).get(leg["kind"].lower())
    return q[0] if q else None


def _costs(legs, lot):
    """Estimated Indian F&O costs on premium (brokerage, STT, exchange, GST, stamp). Not exact."""
    sell_turn = sum(l["entry"] * l["lots"] * lot for l in legs if l["side"] == "S")
    buy_turn = sum(l["entry"] * l["lots"] * lot for l in legs if l["side"] == "B")
    brokerage = 20.0 * 2 * len(legs)                  # flat Rs 20 per executed order
    stt = 0.001 * sell_turn                            # 0.1% on sale of options
    txn = 0.0003503 * (sell_turn + buy_turn)           # NSE F&O exchange transaction charge
    sebi = 0.0000010 * (sell_turn + buy_turn)
    gst = 0.18 * (brokerage + txn + sebi)
    stamp = 0.00003 * buy_turn
    return round(brokerage + stt + txn + sebi + gst + stamp, 2)


def _hours_of(spot, day):
    """Angel H1 bars for one day as (timestamp of the hour's end, close).
    The 15:15 bar is the partial closing bar (it ends 15:30) and is dropped: the real
    bhavcopy close is drawn at 15:30 instead, so an hour is never counted twice."""
    a = int(_dt(day, "09:00").timestamp())
    b = int(_dt(day, "16:00").timestamp())
    last = int(_dt(day, "15:15").timestamp())
    return [(x["t"] + 3600, x["c"]) for x in spot if a <= x["t"] <= b and x["t"] < last]


def run(settings=None, progress=None):
    """Run the whole backtest. Returns the result dict (also written to data/backtest/runs)."""
    s = {**DEFAULTS, **(settings or {})}
    symbol = s["symbol"] if s["symbol"] in SYMBOLS else "NIFTY"
    progress = progress or (lambda msg: None)
    start = _d(s["start"])
    end = _d(s["end"] or date.today())
    if end < start:
        raise ValueError("end date is before the start date")

    progress("Loading trading calendar and NSE option chains (first run downloads them)")
    days = trading_days(symbol, start, end, progress)
    if not days:
        raise RuntimeError("no NSE F&O bhavcopy found in that date range (is NSE reachable?)")
    progress(f"Trading days: {len(days)}. Loading hourly spot from Angel One")
    spot = spot_hourly(symbol, days[0], days[-1], progress)
    if not spot:
        progress("Hourly spot unavailable: marks use daily closes only")
    spot_by_day = {}
    for bar in spot:
        spot_by_day.setdefault(datetime.fromtimestamp(bar["t"], IST).date(), []).append(bar)

    step = SYMBOLS[symbol]["step"]
    r = float(s.get("risk_free") or 0)
    weeks = []
    curve = []
    cum = 0.0
    week_no = 0

    # every entry weekday in range
    d0 = start
    d0 += timedelta(days=(int(s["entry_weekday"]) - d0.weekday()) % 7)
    entry_days = []
    while d0 <= end:
        entry_days.append(d0)
        d0 += timedelta(days=7)

    total = len(entry_days)
    for i, monday in enumerate(entry_days, 1):
        progress(f"Week {i}/{total}: {monday.isoformat()}")
        entry_day = next((x for x in days if x >= monday), None)
        if not entry_day:
            continue
        target_exit = entry_day + timedelta(days=(int(s["exit_weekday"]) - entry_day.weekday()) % 7)
        exit_day = next((x for x in reversed(days) if entry_day < x <= target_exit), None)
        if not exit_day:
            weeks.append({"week": i, "status": "skipped", "reason": "no exit trading day in the same week",
                          "entry_date": entry_day.isoformat()})
            continue
        entry_chain = chain(symbol, entry_day)
        if not entry_chain or not entry_chain.get("u"):
            weeks.append({"week": i, "status": "skipped", "reason": "no option chain on the entry day",
                          "entry_date": entry_day.isoformat()})
            continue

        S0 = entry_chain["u"]
        lot = int(s.get("lot_size") or entry_chain.get("lot") or 65)
        expiries = list(entry_chain["x"].keys())
        near, monthly = pick_expiries(expiries, entry_day, int(s["target_dte"]))
        if not near:
            weeks.append({"week": i, "status": "skipped", "reason": "no expiry after the entry day",
                          "entry_date": entry_day.isoformat()})
            continue
        near_d = _d(near)
        T_entry = _years(_dt(entry_day, s["entry_time"]), _dt(near_d, "15:30"))

        legs, notes = [], []

        def add(side, kind, strike, expiry, lots, tag, from_expiry=None):
            src = expiry
            px = entry_chain["x"].get(src, {}).get(f"{strike:.2f}", {})
            q = px.get(kind.lower())
            if not q or q[0] <= 0:
                # strike not listed in that expiry: use the nearest listed strike
                alt = _strike_from(entry_chain, src, kind, 0, strike, step)
                if alt is None:
                    return False
                q = entry_chain["x"][src][f"{alt:.2f}"][kind.lower()]
                if abs(alt - strike) > 0.5:
                    notes.append(f"{tag}: strike {strike:.0f} not listed in {src}, used {alt:.0f}")
                strike = alt
            legs.append({"side": side, "kind": kind, "strike": float(strike), "expiry": src,
                         "lots": int(lots), "entry": float(q[0]), "tag": tag})
            return True

        ce = _pick_delta_strike(entry_chain, near, "CE", float(s["delta"]), S0, T_entry, r)
        pe = _pick_delta_strike(entry_chain, near, "PE", float(s["delta"]), S0, T_entry, r)
        if not ce or not pe:
            weeks.append({"week": i, "status": "skipped", "reason": "no delta-matched strikes with quotes",
                          "entry_date": entry_day.isoformat()})
            continue
        ce_k, pe_k = ce[1], pe[1]
        ok = add("S", "CE", ce_k, near, s["lots_sell"], "sell CE")
        ok &= add("S", "PE", pe_k, near, s["lots_sell"], "sell PE")
        if s.get("monthly_hedge") and monthly and monthly != near:
            ok &= add("B", "CE", ce_k, monthly, s["lots_buy_same"], "buy CE (monthly)")
            ok &= add("B", "PE", pe_k, monthly, s["lots_buy_same"], "buy PE (monthly)")
        wing_exp = monthly if s.get("wing_expiry") == "monthly" and monthly else near
        if int(s.get("wing_points") or 0) > 0 and int(s.get("wing_lots") or 0) > 0:
            wc = _strike_from(entry_chain, wing_exp, "CE", +abs(int(s["wing_points"])), ce_k, step)
            wp = _strike_from(entry_chain, wing_exp, "PE", -abs(int(s["wing_points"])), pe_k, step)
            if wc:
                ok &= add("B", "CE", wc, wing_exp, s["wing_lots"], "buy CE wing")
            if wp:
                ok &= add("B", "PE", wp, wing_exp, s["wing_lots"], "buy PE wing")
        if not ok:
            weeks.append({"week": i, "status": "skipped", "reason": "a leg could not be priced",
                          "entry_date": entry_day.isoformat()})
            continue

        # IV at entry, per leg, from its real quote
        for l in legs:
            T = _years(_dt(entry_day, s["entry_time"]), _dt(_d(l["expiry"]), "15:30"))
            l["iv"] = implied_vol(l["entry"], S0, l["strike"], T, l["kind"], r) or 0.20
            l["iv_prev"] = l["iv"]      # intraday marks use the last KNOWN iv, never today's close

        # ---------------- walk the holding period
        hold_days = [x for x in days if entry_day <= x <= exit_day]
        week_curve = []
        peak = 0.0
        worst = 0.0
        for day in hold_days:
            ch = chain(symbol, day)
            hours = _hours_of(spot_by_day.get(day, []), day)
            # 1) hourly marks, valued with the last IV that was known BEFORE this day opened
            for l in legs:
                if _d(l["expiry"]) < day:
                    continue
                iv_use = l.get("iv_prev", l["iv"])
                if day == entry_day:
                    continue      # the entry mark itself is placed at the entry time below
                for ts, px in hours:
                    if day == exit_day and ts > int(_dt(exit_day, s["exit_time"]).timestamp()) + 3600:
                        continue
                    T = _years(datetime.fromtimestamp(ts, IST), _dt(_d(l["expiry"]), "15:30"))
                    week_curve.append((ts, l, bs_price(px, l["strike"], T, iv_use, l["kind"], r), px, False))
            # 2) real end-of-day anchor (bhavcopy close) - also the exit price on the last day - then
            #    recalibrate the IV for the next day's marks from that real close
            for l in legs:
                if _d(l["expiry"]) < day:
                    continue
                real = _leg_price(ch, l) if ch else None
                u = (ch or {}).get("u") or S0
                if real:
                    week_curve.append((int(_dt(day, "15:30").timestamp()), l, float(real), u, True))
                if day == exit_day:
                    l["exit"] = float(real) if real else None
                if real and real > 0.1:
                    T = _years(_dt(day, "15:30"), _dt(_d(l["expiry"]), "15:30"))
                    iv = implied_vol(real, u, l["strike"], T, l["kind"], r)
                    if iv:
                        l["iv_prev"] = iv

        # legs whose quote vanished on the exit day: value them with their last IV
        for l in legs:
            if l.get("exit") is None:
                T = _years(_dt(exit_day, "15:30"), _dt(_d(l["expiry"]), "15:30"))
                Sx = (chain(symbol, exit_day) or {}).get("u") or S0
                l["exit"] = round(bs_price(Sx, l["strike"], T, l.get("iv_prev", l["iv"]), l["kind"], r), 2)

        # build the P&L curve for the week
        by_ts = {}
        for ts, l, mark, px, is_eod in week_curve:
            by_ts.setdefault(ts, []).append((l, mark, px, is_eod))
        gross = 0.0
        for l in legs:
            qty = l["lots"] * lot
            gross += (l["entry"] - l["exit"]) * qty if l["side"] == "S" else (l["exit"] - l["entry"]) * qty
        gross = round(gross, 2)
        cost = _costs(legs, lot) if s.get("costs") else 0.0
        net = round(gross - cost, 2)

        # the curve is net: costs are paid on the entry/exit orders, so they are carried from entry on
        wk_points = [{"t": int(_dt(entry_day, s["entry_time"]).timestamp()), "pnl": round(-cost, 2),
                      "spot": S0, "eod": False}]
        for ts in sorted(by_ts):
            marks = by_ts[ts]
            if len({id(m[0]) for m in marks}) < len(legs):
                continue          # a partial hour (not every leg priced): skip
            pnl = 0.0
            for l, mark, px, _eod in marks:
                qty = l["lots"] * lot
                pnl += (l["entry"] - mark) * qty if l["side"] == "S" else (mark - l["entry"]) * qty
            wk_points.append({"t": ts, "pnl": round(pnl - cost, 2), "spot": round(marks[0][2], 2),
                              "eod": all(m[3] for m in marks)})
            peak = max(peak, pnl)
            worst = min(worst, pnl - peak)

        cum += net
        for p in wk_points:
            curve.append({"t": p["t"], "pnl": round(p["pnl"] + (cum - net), 2), "wk": round(p["pnl"], 2),
                          "spot": p["spot"], "eod": p["eod"], "week": week_no + 1})
        week_no += 1
        if exit_day < target_exit:
            notes.append(f"exit moved to {exit_day.isoformat()}: no later trading day with published data this week")
        weeks.append({
            "week": week_no,
            "status": "ok",
            "entry_date": entry_day.isoformat(),
            "entry_time": s["entry_time"],
            "exit_date": exit_day.isoformat(),
            "exit_time": s["exit_time"],
            "expiry": near,
            "expiry_monthly": monthly,
            "dte": (near_d - entry_day).days,
            "spot_entry": S0,
            "spot_exit": (chain(symbol, exit_day) or {}).get("u") or S0,
            "lot": lot,
            "delta_ce": round(ce[3], 3),
            "delta_pe": round(pe[3], 3),
            "strikes": {"ce": ce_k, "pe": pe_k},
            "legs": [{k: (round(v, 2) if isinstance(v, float) else v) for k, v in l.items() if k != "iv"} for l in legs],
            "gross": gross,
            "costs": cost,
            "net": net,
            "cum": round(cum, 2),
            "max_mtm": round(min((p["pnl"] for p in wk_points), default=0.0), 2),
            "max_dd": round(worst, 2),
            "notes": notes,
            "curve": wk_points,
        })

    ok_weeks = [w for w in weeks if w.get("status") == "ok"]
    wins = [w for w in ok_weeks if w["net"] > 0]
    nets = [w["net"] for w in ok_weeks]
    run_peak, max_dd = 0.0, 0.0
    for p in curve:
        run_peak = max(run_peak, p["pnl"])
        max_dd = min(max_dd, p["pnl"] - run_peak)
    summary = {
        "weeks": len(ok_weeks),
        "skipped": len([w for w in weeks if w.get("status") != "ok"]),
        "total_net": round(sum(nets), 2),
        "total_gross": round(sum(w["gross"] for w in ok_weeks), 2),
        "total_costs": round(sum(w["costs"] for w in ok_weeks), 2),
        "win_rate": round(100.0 * len(wins) / len(ok_weeks), 1) if ok_weeks else 0.0,
        "avg_week": round(sum(nets) / len(nets), 2) if nets else 0.0,
        "best_week": max(nets) if nets else 0.0,
        "worst_week": min(nets) if nets else 0.0,
        "max_drawdown": round(max_dd, 2),
        "first": ok_weeks[0]["entry_date"] if ok_weeks else None,
        "last": ok_weeks[-1]["exit_date"] if ok_weeks else None,
    }
    result = {
        "settings": s,
        "symbol": symbol,
        "summary": summary,
        "weeks": weeks,
        "curve": curve,
        "data": {
            "option_prices": "NSE F&O bhavcopy, daily close/settlement (real, includes expired contracts)",
            "spot_hourly": "Angel One SmartAPI H1 candles" if spot else "daily closes only",
            "intraday_option_marks": "Black-Scholes, IV recalibrated to each day's real bhavcopy close",
            "entry_fill": f"{s['entry_time']} targeted, priced at the entry day's bhavcopy close (15:30)",
            "trading_days": len(days),
            "generated": datetime.now(IST).strftime("%Y-%m-%d %H:%M IST"),
        },
    }
    try:
        os.makedirs(RUNS_DIR, exist_ok=True)
        _write_json(os.path.join(RUNS_DIR, f"{int(time.time())}-{symbol}.json"), result)
    except OSError:
        pass
    return result


# ------------------------------------------------------------------ background jobs (used by the hub API)
_jobs = {}
_jobs_lock = threading.Lock()


def start_job(settings):
    job_id = uuid.uuid4().hex[:12]
    job = {"id": job_id, "state": "running", "progress": "starting", "at": time.time(), "result": None, "error": None}
    with _jobs_lock:
        _jobs[job_id] = job
        for k in list(_jobs)[:-12]:       # keep the last dozen
            _jobs.pop(k, None)

    def work():
        try:
            res = run(settings, progress=lambda m: job.update(progress=m))
            job.update(result=res, state="done", progress=f"done: {res['summary']['weeks']} weeks")
        except Exception as exc:
            job.update(state="error", error=f"{exc.__class__.__name__}: {exc}")
        finally:
            job["at"] = time.time()

    threading.Thread(target=work, daemon=True, name=f"backtest-{job_id}").start()
    return job_id


def job_status(job_id, with_result=False):
    job = _jobs.get(job_id)
    if not job:
        return None
    out = {k: v for k, v in job.items() if k != "result"}
    if with_result:
        out["result"] = job.get("result")
    return out


def api(action, payload=None):
    """Routes used by hub/backtest.html (GET /api/backtest/<action>, POST /api/backtest/run)."""
    p = payload or {}
    if action == "run":
        settings = {k: v for k, v in (p.get("settings") or p).items() if k in DEFAULTS}
        return {"ok": True, "id": start_job(settings)}
    if action == "status":
        st = job_status(p.get("id", ""), with_result=bool(p.get("result")))
        return {"ok": True, "job": st} if st else {"ok": False, "error": "unknown job"}
    if action == "defaults":
        return {"ok": True, "defaults": DEFAULTS, "symbols": list(SYMBOLS)}
    raise KeyError(action)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="TradeX options backtest (research only)")
    ap.add_argument("--symbol", default="NIFTY")
    ap.add_argument("--start", default=DEFAULTS["start"])
    ap.add_argument("--end", default=None)
    ap.add_argument("--delta", type=float, default=DEFAULTS["delta"])
    ap.add_argument("--dte", type=int, default=DEFAULTS["target_dte"])
    ap.add_argument("--lots", type=int, default=DEFAULTS["lots_sell"])
    ap.add_argument("--wing", type=int, default=DEFAULTS["wing_points"])
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    res = run({"symbol": a.symbol, "start": a.start, "end": a.end, "delta": a.delta,
               "target_dte": a.dte, "lots_sell": a.lots, "wing_points": a.wing},
              progress=lambda m: print("  " + m, flush=True))
    print(json.dumps(res["summary"], indent=2))
    for w in res["weeks"]:
        if w["status"] == "ok":
            print(f"{w['entry_date']} -> {w['exit_date']}  {w['strikes']['ce']:.0f}/{w['strikes']['pe']:.0f}"
                  f"  exp {w['expiry']}  net {w['net']:>10,.0f}")
    if a.out:
        _write_json(a.out, res)
        print("written:", a.out)
