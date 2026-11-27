# TradeX to-do

## Concurrency: serve several chart requests at once

The hub should stay responsive when several charts, tabs or backtests ask for data together, and the
Angel One path should queue work instead of blocking.

Current limits to address:

- `hub/feeds/angel.py` reuses one global `HTTPSConnection` guarded by `_conn_lock`, so every Angel call is
  serialised no matter how many request threads arrive. `_rate_budget()` also serialises on a file lock.
- The WebSocket handler calls the blocking `history()` on the asyncio event loop, so one slow Angel fetch
  can stall every other chart on that socket.
- The Angel caps (2/s, 40/min, 2000/h per client code) still have to be shared with `scripts/angel_backfill.py`.

Suggested direction: a small per-thread/pooled connection setup, run blocking history calls off the event
loop (`asyncio.to_thread` / executor), and a single fair queue that owns the rate budget across the hub and
the backfill. Verify with N parallel `/api/bars` calls and N open chart tabs, measuring p95 latency before
and after.

Read-only constraint stays: nothing here may place orders.
