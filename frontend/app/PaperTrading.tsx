"use client";

import { useCallback, useEffect, useState } from "react";


// ── Types ────────────────────────────────────────────────────────────────────

type Leg = {
  ticker: string;
  label: string;
  side: "yes" | "no";
  price_cents: number;
  model_prob: number;
  kalshi_prob?: number;   // Kalshi's implied probability that the bucket settles YES
};

// One row of the Google Sheet "Trades" tab (served by /api/kalshi/algorithm/trade-history).
type HistoryRow = {
  timestamp: string | null;
  market_ticker: string | null;
  market_label: string | null;
  action: "buy" | "sell" | null;
  side: "yes" | "no" | null;
  price_cents: number | null;
  contracts: number | null;
  cash_delta_cents: number | null;
  realized_pl_cents: number | null;
  status: "open" | "won" | "lost" | "closed" | null;
  payout_cents: number | null;
  model_prob: number | null;
  kalshi_prob: number | null;
  balance_after_cents: number | null;
};

export type Recommendation = {
  status: "ready" | "waiting";
  reason: string;
  mean?: number | null;
  yes_trade?: Leg | null;
  no_trades?: Leg[];
};

export type BookPrices = {
  ticker: string;
  yes_bid_cents: number | null;
  yes_ask_cents: number | null;
  no_bid_cents: number | null;
};

type Position = {
  market_ticker: string;
  market_label: string;
  side: "yes" | "no";
  contracts: number;
  avg_price_cents: number;
  cost_basis_cents: number;
  settled: boolean;
  payout_cents: number | null;
  realized_pl_cents: number | null;
};

type Trade = {
  id: number;
  market_ticker: string;
  market_label: string;
  side: "yes" | "no";
  price_cents: number;
  contracts: number;
  cash_delta_cents: number;
  created_at: string;
};

type Run = {
  slot: string;
  ran_at: string;
  status: "traded" | "skipped" | "error" | "running";
  message: string;
  forecast_mean: number | null;
  trades_count: number;
};

type PaperState = {
  account: {
    cash_cents: number;
    starting_cash_cents: number;
    auto_enabled: boolean;
    invested_cents: number;
    spent_cents: number;
    realized_pl_cents: number;
    total_profit_cents: number;
    total_loss_cents: number;
    wins: number;
    losses: number;
    yes_trades: number;
    no_trades: number;
    yes_contracts: number;
    no_contracts: number;
  };
  positions: Position[];
  trades: Trade[];
  runs: Run[];
  bot: {
    enabled: boolean;
    window_open: boolean;
    next_run_at: string;
    contracts_per_trade: number;
    interval_minutes: number;
    window: string;
    alerts?: { telegram_configured: boolean; email_configured: boolean };
  };
};


// ── Formatters ───────────────────────────────────────────────────────────────

const ET = "America/New_York";

function usd(cents: number) {
  return (cents / 100).toLocaleString("en-US", { style: "currency", currency: "USD" });
}

function signedUsd(cents: number) {
  if (Math.round(cents) === 0) return usd(0);
  return `${cents > 0 ? "+" : "−"}${usd(Math.abs(cents))}`;
}

function plClass(cents: number) {
  return Math.round(cents) > 0 ? "pt-pos" : Math.round(cents) < 0 ? "pt-neg" : "";
}

function etTime(iso: string, withDate = false) {
  return new Date(iso).toLocaleString("en-US", {
    timeZone: ET,
    hour: "numeric",
    minute: "2-digit",
    ...(withDate ? { month: "short", day: "numeric" } : {}),
  });
}

// Slot keys are already Eastern wall-clock time: "2026-10-17T12:30".
function slotTime(slot: string) {
  const [date, time] = slot.split("T");
  const [h, m] = time.split(":").map(Number);
  const [, mo, d] = date.split("-");
  return `${Number(mo)}/${Number(d)} ${((h + 11) % 12) + 1}:${String(m).padStart(2, "0")} ${h < 12 ? "AM" : "PM"}`;
}

function sideMark(side: "yes" | "no", book?: BookPrices): number | null {
  if (!book) return null;
  if (side === "yes") return book.yes_bid_cents;
  return book.no_bid_cents ?? (book.yes_ask_cents != null ? 100 - book.yes_ask_cents : null);
}


// ── Component ────────────────────────────────────────────────────────────────

export default function PaperTrading({
  books,
  recommendation,
}: {
  books: BookPrices[];
  recommendation?: Recommendation | null;
}) {
  const [state, setState] = useState<PaperState | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [tab, setTab] = useState<"positions" | "trades" | "log">("positions");
  const [history, setHistory] = useState<HistoryRow[] | null>(null);

  const load = useCallback(async () => {
    try {
      const res = await fetch("/api/kalshi/autotrader/state", { cache: "no-store" });
      if (!res.ok) throw new Error(`Paper trading API ${res.status}`);
      setState((await res.json()) as PaperState);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Paper trading unavailable.");
    }
  }, []);

  const loadHistory = useCallback(async () => {
    try {
      const res = await fetch("/api/kalshi/algorithm/trade-history?limit=200", { cache: "no-store" });
      if (!res.ok) return;
      const data = (await res.json()) as { history?: HistoryRow[] };
      setHistory(data.history ?? []);
    } catch {
      // silent — the panel just keeps the last rows until the next successful poll
    }
  }, []);

  useEffect(() => {
    load();
    const timer = window.setInterval(load, 5_000);
    return () => window.clearInterval(timer);
  }, [load]);

  useEffect(() => {
    loadHistory();
    const timer = window.setInterval(loadHistory, 15_000);
    return () => window.clearInterval(timer);
  }, [loadHistory]);

  // Re-read the sheet right away when a trade is placed or a position settles.
  const activity = state ? `${state.trades.length}:${state.positions.filter((p) => p.settled).length}` : "";
  useEffect(() => {
    if (activity) loadHistory();
  }, [activity, loadHistory]);

  async function post(path: string, body?: object) {
    setBusy(true);
    try {
      const res = await fetch(`/api/kalshi/autotrader/${path}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body ?? {}),
      });
      if (!res.ok) throw new Error(`Request failed (${res.status})`);
      setState((await res.json()) as PaperState);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Request failed.");
    } finally {
      setBusy(false);
    }
  }

  async function testAlert() {
    setBusy(true);
    try {
      const res = await fetch("/api/kalshi/autotrader/notify-test", { method: "POST" });
      const data = await res.json().catch(() => ({}));
      window.alert(res.ok ? `Test alert sent (${data.detail}).` : `Alert failed: ${data.detail ?? res.status}`);
    } finally {
      setBusy(false);
    }
  }

  function resetAccount() {
    if (window.confirm("Reset the paper account? This clears all positions, trades and bot history and restores $10,000.")) {
      post("reset");
    }
  }

  if (!state) {
    return (
      <div className="pt-layout">
        <section className="algo-panel pt-wrap">
          <h3 className="algo-section-title">Paper Trading</h3>
          <p className={error ? "algo-error" : "algo-hint"}>{error ?? "Loading paper account…"}</p>
        </section>
      </div>
    );
  }

  const { account, bot } = state;
  const alertsOn = Boolean(bot.alerts?.telegram_configured || bot.alerts?.email_configured);
  const bookByTicker = new Map(books.map((b) => [b.ticker, b]));

  // Mark open positions to the current bid (what we could sell for). No quote → valued at cost.
  const rows = state.positions.map((p) => {
    const mark = p.settled ? null : sideMark(p.side, bookByTicker.get(p.market_ticker));
    const value = p.settled ? (p.payout_cents ?? 0) : mark != null ? Math.round((p.contracts * mark)) : p.cost_basis_cents;
    const pl = p.settled ? (p.realized_pl_cents ?? 0) : value - p.cost_basis_cents;
    return { ...p, mark, value, pl };
  });
  const open = rows.filter((r) => !r.settled);
  const unrealized = open.reduce((sum, r) => sum + r.pl, 0);
  const openValue = open.reduce((sum, r) => sum + r.value, 0);
  const equity = account.cash_cents + openValue;
  const totalPl = equity - account.starting_cash_cents;
  const totalContracts = account.yes_contracts + account.no_contracts;

  const status = !bot.enabled ? "paused" : bot.window_open ? "running" : "idle";
  const statusText = { paused: "Paused", running: "Trading window open", idle: "Outside trading window" }[status];

  // One trade per cycle, same rule as the bot: YES if there is a YES edge, else the NO with the biggest edge.
  const bestNo = [...(recommendation?.no_trades ?? [])].sort(
    (a, b) => (b.kalshi_prob ?? 0) - b.model_prob - ((a.kalshi_prob ?? 0) - a.model_prob)
  )[0];
  const nextLeg = recommendation?.status === "ready" ? recommendation.yes_trade ?? bestNo : undefined;
  const plan: Leg[] = nextLeg ? [nextLeg] : [];

  const historyRows = history ?? [];
  const historyNet = historyRows.reduce((sum, h) => sum + (h.realized_pl_cents ?? 0), 0);

  return (
    <div className="pt-layout">
    <section className="algo-panel pt-wrap">
      <div className="pt-head">
        <h3 className="algo-section-title">
          Paper Trading
          <span className="algo-hint-inline">
            {" "}— {bot.contracts_per_trade} contracts per trade · every {bot.interval_minutes} min · {bot.window}
          </span>
        </h3>
        <div className="pt-controls">
          <span className={`pt-status pt-status--${status}`}>{statusText}</span>
          <span className="pt-next">
            {bot.enabled ? `Next run ${etTime(bot.next_run_at, true)} ET` : "Auto-trading off"}
          </span>
          <button
            className={`algo-btn ${bot.enabled ? "pt-btn-stop" : "algo-btn--trade"}`}
            disabled={busy}
            onClick={() => post("toggle", { enabled: !bot.enabled })}
          >
            {bot.enabled ? "Pause bot" : "Start bot"}
          </button>
          <button
            className="algo-btn algo-btn--refresh"
            disabled={busy}
            onClick={testAlert}
            title={alertsOn ? "Send a test alert to Telegram / email" : "No alert channel is configured on the server"}
          >
            {alertsOn ? "Test alert" : "Alerts off"}
          </button>
          <button className="algo-btn algo-btn--refresh" disabled={busy} onClick={resetAccount}>
            Reset
          </button>
        </div>
      </div>

      {error && <p className="algo-error">{error}</p>}

      <div className="pt-hero">
        <div className="pt-hero-tile">
          <span className="pt-card-label">Available balance</span>
          <strong>{usd(account.cash_cents)}</strong>
          <span className="pt-card-sub">
            cash free to trade · started with {usd(account.starting_cash_cents)}
          </span>
        </div>
        <div className="pt-hero-tile pt-hero-tile--profit">
          <span className="pt-card-label">Total profit</span>
          <strong className="pt-pos">{signedUsd(account.total_profit_cents)}</strong>
          <span className="pt-card-sub">{account.wins} winning position{account.wins === 1 ? "" : "s"} settled</span>
        </div>
        <div className="pt-hero-tile pt-hero-tile--loss">
          <span className="pt-card-label">Total loss</span>
          <strong className="pt-neg">{signedUsd(account.total_loss_cents)}</strong>
          <span className="pt-card-sub">{account.losses} losing position{account.losses === 1 ? "" : "s"} settled</span>
        </div>
        <div className="pt-hero-tile">
          <span className="pt-card-label">Net realized P/L</span>
          <strong className={plClass(account.realized_pl_cents)}>{signedUsd(account.realized_pl_cents)}</strong>
          <span className="pt-card-sub">profit + loss, settled trades only</span>
        </div>
      </div>

      <div className="pt-cards">
        <div className="pt-card">
          <span className="pt-card-label">Portfolio value</span>
          <strong>{usd(equity)}</strong>
          <span className="pt-card-sub">cash + open positions at bid</span>
        </div>
        <div className="pt-card">
          <span className="pt-card-label">Total P/L</span>
          <strong className={plClass(totalPl)}>{signedUsd(totalPl)}</strong>
          <span className="pt-card-sub">
            {((totalPl / account.starting_cash_cents) * 100).toFixed(2)}% of start
          </span>
        </div>
        <div className="pt-card">
          <span className="pt-card-label">Currently traded</span>
          <strong>{usd(account.invested_cents)}</strong>
          <span className="pt-card-sub">{open.length} open position{open.length === 1 ? "" : "s"}</span>
        </div>
        <div className="pt-card">
          <span className="pt-card-label">Unrealized P/L</span>
          <strong className={plClass(unrealized)}>{signedUsd(unrealized)}</strong>
          <span className="pt-card-sub">open positions, until settled</span>
        </div>
        <div className="pt-card">
          <span className="pt-card-label">Trades placed</span>
          <strong>{account.yes_trades + account.no_trades}</strong>
          <span className="pt-card-sub">
            <span className="pt-yes">{account.yes_trades} YES</span> ·{" "}
            <span className="pt-no">{account.no_trades} NO</span> · {totalContracts.toLocaleString()} contracts
          </span>
        </div>
        <div className="pt-card">
          <span className="pt-card-label">Total spent / Win-loss</span>
          <strong>{usd(account.spent_cents)}</strong>
          <span className="pt-card-sub">
            <span className="pt-pos">{account.wins}W</span> · <span className="pt-neg">{account.losses}L</span> settled
          </span>
        </div>
      </div>

      <div className="pt-plan">
        <span className="pt-plan-label">Next cycle would buy</span>
        {plan.length === 0 ? (
          <span className="algo-hint-inline">{recommendation?.reason ?? "Waiting for model forecast…"}</span>
        ) : (
          plan.map((l) => (
            <span key={`${l.ticker}-${l.side}`} className={`pt-chip pt-chip--${l.side}`}>
              {l.side.toUpperCase()} {l.label} @ {l.price_cents}¢ · model {(l.model_prob * 100).toFixed(1)}%
              {l.kalshi_prob != null ? ` vs Kalshi ${(l.kalshi_prob * 100).toFixed(1)}%` : ""}
            </span>
          ))
        )}
      </div>

      <div className="pt-tabs">
        {(["positions", "trades", "log"] as const).map((t) => (
          <button key={t} className={`pt-tab ${tab === t ? "pt-tab--on" : ""}`} onClick={() => setTab(t)}>
            {t === "positions" ? `Positions (${rows.length})` : t === "trades" ? `Trades (${state.trades.length})` : "Bot log"}
          </button>
        ))}
      </div>

      <div className="pt-scroll">
        {tab === "positions" && (
          rows.length === 0 ? (
            <p className="algo-hint">No positions yet — the bot places its first trades at 12:30 PM ET.</p>
          ) : (
            <table className="ev-table">
              <thead>
                <tr>
                  <th>Bucket</th><th>Side</th><th>Contracts</th><th>Avg price</th>
                  <th>Cost</th><th>Mark</th><th>Value</th><th>P/L</th><th>Status</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((r) => (
                  <tr key={`${r.market_ticker}-${r.side}`}>
                    <td>{r.market_label || r.market_ticker.split("-").pop()}</td>
                    <td><span className={`pt-side pt-side--${r.side}`}>{r.side.toUpperCase()}</span></td>
                    <td>{r.contracts.toLocaleString()}</td>
                    <td>{r.avg_price_cents.toFixed(1)}¢</td>
                    <td>{usd(r.cost_basis_cents)}</td>
                    <td>{r.settled ? "—" : r.mark != null ? `${r.mark}¢` : "n/a"}</td>
                    <td>{usd(r.value)}</td>
                    <td className={plClass(r.pl)}>{signedUsd(r.pl)}</td>
                    <td>
                      {r.settled
                        ? <span className={r.pl > 0 ? "pt-pos" : "pt-neg"}>{r.pl > 0 ? "Won" : "Lost"}</span>
                        : "Open"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )
        )}

        {tab === "trades" && (
          state.trades.length === 0 ? (
            <p className="algo-hint">No trades yet.</p>
          ) : (
            <table className="ev-table">
              <thead>
                <tr><th>Time (ET)</th><th>Bucket</th><th>Side</th><th>Price</th><th>Contracts</th><th>Cost</th></tr>
              </thead>
              <tbody>
                {state.trades.map((t) => (
                  <tr key={t.id}>
                    <td>{etTime(t.created_at, true)}</td>
                    <td>{t.market_label || t.market_ticker.split("-").pop()}</td>
                    <td><span className={`pt-side pt-side--${t.side}`}>{t.side.toUpperCase()}</span></td>
                    <td>{t.price_cents}¢</td>
                    <td>{t.contracts.toLocaleString()}</td>
                    <td>{usd(-t.cash_delta_cents)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )
        )}

        {tab === "log" && (
          state.runs.length === 0 ? (
            <p className="algo-hint">No bot cycles yet.</p>
          ) : (
            <table className="ev-table">
              <thead>
                <tr><th>Slot (ET)</th><th>Result</th><th>Forecast</th><th>Trades</th><th>Details</th></tr>
              </thead>
              <tbody>
                {state.runs.map((r) => (
                  <tr key={r.slot}>
                    <td>{slotTime(r.slot)}</td>
                    <td><span className={`pt-run pt-run--${r.status}`}>{r.status}</span></td>
                    <td>{r.forecast_mean != null ? `${r.forecast_mean.toFixed(1)}°F` : "—"}</td>
                    <td>{r.trades_count}</td>
                    <td className="pt-msg">{r.message}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )
        )}
      </div>
    </section>

    <aside className="forecast-history-panel pt-history-panel">
      <h3 className="algo-section-title">
        Trade History
        <span className="algo-hint-inline"> — saved to the Google Sheet “Trades” tab</span>
      </h3>
      {history === null ? (
        <p className="algo-hint">Loading trade history…</p>
      ) : historyRows.length === 0 ? (
        <p className="algo-hint">No trades recorded yet — each trade the bot places lands here and in the sheet.</p>
      ) : (
        <>
          <p className="pt-history-sum">
            {historyRows.length} trade{historyRows.length === 1 ? "" : "s"} · settled P/L{" "}
            <span className={plClass(historyNet)}>{signedUsd(historyNet)}</span>
          </p>
          <div className="forecast-history-scroll pt-history-scroll">
            <table className="forecast-history-table">
              <thead>
                <tr><th>Time (ET)</th><th>Bucket</th><th>Side</th><th>Traded</th><th>P/L</th></tr>
              </thead>
              <tbody>
                {historyRows.map((h, i) => {
                  const traded = h.cash_delta_cents != null ? Math.abs(h.cash_delta_cents) : null;
                  return (
                    <tr key={`${h.timestamp}-${h.market_ticker}-${h.side}-${i}`}>
                      <td>{h.timestamp ? etTime(h.timestamp, true) : "—"}</td>
                      <td>{h.market_label || h.market_ticker?.split("-").pop() || "—"}</td>
                      <td>
                        {h.side ? <span className={`pt-side pt-side--${h.side}`}>{h.side.toUpperCase()}</span> : "—"}
                        {h.action === "sell" && <span className="pt-history-sub"> sold</span>}
                      </td>
                      <td>
                        {traded != null ? usd(traded) : "—"}
                        <span className="pt-history-sub">
                          {h.contracts != null ? `${h.contracts.toLocaleString()} @ ${h.price_cents ?? "—"}¢` : ""}
                        </span>
                      </td>
                      <td>
                        {h.realized_pl_cents != null ? (
                          <span className={plClass(h.realized_pl_cents)}>{signedUsd(h.realized_pl_cents)}</span>
                        ) : (
                          <span className="pt-history-open">open</span>
                        )}
                        {h.status === "won" || h.status === "lost" ? (
                          <span className="pt-history-sub">{h.status}</span>
                        ) : null}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </>
      )}
    </aside>
    </div>
  );
}
