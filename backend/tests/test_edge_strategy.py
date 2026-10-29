"""Trade rule: YES on the top bucket only if model > Kalshi; NO on other live buckets only if model < Kalshi."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from kalshi_api import algorithm as alg  # noqa: E402


def _bucket(ticker, floor, cap, yes_ask, no_ask, dead=False):
    return {
        "ticker": ticker, "label": ticker, "floor": floor, "cap": cap,
        "yes_ask_cents": yes_ask, "no_ask_cents": no_ask, "is_dead": dead, "model_prob": None,
    }


def _run(monkeypatch, buckets, pdf):
    """pdf: {temp: prob}; the model results are a single point-mass-per-temp 3h model."""
    temps = list(pdf)
    model = {"temps": temps, "probs": list(pdf.values()), "high_temps": temps, "high_probs": list(pdf.values())}
    monkeypatch.setattr(alg, "get_temp_snapshot", lambda: {"current_f": None, "daily_high_f": None})
    return alg.get_recommendation(buckets, results={"accuweather_3h": model})


# Model: 50% on 88, 30% on 90, 20% on 92 -> buckets [87,89) 50%, [89,91) 30%, [91,93) 20%
PDF = {88: 0.5, 90: 0.3, 92: 0.2}


def test_yes_when_model_beats_kalshi_and_no_when_kalshi_beats_model(monkeypatch):
    buckets = [
        _bucket("A", 87, 89, yes_ask=40, no_ask=61),   # top: model 50% > Kalshi 40%  -> YES
        _bucket("B", 89, 91, yes_ask=45, no_ask=56),   # model 30% < Kalshi (1-.56=44%) -> NO
        _bucket("C", 91, 93, yes_ask=10, no_ask=91),   # model 20% > Kalshi 9%        -> skip
    ]
    rec = _run(monkeypatch, buckets, PDF)
    assert rec["status"] == "ready"
    assert rec["yes_trade"]["ticker"] == "A" and rec["yes_trade"]["price_cents"] == 40
    assert rec["yes_trade"]["model_prob"] == pytest.approx(0.5)
    assert rec["yes_trade"]["kalshi_prob"] == pytest.approx(0.40)
    assert [t["ticker"] for t in rec["no_trades"]] == ["B"]
    assert rec["no_trades"][0]["price_cents"] == 56
    assert rec["no_trades"][0]["kalshi_prob"] == pytest.approx(0.44)


def test_no_yes_when_kalshi_is_at_or_above_model(monkeypatch):
    buckets = [
        _bucket("A", 87, 89, yes_ask=50, no_ask=51),   # model 50% == Kalshi 50%  -> no YES
        _bucket("B", 89, 91, yes_ask=20, no_ask=81),   # model 30% > Kalshi 19%   -> skip
    ]
    rec = _run(monkeypatch, buckets, PDF)
    assert rec["status"] == "waiting"
    assert rec["yes_trade"] is None and rec["no_trades"] == []


def test_no_trades_still_placed_without_a_yes_edge(monkeypatch):
    buckets = [
        _bucket("A", 87, 89, yes_ask=70, no_ask=31),   # top, model 50% < Kalshi 70% -> no YES
        _bucket("B", 89, 91, yes_ask=45, no_ask=56),   # model 30% < Kalshi 44%      -> NO
    ]
    rec = _run(monkeypatch, buckets, PDF)
    assert rec["status"] == "ready"
    assert rec["yes_trade"] is None
    assert [t["ticker"] for t in rec["no_trades"]] == ["B"]


def test_dead_buckets_and_the_top_bucket_never_get_a_no(monkeypatch):
    buckets = [
        _bucket("A", 87, 89, yes_ask=70, no_ask=31),
        _bucket("D", 85, 87, yes_ask=2, no_ask=99, dead=True),
        _bucket("B", 89, 91, yes_ask=45, no_ask=56),
    ]
    rec = _run(monkeypatch, buckets, PDF)
    assert "D" not in [t["ticker"] for t in rec["no_trades"]]
    assert "A" not in [t["ticker"] for t in rec["no_trades"]]   # top bucket: YES-or-nothing


def test_prices_without_upside_are_skipped(monkeypatch):
    buckets = [
        _bucket("A", 87, 89, yes_ask=99, no_ask=2),    # YES ask 99 > cap -> no YES even though 50% < 99% anyway
        _bucket("B", 89, 91, yes_ask=1, no_ask=99),    # NO ask 99 > cap  -> skip
    ]
    rec = _run(monkeypatch, buckets, {88: 0.995, 90: 0.005})
    assert rec["yes_trade"] is None
    assert rec["no_trades"] == []
