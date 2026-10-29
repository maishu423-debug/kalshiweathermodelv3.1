"""Kalshi bucket semantics, as returned by the API for KXHIGHMIA:
  between 87..88  -> "87° to 88°"   (both ends inclusive)
  greater 88      -> "89° or above" (t > floor)
  less    81      -> "80° or below" (t < cap)
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from kalshi_api import algorithm as alg  # noqa: E402

PDF = {80: 0.05, 81: 0.10, 82: 0.15, 83: 0.20, 87: 0.20, 88: 0.20, 89: 0.10}


def test_between_includes_both_ends():
    assert alg._bracket_prob(87, 88, PDF) == pytest.approx(0.40)


def test_greater_excludes_floor():
    assert alg._bracket_prob(88, None, PDF) == pytest.approx(0.10)   # 89+ only


def test_less_excludes_cap():
    assert alg._bracket_prob(None, 81, PDF) == pytest.approx(0.05)   # 80 and below only


def test_buckets_partition_the_pdf():
    # <=80, 81-82, 83-84, 85-86, 87-88, >=89 (greater floor 88 starts at 89)
    buckets = [(None, 81), (81, 82), (83, 84), (85, 86), (87, 88), (88, None)]
    total = sum(alg._bracket_prob(f, c, PDF) for f, c in buckets)
    assert total == pytest.approx(1.0)


def test_is_dead_uses_top_of_bucket(monkeypatch):
    monkeypatch.setattr(alg, "get_temp_snapshot", lambda: {"current_f": 88.0, "daily_high_f": 88.0})
    monkeypatch.setattr(alg, "get_all_velocities", lambda tickers: {})
    markets = [
        {"ticker": "lo",  "floor_strike": None, "cap_strike": 81,  "yes_ask_cents": 10, "no_ask_cents": 91},
        {"ticker": "mid", "floor_strike": 87,   "cap_strike": 88,  "yes_ask_cents": 50, "no_ask_cents": 51},
        {"ticker": "hi",  "floor_strike": 88,   "cap_strike": None, "yes_ask_cents": 30, "no_ask_cents": 71},
    ]
    dead = {a["ticker"]: a["is_dead"] for a in alg.analyze_markets(markets)}
    assert dead == {"lo": True, "mid": False, "hi": False}   # known high 88 is still inside 87-88
