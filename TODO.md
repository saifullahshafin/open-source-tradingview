# TradeX to-do

## Concurrency and Angel rate limits

Angel's cap (3/s, 120/min, 5000/h) is per client code, so "load balancing" across processes or
connections cannot raise it: more parallel paths reach 403 sooner. The only levers are fewer calls
and one shared budget. Done so far:

- `hub/feeds/base.py` — one shared poller per (source, symbol, timeframe): every chart of the same
  symbol subscribes to one upstream request stream instead of polling for itself. Poll interval now
  follows the timeframe (M1 3 s, M5 15 s, H1 30 s) instead of a flat 2 s, and a poller that finds a
  gap resends the whole window so charts do not show holes.
- `hub/feeds/angel.py` — candles are cached in `data/cache/angel/<tf>/<TICKER>.csv`; a chart open
  that the cache already covers costs zero API calls, a stale cache paints immediately and refreshes
  in the background, and the hourly cap (4500) is now enforced alongside the per-second and
  per-minute ones. A wait longer than 20 s fails fast with a readable message instead of hanging.
- Measured: 8 symbols opened twice — 5.2 s first time (throttled at the API), 0.02 s the second.

Still to do:

- Live data over Angel's own websocket (SmartAPI) instead of REST polling: one connection carries
  up to ~1000 tokens, which removes the per-symbol poll entirely. That is the real answer for many
  symbols at once; REST polling then only fills history.
- `scripts/angel_backfill.py` should take its calls from the same budget file (it mostly does) and
  yield to interactive chart requests when the minute is nearly full.
- A small `GET /api/feeds/angel/budget` so the hub can show "42/110 this minute" instead of only
  logging a 403 after the fact.

Read-only constraint stays: nothing here may place orders.
