from decimal import Decimal

from django.db import models


class PaperAccount(models.Model):
    name = models.CharField(max_length=64, unique=True, default="default")
    cash_cents = models.IntegerField(default=1_000_000)
    auto_enabled = models.BooleanField(default=True)
    # Unused: automatic real-money trading was removed (alerts are sent instead). Kept so the applied migration stays valid.
    live_enabled = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)


class PaperPosition(models.Model):
    account = models.ForeignKey(PaperAccount, on_delete=models.CASCADE)
    market_ticker = models.CharField(max_length=128)
    market_label = models.CharField(max_length=128, blank=True)
    side = models.CharField(max_length=3, choices=[("yes", "Yes"), ("no", "No")])
    contracts = models.DecimalField(max_digits=14, decimal_places=4, default=Decimal("0"))
    avg_price_cents = models.DecimalField(max_digits=8, decimal_places=4, default=Decimal("0"))
    updated_at = models.DateTimeField(auto_now=True)
    # Set once Kalshi resolves the market: payout is $1/contract if our side won, else 0.
    settled_at = models.DateTimeField(null=True, blank=True)
    payout_cents = models.IntegerField(null=True, blank=True)
    realized_pl_cents = models.IntegerField(null=True, blank=True)

    class Meta:
        unique_together = ("account", "market_ticker", "side")


class PaperTrade(models.Model):
    account = models.ForeignKey(PaperAccount, on_delete=models.CASCADE)
    market_ticker = models.CharField(max_length=128)
    market_label = models.CharField(max_length=128, blank=True)
    action = models.CharField(max_length=4, choices=[("buy", "Buy"), ("sell", "Sell")])
    side = models.CharField(max_length=3, choices=[("yes", "Yes"), ("no", "No")])
    price_cents = models.IntegerField()
    contracts = models.DecimalField(max_digits=14, decimal_places=4)
    cash_delta_cents = models.IntegerField()
    realized_pl_cents = models.IntegerField(null=True, blank=True)  # set on sells only
    created_at = models.DateTimeField(auto_now_add=True)


class AutoTradeRun(models.Model):
    """One scheduled auto-trade cycle. `slot` is unique so a restart can't repeat a cycle."""
    slot = models.CharField(max_length=32, unique=True)  # e.g. "2026-10-17T12:30" (ET)
    ran_at = models.DateTimeField(auto_now_add=True)
    status = models.CharField(max_length=8)  # traded | skipped | error
    message = models.TextField(blank=True)
    forecast_mean = models.FloatField(null=True, blank=True)
    trades_count = models.IntegerField(default=0)


class LiveOrder(models.Model):
    """Unused: automatic real-money trading was removed. Kept so the applied migration stays valid."""
    client_order_id = models.CharField(max_length=96, unique=True)
    slot = models.CharField(max_length=32)
    et_date = models.CharField(max_length=10, db_index=True)   # Eastern trading day, for the daily limit
    market_ticker = models.CharField(max_length=128)
    market_label = models.CharField(max_length=128, blank=True)
    side = models.CharField(max_length=3, choices=[("yes", "Yes"), ("no", "No")])
    limit_price_cents = models.IntegerField()
    contracts_requested = models.IntegerField()
    # pending (about to send) | filled | partial | unfilled | error (rejected, not counted toward the
    # daily limit) | unknown (send failed ambiguously, counted) | mismatch (position moved the wrong way)
    status = models.CharField(max_length=10, default="pending")
    order_id = models.CharField(max_length=64, blank=True)
    fill_count = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("0"))
    avg_price_cents = models.FloatField(null=True, blank=True)   # our side's average fill price
    fee_cents = models.FloatField(null=True, blank=True)         # total fee for the fill
    message = models.TextField(blank=True)
    model_prob = models.FloatField(null=True, blank=True)
    kalshi_prob = models.FloatField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    settled_at = models.DateTimeField(null=True, blank=True)
    payout_cents = models.IntegerField(null=True, blank=True)
    realized_pl_cents = models.IntegerField(null=True, blank=True)
