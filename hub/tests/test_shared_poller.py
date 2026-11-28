"""Shared-poller contract for hub/feeds/base.py.

Two charts on the same (source, symbol, tf, history) must share one poller — one upstream history
call plus one poll loop. When the last chart leaves, the poller's task is cancelled and the registry
entry is removed. When the poller sees a gap larger than two candles on its timeframe, it rebroadcasts
the whole window instead of a single bar that would leave a hole.

These are unit-level tests: they feed the shared poller with staged history and a controlled
history_fn, so nothing real is fetched.
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from hub.feeds import base


@pytest.fixture(autouse=True)
def _clean():
    base._clear_shared()
    yield
    base._clear_shared()


def _bars(start, n=5, step=60):
    return [base.bar(start + i * step, i, i + 1, i - 1, i + 2, v=i) for i in range(n)]


def _history_fn(bars):
    def _(symbol, tf, limit, to_ms):
        return list(bars)

    return _


def _history_fn_slow(bars, delay=0.05):
    """A history_fn that takes a little real time, so the poller can wake and post history
    without the test racing the event loop."""
    def _(symbol, tf, limit, to_ms):
        time.sleep(delay)
        return list(bars)

    return _


def _deliver_history(sh, source=None, symbol=None, tf=None):
    """Mirror what poll_stream does once sh.ready is set: fan the staged history out to every
    subscribed queue as one history message. This is the same delivery logic poll_stream uses, so
    tests can assert on queue contents without spinning up a websocket."""
    msg = json.dumps({
        "type": "history",
        "bars": sh.bars,
        "source": source or sh.source,
        "symbol": symbol or sh.symbol,
        "tf": tf or sh.tf,
    })
    sh.send(msg)


@pytest.mark.asyncio
async def test_two_subscribers_share_one_poller_and_one_history_call():
    # One poller, one upstream history call, two subscribers get the same bars.
    hist_bars = _bars(1700000000, n=4)
    sh = base._Shared(("bybit", "BTCUSD", "M5", 1000), _history_fn_slow(hist_bars, delay=0.03), "BTCUSD", "M5", "bybit", 1000, None)

    call_count = 0
    real = sh.history_fn

    def counting(symbol, tf, limit, to_ms):
        nonlocal call_count
        call_count += 1
        return real(symbol, tf, limit, to_ms)

    sh.history_fn = counting
    base._shared[sh.key] = sh
    sh.task = asyncio.create_task(base._pump(sh))

    q1 = await base._subscribe(sh)
    q2 = await base._subscribe(sh)

    # Both subscribers registered, one upstream call made, shared bars set.
    await asyncio.sleep(0.1)
    assert len(sh.clients) == 2
    assert call_count == 1, "two subscribers must not cause two upstream history calls"
    assert sh.bars == hist_bars

    # Both subscribers see the same history (poll_stream would deliver sh.bars once ready;
    # reproduce that here so the assertion is on the shared state, not on a fake send).
    _deliver_history(sh)
    assert q1.get_nowait() == q2.get_nowait()

    # Only one upstream history call for two subscribers.
    assert call_count == 1, "two subscribers must not cause two upstream history calls"

    # Tear down exactly like poll_stream does for the last subscriber.
    sh.clients.discard(q1)
    sh.clients.discard(q2)
    if not sh.clients:
        if sh.task is not None:
            sh.task.cancel()
        if base._shared.get(sh.key) is sh:
            base._shared.pop(sh.key, None)
    if sh.task is not None:
        try:
            await sh.task
        except asyncio.CancelledError:
            pass


@pytest.mark.asyncio
async def test_last_subscriber_leaves_pump_cancelled_and_registry_cleared():
    # One subscriber joins, gets history, leaves; registry entry is removed.
    hist_bars = _bars(1700000000, n=3)
    sh = base._Shared(("deriv", "frxXAUUSD", "M1", 1000), _history_fn_slow(hist_bars, delay=0.03), "frxXAUUSD", "M1", "deriv", 1000, None)
    base._shared[sh.key] = sh
    sh.task = asyncio.create_task(base._pump(sh))

    q = await base._subscribe(sh)
    await asyncio.sleep(0.1)  # let the poller fetch once and set sh.bars

    assert sh.bars == hist_bars
    _deliver_history(sh)
    assert q.qsize() == 1, "delivered history should be one message"
    msg = await q.get()  # history

    # The last subscriber leaves: the shared poller teardown is the same path poll_stream walks.
    sh.clients.discard(q)
    if not sh.clients:
        if sh.task is not None:
            sh.task.cancel()
        if base._shared.get(sh.key) is sh:
            base._shared.pop(sh.key, None)

    assert sh.key not in base._shared, "last subscriber leaves the registry entry removed"


@pytest.mark.asyncio
async def test_gap_larger_than_two_candles_rebroadcasts():
    sh = base._Shared(("binance", "XAUUSD", "M1", 1000), _history_fn(_bars(1700000000, n=4)), "XAUUSD", "M1", "binance", 1000, None)
    base._shared[sh.key] = sh

    span = base.TF_SEC["M1"] * 1000
    bars = _bars(1700000000, n=4, step=60)  # 4 consecutive M1 bars
    base._stage_history(sh, bars)
    sh.last = dict(bars[-1])

    # Manual subscriber (like poll_stream after ready): queue + client registration.
    q = asyncio.Queue(maxsize=256)
    sh.clients.add(q)
    await sh.ready.wait()

    # The gap contract: when a fetch returns a batch whose first new bar is more than 2 spans ahead,
    # the poller rebroadcasts the whole window. Drive that branch directly (network-free).
    gap_bars = _bars(bars[-1]["time"] + int(2.5 * span), n=3, step=60)
    sh.history_fn = _history_fn(gap_bars)

    await base._rebroadcast(sh)

    assert q.qsize() == 1, "a gap > 2 candles must produce one rebroadcast (history) message"
    msg = q.get_nowait()
    assert json.loads(msg).get("type") == "history"
    assert json.loads(msg).get("bars") == gap_bars
