"""Append-only forecast history log, stored in a Google Sheet instead of the
local SQL database.

Render's free tier wipes the local disk (including SQLite) on every restart
or redeploy, so a history log written there doesn't survive. A Google Sheet,
written via a service account, does.

Configure via env vars:
  GOOGLE_SERVICE_ACCOUNT_JSON  - full service account JSON as a string
  GOOGLE_SERVICE_ACCOUNT_PATH  - path to a service account JSON file (local dev)
  GOOGLE_SHEETS_ID             - spreadsheet ID for forecast history
  GOOGLE_TRADES_SHEET_ID       - spreadsheet ID for trade history
                                  (optional — falls back to GOOGLE_SHEETS_ID)

If none of these are configured, both public functions fail gracefully
(print a warning, return "[]" / no-op) rather than raising.
"""
import json
import os
import threading
import time
from datetime import datetime, timezone

HEADER = ["timestamp", "current_temp_f", "model_forecast_f"]
TRADES_WORKSHEET_TITLE = "Trades"
# One row per order. A buy row is written as "open" and updated in place when its market settles:
# realized_pl_cents (profit / loss), status (won / lost) and payout_cents are filled in then.
# The first nine columns are the original layout, so rows written before the extra columns existed stay valid.
TRADES_HEADER = [
    "timestamp", "market_ticker", "market_label", "action", "side",
    "price_cents", "contracts", "cash_delta_cents", "realized_pl_cents",
    "status", "payout_cents", "model_prob", "kalshi_prob", "balance_after_cents",
]
_COL_PL, _COL_STATUS, _COL_PAYOUT = 9, 10, 11   # 1-based sheet columns updated at settlement
_READ_CACHE_SEC = 10

_client_lock = threading.Lock()
_worksheet = None
_init_failed = False

_trades_client_lock = threading.Lock()
_trades_worksheet = None
_trades_init_failed = False
_trades_rows_cache = None   # (monotonic_time, rows) — keeps the 5s UI poll from exhausting the Sheets read quota


def _open_sheet(sheet_id):
    import gspread
    from google.oauth2.service_account import Credentials

    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]

    raw_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    json_path = os.environ.get("GOOGLE_SERVICE_ACCOUNT_PATH")

    if not sheet_id:
        raise RuntimeError("No spreadsheet ID configured")

    if raw_json:
        info = json.loads(raw_json)
        creds = Credentials.from_service_account_info(info, scopes=scopes)
    elif json_path:
        creds = Credentials.from_service_account_file(json_path, scopes=scopes)
    else:
        raise RuntimeError(
            "Neither GOOGLE_SERVICE_ACCOUNT_JSON nor GOOGLE_SERVICE_ACCOUNT_PATH is set"
        )

    client = gspread.authorize(creds)
    return client.open_by_key(sheet_id)


def _build_worksheet():
    sheet_id = os.environ.get("GOOGLE_SHEETS_ID")
    if not sheet_id:
        raise RuntimeError("GOOGLE_SHEETS_ID is not set")

    worksheet = _open_sheet(sheet_id).sheet1

    if not worksheet.get_all_values():
        worksheet.append_row(HEADER)

    return worksheet


def _get_worksheet():
    """Lazily build and cache the worksheet handle. Thread-safe."""
    global _worksheet, _init_failed

    if _worksheet is not None:
        return _worksheet
    if _init_failed:
        return None

    with _client_lock:
        if _worksheet is not None:
            return _worksheet
        if _init_failed:
            return None
        try:
            _worksheet = _build_worksheet()
        except Exception as exc:
            _init_failed = True
            print(f"[sheets_history] Google Sheets not configured, skipping ({exc})")
            return None

    return _worksheet


def _build_trades_worksheet():
    import gspread

    sheet_id = os.environ.get("GOOGLE_TRADES_SHEET_ID") or os.environ.get("GOOGLE_SHEETS_ID")
    if not sheet_id:
        raise RuntimeError("Neither GOOGLE_TRADES_SHEET_ID nor GOOGLE_SHEETS_ID is set")

    sheet = _open_sheet(sheet_id)
    try:
        worksheet = sheet.worksheet(TRADES_WORKSHEET_TITLE)
    except gspread.exceptions.WorksheetNotFound:
        worksheet = sheet.add_worksheet(title=TRADES_WORKSHEET_TITLE, rows=1000, cols=len(TRADES_HEADER))

    existing = worksheet.get_all_values()
    if not existing:
        worksheet.append_row(TRADES_HEADER)
    elif existing[0] != TRADES_HEADER:
        # Older sheet with the 9-column layout: extend the header row, leave the data rows untouched.
        if worksheet.col_count < len(TRADES_HEADER):
            worksheet.resize(cols=len(TRADES_HEADER))
        worksheet.update(values=[TRADES_HEADER], range_name="A1")

    return worksheet


def _get_trades_worksheet():
    """Lazily build and cache the Trades worksheet handle. Thread-safe."""
    global _trades_worksheet, _trades_init_failed

    if _trades_worksheet is not None:
        return _trades_worksheet
    if _trades_init_failed:
        return None

    with _trades_client_lock:
        if _trades_worksheet is not None:
            return _trades_worksheet
        if _trades_init_failed:
            return None
        try:
            _trades_worksheet = _build_trades_worksheet()
        except Exception as exc:
            _trades_init_failed = True
            print(f"[sheets_history] Google Sheets Trades tab not configured, skipping ({exc})")
            return None

    return _trades_worksheet


def append_snapshot(current_temp_f, model_forecast_f):
    """Append one forecast-history row. No-ops if Sheets isn't configured."""
    worksheet = _get_worksheet()
    if worksheet is None:
        return

    try:
        timestamp = datetime.now(timezone.utc).isoformat()
        worksheet.append_row([timestamp, current_temp_f, model_forecast_f])
    except Exception as exc:
        print(f"[sheets_history] failed to append snapshot ({exc})")


def get_recent_snapshots(limit=100):
    """Return up to `limit` most recent snapshots, newest first, as dicts
    shaped like {"timestamp", "current_temp_f", "model_forecast_f"}.
    Returns [] if Sheets isn't configured or the read fails.
    """
    worksheet = _get_worksheet()
    if worksheet is None:
        return []

    try:
        rows = worksheet.get_all_values()
    except Exception as exc:
        print(f"[sheets_history] failed to read snapshots ({exc})")
        return []

    if len(rows) <= 1:
        return []

    data_rows = rows[1:]  # drop header
    recent = data_rows[-limit:]
    recent.reverse()  # newest first

    def _to_float(value):
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    return [
        {
            "timestamp": row[0] if len(row) > 0 else None,
            "current_temp_f": _to_float(row[1]) if len(row) > 1 else None,
            "model_forecast_f": _to_float(row[2]) if len(row) > 2 else None,
        }
        for row in recent
    ]


def _invalidate_trades_cache():
    global _trades_rows_cache
    _trades_rows_cache = None


def append_trade(
    market_ticker, market_label, action, side,
    price_cents, contracts, cash_delta_cents, realized_pl_cents,
    status="open", payout_cents=None, model_prob=None, kalshi_prob=None, balance_after_cents=None,
):
    """Append one trade-history row. No-ops if Sheets isn't configured."""
    worksheet = _get_trades_worksheet()
    if worksheet is None:
        return

    def blank(value):
        return "" if value is None else value

    try:
        timestamp = datetime.now(timezone.utc).isoformat()
        worksheet.append_row([
            timestamp, market_ticker, market_label, action, side,
            price_cents, contracts, cash_delta_cents, blank(realized_pl_cents),
            status, blank(payout_cents), blank(model_prob), blank(kalshi_prob), blank(balance_after_cents),
        ])
        _invalidate_trades_cache()
    except Exception as exc:
        print(f"[sheets_history] failed to append trade ({exc})")


def settle_trades(market_ticker, side, won):
    """Fill in profit/loss on every still-open buy row of (market_ticker, side) once Kalshi has resolved the
    market: a winning contract pays 100¢, a losing one 0¢. Returns the number of rows updated."""
    worksheet = _get_trades_worksheet()
    if worksheet is None:
        return 0

    try:
        rows = worksheet.get_all_values()
        updates = []
        for index, row in enumerate(rows[1:], start=2):   # sheet rows are 1-based; row 1 is the header
            if len(row) < 7 or row[1] != market_ticker or row[4] != side or row[3] != "buy":
                continue
            if len(row) > _COL_STATUS - 1 and row[_COL_STATUS - 1] not in ("", "open"):
                continue   # already settled
            contracts = float(row[6])
            cost_cents = -int(float(row[7]))             # cash_delta_cents of a buy is negative
            payout = int(round(contracts * 100)) if won else 0
            updates.append({
                "range": f"I{index}:K{index}",
                "values": [[payout - cost_cents, "won" if won else "lost", payout]],
            })
        if updates:
            worksheet.batch_update(updates)
            _invalidate_trades_cache()
        return len(updates)
    except Exception as exc:
        print(f"[sheets_history] failed to settle trades for {market_ticker} {side} ({exc})")
        return 0


def get_recent_trades(limit=100):
    """Return up to `limit` most recent trades, newest first, as dicts shaped
    like {"timestamp", "market_ticker", "market_label", "action", "side",
    "price_cents", "contracts", "cash_delta_cents", "realized_pl_cents",
    "status", "payout_cents", "model_prob", "kalshi_prob", "balance_after_cents"}.
    Returns [] if Sheets isn't configured or the read fails.
    """
    global _trades_rows_cache

    worksheet = _get_trades_worksheet()
    if worksheet is None:
        return []

    cached = _trades_rows_cache
    if cached is not None and time.monotonic() - cached[0] < _READ_CACHE_SEC:
        rows = cached[1]
    else:
        try:
            rows = worksheet.get_all_values()
        except Exception as exc:
            print(f"[sheets_history] failed to read trades ({exc})")
            return []
        _trades_rows_cache = (time.monotonic(), rows)

    if len(rows) <= 1:
        return []

    data_rows = rows[1:]  # drop header
    recent = data_rows[-limit:]
    recent.reverse()  # newest first

    def _to_float(value):
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _to_int(value):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return None

    def _cell(row, i):
        return row[i] if len(row) > i and row[i] != "" else None

    return [
        {
            "timestamp":           _cell(row, 0),
            "market_ticker":       _cell(row, 1),
            "market_label":        _cell(row, 2),
            "action":              _cell(row, 3),
            "side":                _cell(row, 4),
            "price_cents":         _to_int(_cell(row, 5)),
            "contracts":           _to_float(_cell(row, 6)),
            "cash_delta_cents":    _to_int(_cell(row, 7)),
            "realized_pl_cents":   _to_int(_cell(row, 8)),
            # rows written before the status column existed: a buy with no result yet is open
            "status":              _cell(row, 9) or ("open" if _cell(row, 3) == "buy" and _cell(row, 8) is None else "closed"),
            "payout_cents":        _to_int(_cell(row, 10)),
            "model_prob":          _to_float(_cell(row, 11)),
            "kalshi_prob":         _to_float(_cell(row, 12)),
            "balance_after_cents": _to_int(_cell(row, 13)),
        }
        for row in recent
    ]
