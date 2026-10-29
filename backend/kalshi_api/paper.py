import json
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP

from django.db import transaction
from django.utils import timezone

from .models import AutoTradeRun, PaperAccount, PaperPosition, PaperTrade


STARTING_CASH_CENTS = 1_000_000  # $10,000
ACCOUNT_NAME = "default"


class PaperTradeError(Exception):
    pass


def get_account():
    return PaperAccount.objects.get_or_create(
        name=ACCOUNT_NAME,
        defaults={"cash_cents": STARTING_CASH_CENTS},
    )[0]


def decimal_to_float(value):
    return float(value.quantize(Decimal("0.0001")))


def money_to_cents(value):
    return int(Decimal(value).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def serialize_position(position):
    return {
        "market_ticker": position.market_ticker,
        "market_label": position.market_label,
        "side": position.side,
        "contracts": decimal_to_float(position.contracts),
        "avg_price_cents": float(position.avg_price_cents),
        "cost_basis_cents": money_to_cents(position.contracts * position.avg_price_cents),
        "settled": position.settled_at is not None,
        "payout_cents": position.payout_cents,
        "realized_pl_cents": position.realized_pl_cents,
    }


def serialize_trade(trade):
    return {
        "id": trade.id,
        "market_ticker": trade.market_ticker,
        "market_label": trade.market_label,
        "action": trade.action,
        "side": trade.side,
        "price_cents": trade.price_cents,
        "contracts": decimal_to_float(trade.contracts),
        "cash_delta_cents": trade.cash_delta_cents,
        "realized_pl_cents": trade.realized_pl_cents,
        "created_at": trade.created_at.isoformat(),
    }


def serialize_run(run):
    return {
        "slot": run.slot,
        "ran_at": run.ran_at.isoformat(),
        "status": run.status,
        "message": run.message,
        "forecast_mean": run.forecast_mean,
        "trades_count": run.trades_count,
    }


def get_state():
    account = get_account()
    positions = list(
        PaperPosition.objects.filter(account=account, contracts__gt=0).order_by(
            "settled_at", "market_ticker", "side"
        )
    )
    trades = PaperTrade.objects.filter(account=account).order_by("-created_at")[:200]
    buys = list(PaperTrade.objects.filter(account=account, action="buy"))
    runs = AutoTradeRun.objects.order_by("-ran_at")[:30]

    open_positions = [p for p in positions if p.settled_at is None]
    settled = [p for p in positions if p.settled_at is not None]
    realized = [p.realized_pl_cents or 0 for p in settled]

    invested_cents = sum(money_to_cents(p.contracts * p.avg_price_cents) for p in open_positions)
    spent_cents = sum(-t.cash_delta_cents for t in buys)
    yes_buys = [t for t in buys if t.side == "yes"]
    no_buys = [t for t in buys if t.side == "no"]

    return {
        "account": {
            "cash_cents": account.cash_cents,
            "starting_cash_cents": STARTING_CASH_CENTS,
            "auto_enabled": account.auto_enabled,
            "invested_cents": invested_cents,
            "spent_cents": spent_cents,
            "realized_pl_cents": sum(realized),
            "total_profit_cents": sum(v for v in realized if v > 0),
            "total_loss_cents": sum(v for v in realized if v < 0),
            "wins": sum(1 for v in realized if v > 0),
            "losses": sum(1 for v in realized if v < 0),
            "yes_trades": len(yes_buys),
            "no_trades": len(no_buys),
            "yes_contracts": decimal_to_float(sum((t.contracts for t in yes_buys), Decimal("0"))),
            "no_contracts": decimal_to_float(sum((t.contracts for t in no_buys), Decimal("0"))),
        },
        "positions": [serialize_position(p) for p in positions],
        "trades": [serialize_trade(t) for t in trades],
        "runs": [serialize_run(r) for r in runs],
    }


def parse_order(raw_body):
    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise PaperTradeError("Invalid JSON body.") from exc

    action = payload.get("action")
    side = payload.get("side")
    market_ticker = payload.get("market_ticker")
    market_label = payload.get("market_label", "")
    price_cents = payload.get("price_cents")
    dollars_cents = payload.get("dollars_cents")
    contracts = payload.get("contracts")

    if action not in {"buy", "sell"}:
        raise PaperTradeError("Action must be buy or sell.")
    if side not in {"yes", "no"}:
        raise PaperTradeError("Side must be yes or no.")
    if not market_ticker:
        raise PaperTradeError("Missing market ticker.")

    try:
        price_cents = int(price_cents)
        if contracts is not None:
            contracts = int(contracts)
        else:
            dollars_cents = int(dollars_cents)
    except (TypeError, ValueError) as exc:
        raise PaperTradeError("Price and trade size must be numbers.") from exc

    if price_cents <= 0 or price_cents >= 100:
        raise PaperTradeError("Price must be between 1 and 99 cents.")
    if contracts is not None:
        if contracts <= 0:
            raise PaperTradeError("Contracts must be greater than 0.")
    elif dollars_cents <= 0:
        raise PaperTradeError("Enter a trade amount greater than $0.")

    def optional_prob(key):
        try:
            return round(float(payload[key]), 4) if payload.get(key) is not None else None
        except (TypeError, ValueError):
            return None

    return {
        "action": action,
        "side": side,
        "market_ticker": market_ticker,
        "market_label": market_label,
        "price_cents": price_cents,
        "dollars_cents": dollars_cents,
        "contracts": contracts,
        # Recorded in the trade-history sheet only: why the bot took this trade.
        "model_prob": optional_prob("model_prob"),
        "kalshi_prob": optional_prob("kalshi_prob"),
    }


@transaction.atomic
def place_order(order):
    account = PaperAccount.objects.select_for_update().get(pk=get_account().pk)
    price = Decimal(order["price_cents"])
    if order.get("contracts") is not None:
        contracts = Decimal(order["contracts"])
    else:
        contracts = (Decimal(order["dollars_cents"]) / price).quantize(
            Decimal("0.0001"),
            rounding=ROUND_DOWN,
        )

    if contracts <= 0:
        raise PaperTradeError("Trade amount is too small for this price.")

    cash_amount = money_to_cents(contracts * price)
    position, _created = PaperPosition.objects.select_for_update().get_or_create(
        account=account,
        market_ticker=order["market_ticker"],
        side=order["side"],
        defaults={
            "market_label": order["market_label"],
            "contracts": Decimal("0"),
            "avg_price_cents": Decimal("0"),
        },
    )
    position.market_label = order["market_label"]

    realized_pl = None

    if order["action"] == "buy":
        if position.settled_at is not None:
            raise PaperTradeError("Market already settled.")
        if cash_amount > account.cash_cents:
            raise PaperTradeError("Insufficient paper cash.")

        old_value = position.contracts * position.avg_price_cents
        new_value = contracts * price
        new_contracts = position.contracts + contracts
        position.avg_price_cents = (old_value + new_value) / new_contracts
        position.contracts = new_contracts
        account.cash_cents -= cash_amount
        cash_delta = -cash_amount
    else:
        if position.contracts <= 0 or position.settled_at is not None:
            raise PaperTradeError("No position to sell.")
        if contracts > position.contracts:
            contracts = position.contracts
            cash_amount = money_to_cents(contracts * price)

        realized_pl = money_to_cents((price - position.avg_price_cents) * contracts)
        position.contracts -= contracts
        if position.contracts <= 0:
            position.avg_price_cents = Decimal("0")
        account.cash_cents += cash_amount
        cash_delta = cash_amount

    account.save(update_fields=["cash_cents", "updated_at"])
    position.save(update_fields=["market_label", "contracts", "avg_price_cents", "updated_at"])

    trade = PaperTrade.objects.create(
        account=account,
        market_ticker=order["market_ticker"],
        market_label=order["market_label"],
        action=order["action"],
        side=order["side"],
        price_cents=order["price_cents"],
        contracts=contracts,
        cash_delta_cents=cash_delta,
        realized_pl_cents=realized_pl if order["action"] == "sell" else None,
    )

    _record_trade_history(trade, order, account.cash_cents)

    return {"trade": serialize_trade(trade), "state": get_state()}


def _record_trade_history(trade, order, balance_after_cents):
    """Persist a trade-history row to Google Sheets. Never breaks a trade."""
    try:
        from .sheets_history import append_trade

        append_trade(
            market_ticker=trade.market_ticker,
            market_label=trade.market_label,
            action=trade.action,
            side=trade.side,
            price_cents=trade.price_cents,
            contracts=decimal_to_float(trade.contracts),
            cash_delta_cents=trade.cash_delta_cents,
            realized_pl_cents=trade.realized_pl_cents,
            status="open" if trade.action == "buy" else "closed",
            model_prob=order.get("model_prob"),
            kalshi_prob=order.get("kalshi_prob"),
            balance_after_cents=balance_after_cents,
        )
    except Exception as exc:
        print(f"[paper] failed to record trade history ({exc})")


def _record_settlements(settled):
    """Write each settled position's profit/loss into its trade-history rows. Never breaks settlement."""
    try:
        from .sheets_history import settle_trades

        for ticker, side, won in settled:
            settle_trades(ticker, side, won)
    except Exception as exc:
        print(f"[paper] failed to record settlements in trade history ({exc})")


def settle_positions():
    """
    Settle every open position whose Kalshi market has resolved: $1 per contract
    if our side won, $0 otherwise. Returns the number of positions settled.
    """
    from .client import KalshiClientError, fetch_json

    account = get_account()
    open_positions = list(
        PaperPosition.objects.filter(account=account, contracts__gt=0, settled_at__isnull=True)
    )

    results = {}
    for ticker in {p.market_ticker for p in open_positions}:
        try:
            market = fetch_json(f"/markets/{ticker}").get("market", {})
        except KalshiClientError as exc:
            print(f"[paper] settlement lookup failed for {ticker} ({exc})")
            continue
        if market.get("status") in ("determined", "finalized", "settled") and market.get("result") in ("yes", "no"):
            results[ticker] = market["result"]

    settled = 0
    settled_for_sheet = []
    for position in open_positions:
        result = results.get(position.market_ticker)
        if result is None:
            continue
        with transaction.atomic():
            locked_account = PaperAccount.objects.select_for_update().get(pk=account.pk)
            locked = PaperPosition.objects.select_for_update().get(pk=position.pk)
            if locked.settled_at is not None:
                continue
            payout = money_to_cents(locked.contracts * 100) if locked.side == result else 0
            cost = money_to_cents(locked.contracts * locked.avg_price_cents)
            locked.settled_at = timezone.now()
            locked.payout_cents = payout
            locked.realized_pl_cents = payout - cost
            locked.save(update_fields=["settled_at", "payout_cents", "realized_pl_cents", "updated_at"])
            locked_account.cash_cents += payout
            locked_account.save(update_fields=["cash_cents", "updated_at"])
            settled += 1
            settled_for_sheet.append((locked.market_ticker, locked.side, locked.side == result))
    _record_settlements(settled_for_sheet)
    return settled


def set_auto_enabled(enabled):
    account = get_account()
    account.auto_enabled = bool(enabled)
    account.save(update_fields=["auto_enabled", "updated_at"])
    return get_state()


@transaction.atomic
def reset_account():
    account = get_account()
    PaperPosition.objects.filter(account=account).delete()
    PaperTrade.objects.filter(account=account).delete()
    AutoTradeRun.objects.all().delete()
    account.cash_cents = STARTING_CASH_CENTS
    account.save(update_fields=["cash_cents", "updated_at"])
    return get_state()
