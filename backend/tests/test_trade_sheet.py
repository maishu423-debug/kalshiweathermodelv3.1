"""Trade-history sheet: settlement fills profit/loss into the right open rows, and reads map columns."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from kalshi_api import sheets_history as sh  # noqa: E402


class FakeWorksheet:
    def __init__(self, rows):
        self.rows = rows
        self.batches = []

    def get_all_values(self):
        return [list(r) for r in self.rows]

    def batch_update(self, data):
        self.batches.append(data)
        for item in data:
            row_no = int(item["range"].split(":")[0][1:])
            start = 8   # column I
            row = self.rows[row_no - 1]
            row.extend([""] * (14 - len(row)))
            for offset, value in enumerate(item["values"][0]):
                row[start + offset] = str(value)

    def append_row(self, row):
        self.rows.append([str(v) for v in row])


def _buy(ticker, side, contracts, price, status="open"):
    return ["2026-10-19T16:30:00+00:00", ticker, "88-89", "buy", side, str(price), str(contracts),
            str(-contracts * price), "", status, "", "0.5", "0.4", "900000"]


@pytest.fixture
def ws(monkeypatch):
    fake = FakeWorksheet([sh.TRADES_HEADER, _buy("T-1", "yes", 100, 40), _buy("T-1", "no", 100, 56),
                          _buy("T-2", "yes", 100, 30), _buy("T-1", "yes", 50, 50, status="won")])
    monkeypatch.setattr(sh, "_get_trades_worksheet", lambda: fake)
    monkeypatch.setattr(sh, "_trades_rows_cache", None)
    return fake


def test_winning_row_gets_payout_and_profit(ws):
    assert sh.settle_trades("T-1", "yes", won=True) == 1
    row = ws.rows[1]
    assert row[8] == str(100 * 100 - 100 * 40)   # profit = payout - cost = 6000
    assert row[9] == "won" and row[10] == "10000"


def test_losing_row_gets_loss_equal_to_cost(ws):
    assert sh.settle_trades("T-1", "no", won=False) == 1
    row = ws.rows[2]
    assert row[8] == str(-100 * 56)
    assert row[9] == "lost" and row[10] == "0"


def test_other_markets_and_already_settled_rows_are_untouched(ws):
    sh.settle_trades("T-1", "yes", won=False)
    assert ws.rows[3][9] == "open"     # T-2 untouched
    assert ws.rows[4][9] == "won"      # already-settled T-1 yes row untouched
    assert sh.settle_trades("T-1", "yes", won=False) == 0   # idempotent


def test_get_recent_trades_maps_new_columns_and_handles_legacy_rows(ws):
    ws.rows.append(["2026-10-18T16:30:00+00:00", "T-0", "86-87", "buy", "no", "60", "100", "-6000", ""])  # 9-col legacy
    sh.settle_trades("T-2", "yes", won=True)
    trades = sh.get_recent_trades(limit=10)
    assert trades[0]["market_ticker"] == "T-0" and trades[0]["status"] == "open"    # newest first
    assert trades[0]["model_prob"] is None and trades[0]["balance_after_cents"] is None
    t2 = next(t for t in trades if t["market_ticker"] == "T-2")
    assert t2["status"] == "won" and t2["realized_pl_cents"] == 7000 and t2["payout_cents"] == 10000
    assert t2["model_prob"] == pytest.approx(0.5) and t2["balance_after_cents"] == 900000
