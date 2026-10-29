from decimal import Decimal

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name="PaperAccount",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(default="default", max_length=64, unique=True)),
                ("cash_cents", models.IntegerField(default=100000)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
        ),
        migrations.CreateModel(
            name="PaperTrade",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("market_ticker", models.CharField(max_length=128)),
                ("market_label", models.CharField(blank=True, max_length=128)),
                ("action", models.CharField(choices=[("buy", "Buy"), ("sell", "Sell")], max_length=4)),
                ("side", models.CharField(choices=[("yes", "Yes"), ("no", "No")], max_length=3)),
                ("price_cents", models.IntegerField()),
                ("contracts", models.DecimalField(decimal_places=4, max_digits=14)),
                ("cash_delta_cents", models.IntegerField()),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("account", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to="kalshi_api.paperaccount")),
            ],
        ),
        migrations.CreateModel(
            name="PaperPosition",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("market_ticker", models.CharField(max_length=128)),
                ("market_label", models.CharField(blank=True, max_length=128)),
                ("side", models.CharField(choices=[("yes", "Yes"), ("no", "No")], max_length=3)),
                ("contracts", models.DecimalField(decimal_places=4, default=Decimal("0"), max_digits=14)),
                ("avg_price_cents", models.DecimalField(decimal_places=4, default=Decimal("0"), max_digits=8)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("account", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to="kalshi_api.paperaccount")),
            ],
            options={
                "unique_together": {("account", "market_ticker", "side")},
            },
        ),
    ]
