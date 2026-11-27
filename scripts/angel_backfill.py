#!/usr/bin/env python3
"""Store Angel One candles on disk as CSV so TradeX history survives without the API.

Angel only serves ~100 days of intraday candles and ~2000 days of daily candles, so
"two years" is only possible for D1. Default run writes:
  D1, ~2 years (520 trading days), every chart (indices + NSE equities)
  M5, the full ~100 days Angel keeps, indices and the NIFTY 50 stocks only

Files: data/history/<tf>/<TICKER>.csv  (time, datetime IST, open, high, low, close, volume)
Manifest: data/history/manifest.json  (rows, first/last candle, when it was written)

Usage (from the repo root):
  .venv/bin/python scripts/angel_backfill.py                # the default set above
  .venv/bin/python scripts/angel_backfill.py --dry-run      # just say what it would fetch
  .venv/bin/python scripts/angel_backfill.py --tf D1 --groups all --bars 520
  .venv/bin/python scripts/angel_backfill.py --force        # re-download finished files
It is resumable: a symbol whose CSV already exists is skipped, so stopping and
restarting costs nothing. Read-only, like the rest of the project.
"""
from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import os
import sys
import time
from datetime import datetime

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
HISTORY = os.path.join(ROOT, "data", "history")
MANIFEST = os.path.join(HISTORY, "manifest.json")

_spec = importlib.util.spec_from_file_location("angel", os.path.join(ROOT, "hub", "feeds", "angel.py"))
angel = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(angel)

angel.RATE_INTERVAL = 0.6           # 2 requests/s is Angel's per-second cap; stay well under it
CALLS_PER_MIN = 30                  # getCandleData: 2/s, 40/min, 2000/hour - 1800/h stays clear
RETRY_PAUSE = 20                    # seconds to wait when the hourly rate cap answers 403
MAX_TRIES = 6
WINDOW_DAYS = {"D1": 760, "intraday": 100}    # 2 calendar years of daily; Angel's intraday cap


def _log(msg):
    print(msg, flush=True)


def _install_minute_limiter():
    """Hard cap on upward calls: Angel 403s the whole run if one minute goes over 80.
    Wrapping the lowest level keeps the count right even when a symbol needs 3 chunks."""
    original = angel._candle_rows
    recent = []

    def limited(*a, **kw):
        while True:
            now = time.monotonic()
            recent[:] = [t for t in recent if now - t < 60]
            if len(recent) < CALLS_PER_MIN:
                break
            time.sleep(1.0)
        recent.append(time.monotonic())
        return original(*a, **kw)

    angel._candle_rows = limited


def _ist(epoch_s):
    return datetime.fromtimestamp(epoch_s, angel.IST).strftime("%Y-%m-%d %H:%M:%S+05:30")


def _pick(groups):
    """Symbol list for the requested groups ('all', 'index', 'nifty50')."""
    uni = angel._universe()
    items = (uni["indices"] + uni["equities"]) if uni else angel._fallback_universe()
    if groups == "all":
        return items
    wanted = {"index": {"index"}, "nifty50": {"index", "nifty50"}}[groups]
    return [i for i in items if i.get("group") in wanted]


def _fetch(ticker, tf, bars):
    """One request per symbol: Angel happily returns 1500 daily candles in one go, and a
    single FIVE_MINUTE request comes back with its full ~4900 bars (about 66 sessions,
    everything inside its ~100 day retention). history() would spend 3 calls on this."""
    token, exchange, _key = angel._resolve(ticker)
    interval = angel.INTERVAL["D1" if tf == "D1" else ("H1" if tf == "H4" else tf)]
    days = WINDOW_DAYS["D1" if tf == "D1" else "intraday"]
    now = int(time.time())
    for attempt in range(1, MAX_TRIES + 1):
        try:
            rows = angel._candle_rows(token, exchange, interval, now - days * 86400, now)
            if rows:
                return rows[-bars:] if bars else rows
            # a dead/stale symbol simply has nothing in the window: widen once before giving up
            return angel._span_bars(token, exchange, tf, min(bars or 200, 300), now)
        except angel.AngelError as exc:
            text = str(exc)
            if "rate" in text.lower() or "Access denied" in text or "403" in text:
                _log(f"    rate limit at {ticker} ({text[:70]}) - pausing {RETRY_PAUSE}s")
                time.sleep(RETRY_PAUSE)
                continue
            raise
    raise angel.AngelError(f"gave up on {ticker} after {MAX_TRIES} rate-limit retries")


def _write_csv(path, bars):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["time", "datetime", "open", "high", "low", "close", "volume"])
        for b in bars:
            w.writerow([b["time"], _ist(b["time"]), b["open"], b["high"], b["low"], b["close"], b["volume"]])
    os.replace(tmp, path)


def _manifest():
    try:
        with open(MANIFEST, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def _save_manifest(data):
    os.makedirs(HISTORY, exist_ok=True)
    tmp = MANIFEST + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=1, sort_keys=True)
    os.replace(tmp, MANIFEST)


def main(argv=None):
    p = argparse.ArgumentParser(description="Store Angel One candles as CSV (read-only).")
    p.add_argument("--tf", default="D1", choices=["D1", "M5", "M15", "H1"], help="timeframe for this run")
    p.add_argument("--groups", default=None, choices=["all", "index", "nifty50"],
                   help="symbol set (default: all for D1, nifty50+indices for intraday)")
    p.add_argument("--bars", type=int, default=None, help="candles wanted (default: 520 for D1, 25000 intraday)")
    p.add_argument("--limit", type=int, default=0, help="only the first N symbols (testing)")
    p.add_argument("--force", action="store_true", help="re-download symbols whose CSV already exists")
    p.add_argument("--dry-run", action="store_true", help="print the plan, fetch nothing")
    a = p.parse_args(argv)

    intraday = a.tf != "D1"
    groups = a.groups or ("nifty50" if intraday else "all")
    bars = a.bars or (25000 if intraday else 520)
    max_days = angel.MINUTE_RETENTION_DAYS if intraday else angel.DAY_RETENTION_DAYS

    if not angel.configured():
        _log("Angel One is not configured: run the login (see --help) or set ANGEL_* in .env")
        return 2
    st = angel.session_status()
    _log(f"Angel One: logged_in={st['logged_in']} totp_secret={st['totp_secret']} "
         f"key={st['api_key']} client={st['client_id']}")
    if not st["logged_in"]:
        angel.login()

    items = _pick(groups)
    if a.limit:
        items = items[: a.limit]
    _log(f"plan: tf={a.tf} groups={groups} symbols={len(items)} bars={bars} "
         f"(Angel keeps ~{max_days} days of {'intraday' if intraday else 'daily'} data)")

    todo = [i for i in items if a.force or not os.path.exists(os.path.join(HISTORY, a.tf, i["ticker"] + ".csv"))]
    _log(f"to download: {len(todo)} (already stored: {len(items) - len(todo)})")
    if a.dry_run or not todo:
        return 0

    _install_minute_limiter()
    manifest = _manifest()
    started = time.time()
    done = failed = 0
    for idx, item in enumerate(todo, 1):
        ticker = item["ticker"]
        t0 = time.time()
        try:
            bars_out = _fetch(ticker, a.tf, bars)
        except Exception as exc:                       # one bad symbol must not stop the run
            failed += 1
            _log(f"[{idx}/{len(todo)}] {ticker}: FAILED {str(exc)[:100]}")
            continue
        if not bars_out:
            failed += 1
            _log(f"[{idx}/{len(todo)}] {ticker}: no candles returned (skipped)")
            continue
        path = os.path.join(HISTORY, a.tf, ticker + ".csv")
        _write_csv(path, bars_out)
        done += 1
        manifest[f"{a.tf}/{ticker}"] = {
            "ticker": ticker, "name": item.get("name", ""), "tf": a.tf, "rows": len(bars_out),
            "first": _ist(bars_out[0]["time"]), "last": _ist(bars_out[-1]["time"]),
            "updated": int(time.time()),
        }
        if done % 25 == 0:
            _save_manifest(manifest)
        elapsed = time.time() - started
        eta = elapsed / idx * (len(todo) - idx)
        _log(f"[{idx}/{len(todo)}] {ticker:<22} {len(bars_out):>5} rows "
             f"{_ist(bars_out[0]['time'])[:10]}..{_ist(bars_out[-1]['time'])[:10]} "
             f"{time.time() - t0:.1f}s | eta {int(eta // 60)}m")

    _save_manifest(manifest)
    _log(f"done: {done} stored, {failed} failed, {len(todo)} attempted, "
         f"{int((time.time() - started) // 60)}m {int((time.time() - started) % 60)}s")
    _log(f"files in {os.path.join('data', 'history', a.tf)} - manifest {os.path.join('data', 'history', 'manifest.json')}")
    return 0 if not failed else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        _log("stopped - every finished symbol is already saved, rerun to continue")
        raise SystemExit(130)
