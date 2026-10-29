import json
import math
import os
import time
import urllib.error
from datetime import date
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode
from urllib.request import Request, urlopen


# Point KALSHI_API_BASE_URL at https://external-api.demo.kalshi.co/trade-api/v2 (with demo keys) to
# rehearse live trading with fake money.
KALSHI_BASE_URL = os.environ.get(
    "KALSHI_API_BASE_URL", "https://external-api.kalshi.com/trade-api/v2"
).strip().rstrip("/")
MIAMI_SERIES_TICKER = "KXHIGHMIA"


def _auth_headers(method: str, path: str) -> dict:
    """Return signed auth headers, or empty dict if credentials not configured."""
    try:
        from .auth import get_api_key_id, get_private_key, sign_pss_text
        api_key_id = get_api_key_id()
        if not api_key_id:
            return {}
        private_key = get_private_key()
        timestamp = str(int(time.time() * 1000))
        signature = sign_pss_text(private_key, timestamp + method + path)
        return {
            "KALSHI-ACCESS-KEY":       api_key_id,
            "KALSHI-ACCESS-SIGNATURE": signature,
            "KALSHI-ACCESS-TIMESTAMP": timestamp,
        }
    except Exception:
        return {}


def _auth_headers_full(method: str, api_path: str) -> dict:
    """Return signed auth headers using the full /trade-api/v2 path (required for portfolio endpoints)."""
    try:
        from .auth import get_api_key_id, get_private_key, sign_pss_text
        api_key_id = get_api_key_id()
        if not api_key_id:
            return {}
        private_key = get_private_key()
        timestamp = str(int(time.time() * 1000))
        full_path = f"/trade-api/v2{api_path}"
        signature = sign_pss_text(private_key, timestamp + method + full_path)
        return {
            "KALSHI-ACCESS-KEY":       api_key_id,
            "KALSHI-ACCESS-SIGNATURE": signature,
            "KALSHI-ACCESS-TIMESTAMP": timestamp,
        }
    except Exception:
        return {}


def get_today_event_ticker() -> str:
    """Build the event ticker for today's Miami temperature market.
    Kalshi format: KXHIGHMIA-YYMMMDD  e.g. KXHIGHMIA-26JUN21
    """
    return f"KXHIGHMIA-{date.today().strftime('%y%b%d').upper()}"


def get_default_market_ticker() -> str:
    return f"{get_today_event_ticker()}-B94.5"


class KalshiClientError(Exception):
    pass


def fetch_json(path, params=None):
    query = f"?{urlencode(params)}" if params else ""
    auth = _auth_headers("GET", path)
    url = f"{KALSHI_BASE_URL}{path}{query}"
    headers = {"Accept": "application/json", "User-Agent": "kalshi-clone-mvp/0.1", **auth}
    request = Request(url, headers=headers)
    try:
        with urlopen(request, timeout=15) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        raise KalshiClientError(f"Kalshi request failed for {path}") from exc


def dollars_to_cents(value):
    if value in (None, ""):
        return None

    try:
        return int((Decimal(str(value)) * 100).quantize(Decimal("1")))
    except (InvalidOperation, ValueError):
        return None


def fixed_point_to_float(value):
    if value in (None, ""):
        return None

    try:
        return float(Decimal(str(value)))
    except (InvalidOperation, ValueError):
        return None


def normalize_series(series):
    important_info = series.get("product_metadata", {}).get("important_info", {})

    return {
        "ticker": series.get("ticker"),
        "title": series.get("title"),
        "category": series.get("category"),
        "frequency": series.get("frequency"),
        "tags": series.get("tags", []),
        "settlement_sources": series.get("settlement_sources", []),
        "important_info": {
            "title": important_info.get("title"),
            "message": important_info.get("message"),
            "markdown": important_info.get("markdown"),
        },
        "contract_terms_url": series.get("contract_terms_url"),
        "contract_url": series.get("contract_url"),
    }


def normalize_event(event):
    return {
        "event_ticker": event.get("event_ticker"),
        "series_ticker": event.get("series_ticker"),
        "title": event.get("title"),
        "subtitle": event.get("sub_title"),
        "category": event.get("category"),
        "mutually_exclusive": event.get("mutually_exclusive"),
        "strike_date": event.get("strike_date"),
    }


def _bracket_label(floor_strike, cap_strike):
    """
    Build a bracket label matching Kalshi's UI convention.

    Kalshi stores strikes at 0.5°F increments (e.g. 90.5, 91.5) but its UI
    displays them as whole degrees using floor():
      floor(90.5) = 90, floor(91.5) = 91  →  "90° to 91°"

    The API's subtitle field uses ceil() instead, which is why it disagrees.
    For the unbounded-above case, the UI shows ceil(floor_strike) — e.g.
    floor_strike=96.5  →  math.floor(96.5)+1 = 97  →  "97° or above".
    """
    if floor_strike is None and cap_strike is not None:
        return f"{math.floor(float(cap_strike))}° or below"
    if floor_strike is not None and cap_strike is None:
        return f"{math.floor(float(floor_strike)) + 1}° or above"
    if floor_strike is not None and cap_strike is not None:
        lo = math.floor(float(floor_strike))
        hi = math.floor(float(cap_strike))
        return f"{lo}° to {hi}°"
    return None


def normalize_market(market):
    yes_bid = dollars_to_cents(market.get("yes_bid_dollars"))
    yes_ask = dollars_to_cents(market.get("yes_ask_dollars"))
    last_price = dollars_to_cents(market.get("last_price_dollars"))

    chance = last_price
    if chance is None and yes_bid is not None and yes_ask is not None:
        chance = round((yes_bid + yes_ask) / 2)

    floor_strike = market.get("floor_strike")
    cap_strike   = market.get("cap_strike")
    computed_label = _bracket_label(floor_strike, cap_strike)

    return {
        "ticker": market.get("ticker"),
        "event_ticker": market.get("event_ticker"),
        "label": computed_label or market.get("subtitle") or market.get("yes_sub_title"),
        "title": market.get("title"),
        "status": market.get("status"),
        "strike_type": market.get("strike_type"),
        "floor_strike": market.get("floor_strike"),
        "cap_strike": market.get("cap_strike"),
        "yes_bid_cents": yes_bid,
        "yes_ask_cents": yes_ask,
        "yes_bid_size": fixed_point_to_float(market.get("yes_bid_size_fp")),
        "yes_ask_size": fixed_point_to_float(market.get("yes_ask_size_fp")),
        "no_bid_cents": dollars_to_cents(market.get("no_bid_dollars")),
        "no_ask_cents": dollars_to_cents(market.get("no_ask_dollars")),
        "last_price_cents": last_price,
        "chance_percent": chance,
        "volume": fixed_point_to_float(market.get("volume_fp")),
        "volume_24h": fixed_point_to_float(market.get("volume_24h_fp")),
        "open_interest": fixed_point_to_float(market.get("open_interest_fp")),
        "open_time": market.get("open_time"),
        "close_time": market.get("close_time"),
        "expected_expiration_time": market.get("expected_expiration_time"),
        "rules_primary": market.get("rules_primary"),
        "rules_secondary": market.get("rules_secondary"),
    }


def normalize_orderbook(payload):
    orderbook = payload.get("orderbook_fp", {})

    def normalize_side(levels):
        return [
            {
                "price_cents": dollars_to_cents(price),
                "price_dollars": price,
                "count": fixed_point_to_float(count),
            }
            for price, count in levels or []
        ]

    return {
        "yes": normalize_side(orderbook.get("yes_dollars")),
        "no": normalize_side(orderbook.get("no_dollars")),
    }


def sort_markets(markets):
    def sort_key(market):
        floor = market.get("floor_strike")
        cap = market.get("cap_strike")
        if floor is None and cap is not None:
            return cap - 1000
        if floor is not None:
            return floor
        return 9999

    return sorted(markets, key=sort_key)


def fetch_json_auth(path, params=None):
    """Fetch from a Kalshi endpoint that requires valid auth (e.g. /portfolio/*)."""
    auth = _auth_headers_full("GET", path)
    if not auth:
        raise KalshiClientError("Kalshi API credentials not configured.")
    query = f"?{urlencode(params)}" if params else ""
    url = f"{KALSHI_BASE_URL}{path}{query}"
    headers = {"Accept": "application/json", "User-Agent": "kalshi-clone-mvp/0.1", **auth}
    request = Request(url, headers=headers)
    try:
        with urlopen(request, timeout=15) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read().decode("utf-8")
        except Exception:
            body = str(exc)
        raise KalshiClientError(f"Kalshi API {exc.code}: {body}") from exc
    except Exception as exc:
        raise KalshiClientError(f"Kalshi request failed for {path}") from exc


def post_json(path, body: dict):
    """Sign and POST JSON to Kalshi API."""
    auth = _auth_headers_full("POST", path)
    if not auth:
        raise KalshiClientError("Kalshi API credentials not configured.")
    data = json.dumps(body).encode("utf-8")
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "kalshi-clone-mvp/0.1",
        **auth,
    }
    url = f"{KALSHI_BASE_URL}{path}"
    request = Request(url, data=data, headers=headers, method="POST")
    try:
        with urlopen(request, timeout=15) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            body_text = exc.read().decode("utf-8")
        except Exception:
            body_text = str(exc)
        raise KalshiClientError(f"Kalshi API {exc.code}: {body_text}") from exc
    except Exception as exc:
        raise KalshiClientError(f"Kalshi POST failed for {path}") from exc


def get_portfolio_balance():
    """Return raw Kalshi balance response."""
    return fetch_json_auth("/portfolio/balance")


def get_portfolio_positions(cursor=None):
    """Return raw Kalshi positions response."""
    params = {"cursor": cursor} if cursor else None
    return fetch_json_auth("/portfolio/positions", params)


def get_miami_temperature_payload(selected_market_ticker=None, include_orderbook=True):
    event_ticker = get_today_event_ticker()
    selected_market_ticker = selected_market_ticker or get_default_market_ticker()

    series_payload = fetch_json(f"/series/{MIAMI_SERIES_TICKER}")
    event_payload = fetch_json(f"/events/{event_ticker}")

    markets = sort_markets(
        [normalize_market(market) for market in event_payload.get("markets", [])]
    )
    selected_market = next(
        (market for market in markets if market["ticker"] == selected_market_ticker),
        markets[0] if markets else None,
    )
    selected_market_ticker = (
        selected_market.get("ticker")
        if selected_market and selected_market.get("ticker")
        else selected_market_ticker
    )

    orderbook = {"yes": [], "no": []}
    if include_orderbook:
        orderbook_payload = fetch_json(
            f"/markets/{selected_market_ticker}/orderbook",
            {"depth": 5},
        )
        orderbook = normalize_orderbook(orderbook_payload)

    return {
        "series": normalize_series(series_payload["series"]),
        "event": normalize_event(event_payload["event"]),
        "markets": markets,
        "selected_market": selected_market,
        "orderbook": orderbook,
        "source": {
            "base_url": KALSHI_BASE_URL,
            "event_ticker": event_ticker,
            "selected_market_ticker": selected_market_ticker,
        },
    }
