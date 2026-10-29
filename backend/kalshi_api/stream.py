"""
Kalshi WebSocket → Server-Sent Events bridge.

The WebSocket runs in a dedicated background thread (own asyncio event loop).
Messages are pushed into a thread-safe queue that the SSE generator drains.
This avoids the async/sync impedance mismatch that caused the original stream
to disconnect immediately.
"""
import asyncio
import json
import queue
import threading
import time

import websockets

from .auth import KalshiAuthError, create_websocket_headers
from .client import fetch_json, get_today_event_ticker


KALSHI_WS_URL = "wss://external-api-ws.kalshi.com/trade-api/ws/v2"


# ── Helpers ───────────────────────────────────────────────────────────────────

def sse_message(event, data):
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def _dollars_to_cents(value):
    if value in (None, ""):
        return None
    return round(float(value) * 100)


def _fp_to_float(value):
    if value in (None, ""):
        return None
    return float(value)


def _normalize_ticker(msg):
    return {
        "ticker":           msg.get("market_ticker"),
        "yes_bid_cents":    _dollars_to_cents(msg.get("yes_bid_dollars")),
        "yes_ask_cents":    _dollars_to_cents(msg.get("yes_ask_dollars")),
        "last_price_cents": _dollars_to_cents(
            msg.get("price_dollars") or msg.get("last_price_dollars")
        ),
        "volume":           _fp_to_float(msg.get("volume_fp")),
        "open_interest":    _fp_to_float(msg.get("open_interest_fp")),
        "yes_bid_size":     _fp_to_float(msg.get("yes_bid_size_fp")),
        "yes_ask_size":     _fp_to_float(msg.get("yes_ask_size_fp")),
        "ts_ms":            msg.get("ts_ms"),
    }


# ── WebSocket background thread ───────────────────────────────────────────────

async def _ws_to_queue(msg_queue: queue.Queue, stop_event: threading.Event, market_tickers: list, headers: dict):
    async with websockets.connect(
        KALSHI_WS_URL,
        additional_headers=headers,
        open_timeout=20,
        ping_interval=20,
        ping_timeout=20,
    ) as ws:
        await ws.send(json.dumps({
            "id": 1,
            "cmd": "subscribe",
            "params": {
                "channels": ["ticker", "orderbook_delta", "trade"],
                "market_tickers": market_tickers,
            },
        }))

        async for raw in ws:
            if stop_event.is_set():
                break
            msg_queue.put(("msg", json.loads(raw)))

    msg_queue.put(("done", None))


def _run_ws(msg_queue: queue.Queue, stop_event: threading.Event):
    """Entry point for the background WebSocket thread — auto-restarts on disconnect."""
    import sys
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    while not stop_event.is_set():
        try:
            headers        = create_websocket_headers()
            event_payload  = fetch_json(f"/events/{get_today_event_ticker()}")
            market_tickers = [m["ticker"] for m in event_payload.get("markets", [])]
            asyncio.run(_ws_to_queue(msg_queue, stop_event, market_tickers, headers))
        except KalshiAuthError as exc:
            msg_queue.put(("auth_error", str(exc)))
            return
        except Exception as exc:
            msg_queue.put(("reconnecting", str(exc)))
        if not stop_event.is_set():
            time.sleep(3)  # brief pause before reconnect


# ── SSE generator ─────────────────────────────────────────────────────────────

def stream_events():
    """
    Django StreamingHttpResponse generator.
    Starts the WebSocket thread, then yields SSE frames as they arrive.
    """
    msg_queue  = queue.Queue(maxsize=500)
    stop_event = threading.Event()

    yield sse_message("status", {
        "connected": False,
        "message":   "Connecting to Kalshi WebSocket…",
        "ts":        int(time.time()),
    })

    thread = threading.Thread(
        target=_run_ws, args=(msg_queue, stop_event), daemon=True, name="kalshi-ws"
    )
    thread.start()

    try:
        while True:
            try:
                kind, payload = msg_queue.get(timeout=25)
            except queue.Empty:
                # Keepalive — prevents browser / proxy from closing the connection
                yield ": ka\n\n"
                continue

            if kind == "auth_error":
                yield sse_message("auth_missing", {
                    "message":  payload,
                    "fallback": "Kalshi API credentials not configured — using REST polling.",
                })
                break

            if kind == "error":
                yield sse_message("stream_error", {"message": payload})
                break

            if kind == "reconnecting":
                yield sse_message("status", {"connected": False, "message": "Reconnecting…"})
                continue

            if kind == "done":
                # WebSocket closed cleanly — thread will reconnect automatically
                continue

            # kind == "msg"
            message      = payload
            message_type = message.get("type")
            msg          = message.get("msg", {})

            if message_type == "ticker":
                data = _normalize_ticker(msg)
                # Also feed real-time prices into the velocity tracker
                try:
                    from .price_tracker import record_price
                    if data["ticker"]:
                        record_price(data["ticker"], data["yes_bid_cents"], data["yes_ask_cents"])
                except Exception:
                    pass
                yield sse_message("ticker", data)

            elif message_type == "orderbook_delta":
                yield sse_message("orderbook_delta", {
                    "ticker":      msg.get("market_ticker"),
                    "side":        msg.get("side"),
                    "price_cents": _dollars_to_cents(msg.get("price_dollars")),
                    "delta":       _fp_to_float(msg.get("delta_fp")),
                    "seq":         message.get("seq"),  # seq is in outer message, not msg
                })

            elif message_type == "orderbook_snapshot":
                def _parse_snapshot_side(levels):
                    return [
                        {"price_cents": _dollars_to_cents(p), "size": _fp_to_float(s)}
                        for p, s in (levels or [])
                    ]
                yield sse_message("orderbook_snapshot", {
                    "ticker": msg.get("market_ticker"),
                    "seq":    message.get("seq"),
                    "yes":    _parse_snapshot_side(msg.get("yes_dollars_fp")),
                    "no":     _parse_snapshot_side(msg.get("no_dollars_fp")),
                })

            elif message_type == "trade":
                yield sse_message("trade", msg)

            elif message_type == "error":
                yield sse_message("kalshi_error", msg)

            elif message_type in {"subscribed", "ok"}:
                yield sse_message("status", {
                    "connected": True,
                    "message":   "Live WebSocket connected.",
                })

    finally:
        stop_event.set()
