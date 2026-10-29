"""
Background profit-target watcher for live positions.

Maintains its own Kalshi WebSocket subscription (independent of the SSE
browser bridge).  On every ticker update it checks whether any live position
has reached PROFIT_TARGET_CENTS and, if so, immediately places a sell order.

Start order:
  apps.py → start_profit_watcher()   (called once at Django startup)
"""

import asyncio
import json
import logging
import threading
import time

logger = logging.getLogger(__name__)

PROFIT_TARGET_CENTS  = 7    # must match algorithm.py
POSITION_REFRESH_SEC = 30   # re-query Kalshi positions every 30 s

_lock          = threading.Lock()
_positions     = {}   # ticker → {side, avg_price_cents, contracts}
_pending_sells = set()  # tickers mid-sell (duplicate-sell guard)
_started       = False
_enabled       = False  # only active when live mode is on


def set_enabled(enabled: bool):
    global _enabled
    with _lock:
        _enabled = enabled
    logger.info(f"[profit-watcher] {'ENABLED' if enabled else 'disabled'}")


# ── Position cache ────────────────────────────────────────────────────────────

def _refresh_positions():
    """Pull the latest live positions from Kalshi and update the watched set."""
    with _lock:
        if not _enabled:
            return
    try:
        from .live import get_state
        state = get_state()
        with _lock:
            _positions.clear()
            for p in state["positions"]:
                _positions[p["market_ticker"]] = {
                    "side":            p["side"],
                    "avg_price_cents": p["avg_price_cents"],
                    "contracts":       p["contracts"],
                }
        logger.debug(f"[profit-watcher] positions refreshed: {list(_positions.keys()) or 'none'}")
    except Exception as exc:
        logger.warning(f"[profit-watcher] position refresh failed: {exc}")


# ── Profit-target check ───────────────────────────────────────────────────────

def _check_and_sell(ticker: str, yes_bid_cents: int):
    """
    Called on every WebSocket ticker update.
    Sells immediately if profit >= PROFIT_TARGET_CENTS.
    Thread-safe; duplicate sells are prevented by _pending_sells.
    """
    with _lock:
        if not _enabled:
            return
        pos = _positions.get(ticker)
        if not pos or ticker in _pending_sells:
            return

        side      = pos["side"]
        avg       = pos["avg_price_cents"]
        contracts = pos["contracts"]

        # Express current market bid in the position's side
        bid = yes_bid_cents if side == "yes" else (100 - yes_bid_cents)

        if avg is None or bid is None or (bid - avg) < PROFIT_TARGET_CENTS:
            return

        profit = bid - avg
        _pending_sells.add(ticker)

    # ── Outside the lock: place the sell ─────────────────────────────────
    try:
        from .live import place_live_sell
        logger.info(
            f"[profit-watcher] PROFIT TARGET HIT — {ticker} "
            f"{side.upper()} +{profit}¢  (avg {avg}¢ → bid {bid}¢) "
            f"selling {contracts} contracts"
        )
        place_live_sell(ticker, side, bid, contracts, market_label=ticker)

        with _lock:
            _positions.pop(ticker, None)

        logger.info(f"[profit-watcher] sell submitted for {ticker}")

    except Exception as exc:
        logger.error(f"[profit-watcher] sell FAILED for {ticker}: {exc}")

    finally:
        with _lock:
            _pending_sells.discard(ticker)


# ── WebSocket watch loop ──────────────────────────────────────────────────────

async def _watch_loop():
    from .auth import KalshiAuthError, create_websocket_headers
    from .client import fetch_json, get_today_event_ticker
    import websockets

    KALSHI_WS_URL = "wss://external-api-ws.kalshi.com/trade-api/ws/v2"

    while True:
        try:
            headers = create_websocket_headers()
        except KalshiAuthError:
            logger.info("[profit-watcher] No Kalshi credentials — sleeping 5 min.")
            await asyncio.sleep(300)
            continue

        try:
            event_payload  = fetch_json(f"/events/{get_today_event_ticker()}")
            market_tickers = [m["ticker"] for m in event_payload.get("markets", [])]
        except Exception as exc:
            logger.warning(f"[profit-watcher] failed to fetch market tickers: {exc}")
            await asyncio.sleep(15)
            continue

        if not market_tickers:
            logger.info("[profit-watcher] No open markets today — sleeping 60 s.")
            await asyncio.sleep(60)
            continue

        logger.info(f"[profit-watcher] Subscribing to {len(market_tickers)} markets")

        try:
            async with websockets.connect(
                KALSHI_WS_URL,
                additional_headers=headers,
                open_timeout=20,
                ping_interval=20,
                ping_timeout=20,
            ) as ws:
                await ws.send(json.dumps({
                    "id":     99,
                    "cmd":    "subscribe",
                    "params": {
                        "channels":       ["ticker"],
                        "market_tickers": market_tickers,
                    },
                }))

                last_refresh = 0.0

                async for raw in ws:
                    now = time.time()

                    # Refresh position cache on schedule
                    if now - last_refresh >= POSITION_REFRESH_SEC:
                        # Run blocking I/O in thread pool so it doesn't block the event loop
                        await asyncio.get_event_loop().run_in_executor(
                            None, _refresh_positions
                        )
                        last_refresh = now

                    msg_data = json.loads(raw)
                    if msg_data.get("type") != "ticker":
                        continue

                    msg          = msg_data.get("msg", {})
                    ticker       = msg.get("market_ticker")
                    yes_bid_raw  = msg.get("yes_bid_dollars")

                    if ticker and yes_bid_raw is not None:
                        try:
                            yes_bid = round(float(yes_bid_raw) * 100)
                        except (TypeError, ValueError):
                            continue
                        # Run sell check in thread pool (it does blocking HTTP)
                        await asyncio.get_event_loop().run_in_executor(
                            None, _check_and_sell, ticker, yes_bid
                        )

        except Exception as exc:
            logger.warning(f"[profit-watcher] WebSocket disconnected: {exc} — reconnecting in 5 s")
            await asyncio.sleep(5)


# ── Thread entry point ────────────────────────────────────────────────────────

def _run():
    import sys
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    # Initial position load before the loop starts
    _refresh_positions()
    asyncio.run(_watch_loop())


def start_profit_watcher():
    global _started
    with _lock:
        if _started:
            return
        _started = True

    t = threading.Thread(target=_run, daemon=True, name="profit-watcher")
    t.start()
    logger.info("[profit-watcher] started")
