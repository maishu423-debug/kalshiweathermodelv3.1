"""
Model-driven paper-trading strategy.

ENSEMBLE:     Each of the 6 models (A1,V1,A2,V2,A3,V3) yields a daily-high PDF
              (max over its simulated paths); they are mixed with
              HORIZON_MIX[h] * in-pair weight into one daily-high PDF.
EACH CYCLE:   Rank the live Kalshi buckets by model probability.
              YES: only on the single highest-probability bucket, and only if
                   our model probability is HIGHER than Kalshi's price for it.
              NO:  on every other live bucket whose model probability is LOWER
                   than Kalshi's price for it.
              Otherwise no trade on that bucket.
NO SELLING:   Contracts are held until Kalshi settles the market.
"""
from collections import defaultdict
from datetime import datetime, timezone

from .forecast import get_forecast_cache
from .price_tracker import get_all_velocities, get_temp_snapshot, nws_round_temp_f


MIAMI_UTC_OFFSET  = -4
CUTOFF_LOCAL_HOUR = 16

# Never pay more than this for a contract (above it there is no upside left).
NO_MAX_PRICE_CENTS = 98
YES_MAX_PRICE_CENTS = 98

# Weighted combination within each horizon pair.
# AccuWeather and Variable-combined models are paired by forecast horizon.
# Weights can be tuned from backtesting results.
HORIZON_WEIGHTS = {
    "1h": {"accuweather_1h": 0.5788, "var_1h": 0.4212},
    "2h": {"accuweather_2h": 0.5744, "var_2h": 0.4256},
    "3h": {"accuweather_3h": 0.5643, "var_3h": 0.4357},
}

# Weight of each horizon pair in the final daily-high mixture (all six models contribute).
# Equal by default; tune from backtesting. Only the ratios matter (re-normalized).
HORIZON_MIX = {"1h": 1.0, "2h": 1.0, "3h": 1.0}


def hours_to_cutoff() -> float:
    now = datetime.now(timezone.utc)
    local_h = (now.hour + MIAMI_UTC_OFFSET) % 24 + now.minute / 60.0
    return max(0.0, CUTOFF_LOCAL_HOUR - local_h)


def _nws_known_high_f(*values) -> int | None:
    rounded = [nws_round_temp_f(v) for v in values if v is not None]
    return max(rounded) if rounded else None


# ── Step 1: same-horizon ensemble PDFs ───────────────────────────────────────

def _get_horizon_pdfs(results: dict) -> dict:
    """
    For each horizon (1h, 2h, 3h) compute a weighted ensemble PDF over
    integer temperatures.  If one model in a pair is missing/failed, the
    available model gets full weight (after re-normalizing).

    Returns {horizon_key: {temp_int: probability}}.
    """
    horizon_pdfs: dict[str, dict[int, float]] = {}

    for horizon, weights in HORIZON_WEIGHTS.items():
        combined: dict[int, float] = defaultdict(float)
        total_weight = 0.0

        for model_name, weight in weights.items():
            data = results.get(model_name, {})
            if "error" in data or "temps" not in data or "probs" not in data:
                continue
            for t, p in zip(data["temps"], data["probs"]):
                combined[int(t)] += weight * p
            total_weight += weight

        if not combined or total_weight == 0:
            continue

        norm = sum(combined.values())
        if norm > 0:
            horizon_pdfs[horizon] = {t: p / norm for t, p in combined.items()}

    return horizon_pdfs


# ── Step 2: daily-high PDF ───────────────────────────────────────────────────

def _get_path_high_pdf(results: dict) -> dict:
    """
    Daily-high PDF from ALL six models, built from the running maximum of each
    model's *simulated paths* (keys "high_temps"/"high_probs").  Each model's
    PDF is weighted by HORIZON_MIX[horizon] * its in-pair weight from
    HORIZON_WEIGHTS; models that errored, are disabled or lack the keys are
    dropped and the rest re-normalized.  Returns {} if no model provides it so
    the caller can fall back.
    """
    combined: dict[int, float] = defaultdict(float)
    total_weight = 0.0
    for horizon, pair in HORIZON_WEIGHTS.items():
        for model_name, pair_weight in pair.items():
            data = results.get(model_name, {})
            if "error" in data or "high_temps" not in data or "high_probs" not in data:
                continue
            weight = HORIZON_MIX[horizon] * pair_weight
            for t, p in zip(data["high_temps"], data["high_probs"]):
                combined[int(t)] += weight * p
            total_weight += weight
    if not combined or total_weight == 0:
        return {}
    norm = sum(combined.values())
    return {t: p / norm for t, p in combined.items()} if norm > 0 else {}


def _floor_pdf_at(pdf: dict, known_high_f: float | None) -> dict:
    """The final high can't be below what has already been observed: fold mass below it onto it."""
    if known_high_f is None:
        return dict(pdf)
    h_now = nws_round_temp_f(known_high_f)
    out: dict[int, float] = defaultdict(float)
    for t, p in pdf.items():
        out[max(int(t), h_now)] += p
    return dict(out)


def _compute_daily_high_pdf(horizon_pdfs: dict, known_high_f: float | None,
                            path_high_pdf: dict | None = None) -> dict:
    """
    P(final daily high = k) over integer temperatures.

    Preferred: `path_high_pdf`, the PDF of the max over each simulated path (see
    _get_path_high_pdf), floored at the observed high.

    Fallback (only when path PDFs are unavailable): treat the hourly draws as
    independent and take their max with the observed current high,

      P(final_high <= k) = 0                       if k < rounded known_high
                         = prod_h P(T_h <= k)       otherwise

    This is biased warm — the horizons are strongly correlated, and the product
    of CDFs of correlated variables is smaller than the CDF of their max (~+0.5F).
    """
    if path_high_pdf:
        return _floor_pdf_at(path_high_pdf, known_high_f)
    if not horizon_pdfs:
        return {}

    all_temps: set[int] = set()
    for pdf in horizon_pdfs.values():
        all_temps.update(pdf.keys())
    if not all_temps:
        return {}

    h_now  = nws_round_temp_f(known_high_f) if known_high_f is not None else min(all_temps)
    t_min  = min(min(all_temps), h_now)
    t_max  = max(all_temps)

    def _cdf(pdf: dict, k: int) -> float:
        return sum(p for t, p in pdf.items() if t <= k)

    prev_cdf = 0.0
    high_pdf: dict[int, float] = {}
    for k in range(t_min, t_max + 2):
        if k < h_now:
            cur_cdf = 0.0
        else:
            cur_cdf = 1.0
            for pdf in horizon_pdfs.values():
                cur_cdf *= _cdf(pdf, k)
        p = cur_cdf - prev_cdf
        if p > 1e-6:
            high_pdf[k] = p
        prev_cdf = cur_cdf

    total = sum(high_pdf.values())
    if total > 0:
        high_pdf = {k: p / total for k, p in high_pdf.items()}
    return high_pdf


# ── Step 3: bracket probability from daily-high PDF ──────────────────────────

def _in_bracket(t, floor, cap) -> bool:
    """
    Does integer temperature t settle a Kalshi bucket YES?
      between (floor, cap):  floor <= t <= cap   ("87° to 88°" is floor 87, cap 88)
      greater (floor only):  t > floor           ("89° or above" is floor 88)
      less    (cap only):    t < cap             ("80° or below" is cap 81)
    """
    if floor is not None and cap is not None:
        return float(floor) <= t <= float(cap)
    if floor is not None:
        return t > float(floor)
    if cap is not None:
        return t < float(cap)
    return True


def _bracket_prob(floor, cap, high_pdf: dict) -> float:
    """P(final_daily_high settles the bucket) using integer temperatures."""
    return round(sum(p for t, p in high_pdf.items() if _in_bracket(t, floor, cap)), 4)


# ── Market analysis ───────────────────────────────────────────────────────────

def analyze_markets(markets: list) -> list:
    temp         = get_temp_snapshot()
    current_f    = temp["current_f"]
    daily_high_f = temp["daily_high_f"]
    known        = _nws_known_high_f(current_f, daily_high_f) or 0

    tickers    = [m["ticker"] for m in markets]
    velocities = get_all_velocities(tickers)

    out = []
    for market in markets:
        status = market.get("status", "")
        if status and status not in ("open", "active"):
            continue

        ticker  = market["ticker"]
        floor   = market.get("floor_strike")
        cap     = market.get("cap_strike")
        yes_ask = market.get("yes_ask_cents")
        yes_bid = market.get("yes_bid_cents")
        no_ask  = market.get("no_ask_cents")
        no_bid  = market.get("no_bid_cents")

        if yes_ask is None and yes_bid is None:
            continue

        if no_ask is None and yes_bid is not None:
            no_ask = 100 - yes_bid

        # The daily high only rises: a bucket is dead once the known high is above its top.
        is_dead = (
            (cap is not None and known > (float(cap) if floor is not None else float(cap) - 1))
            or (yes_ask is not None and yes_ask <= 3)
        )

        vels    = velocities.get(ticker, {})
        yes_vel = vels.get("yes_velocity", 0.0)
        no_vel  = vels.get("no_velocity",  0.0)

        out.append({
            "ticker":        ticker,
            "label":         market.get("label", ""),
            "floor":         floor,
            "cap":           cap,
            "yes_ask_cents": yes_ask,
            "yes_bid_cents": yes_bid,
            "no_ask_cents":  no_ask,
            "no_bid_cents":  no_bid,
            "volume":        market.get("volume"),
            "yes_velocity":  round(yes_vel, 3),
            "no_velocity":   round(no_vel,  3),
            "is_dead":       is_dead,
            "model_prob":    None,  # filled in by get_recommendation
        })

    out.sort(key=lambda x: x["is_dead"])
    for i, row in enumerate(out):
        row["rank"] = i + 1

    return out


# ── Recommendation ────────────────────────────────────────────────────────────

def get_recommendation(analyses: list, results=None) -> dict:
    """
    Strategy run each cycle:
      1. Rank live brackets by model probability.
      2. YES on the highest-probability bracket, only if model_prob > Kalshi's
         price for YES on that bracket (its YES ask).
      3. NO on every other live bracket, only if model_prob < Kalshi's implied
         YES probability on that bracket at the price we would pay for NO
         (1 - NO ask).
      Brackets without an edge are skipped.

    Return shape:
      {
        "status":     "ready" | "waiting",
        "reason":     str,
        "mean":       float | None,   # ensemble mean daily high
        "yes_trade":  {ticker, label, side, price_cents, model_prob, kalshi_prob} | None,
        "no_trades":  [{ticker, label, side, price_cents, model_prob, kalshi_prob}, ...],
      }
    `kalshi_prob` is Kalshi's implied probability that the bracket settles YES.
    """
    if results is None:
        results = get_forecast_cache().get("results", {})

    temp     = get_temp_snapshot()
    known_hi = _nws_known_high_f(temp.get("current_f"), temp.get("daily_high_f"))

    horizon_pdfs = _get_horizon_pdfs(results)
    high_pdf     = _compute_daily_high_pdf(horizon_pdfs, known_hi, _get_path_high_pdf(results))
    has_models   = bool(high_pdf)

    def waiting(reason, mean=None):
        return {"status": "waiting", "reason": reason, "mean": mean, "yes_trade": None, "no_trades": []}

    live = [a for a in analyses if not a["is_dead"]]
    for bracket in live:
        bracket["model_prob"] = (
            _bracket_prob(bracket.get("floor"), bracket.get("cap"), high_pdf)
            if has_models else None
        )

    if not live:
        return waiting("All brackets eliminated by daily high.")
    if not has_models:
        return waiting("Waiting for model forecasts to load.")

    mean_high = round(sum(t * p for t, p in high_pdf.items()), 2)

    def leg(bracket, side, price_cents):
        model_p = bracket["model_prob"] or 0.0
        kalshi_p = price_cents / 100.0 if side == "yes" else 1.0 - price_cents / 100.0
        return {
            "ticker":      bracket["ticker"],
            "label":       bracket["label"],
            "side":        side,
            "price_cents": price_cents,
            "model_prob":  model_p,
            "kalshi_prob": round(kalshi_p, 4),
        }

    top = max(live, key=lambda b: b["model_prob"] or 0.0)
    yes_trade = None
    yes_ask = top.get("yes_ask_cents")
    if yes_ask and yes_ask <= YES_MAX_PRICE_CENTS:
        candidate = leg(top, "yes", yes_ask)
        if candidate["model_prob"] > candidate["kalshi_prob"]:
            yes_trade = candidate

    no_trades = []
    for b in live:
        no_ask = b.get("no_ask_cents")
        if b["ticker"] == top["ticker"] or not no_ask or no_ask > NO_MAX_PRICE_CENTS:
            continue
        candidate = leg(b, "no", no_ask)
        if candidate["model_prob"] < candidate["kalshi_prob"]:
            no_trades.append(candidate)

    if yes_trade is None and not no_trades:
        top_ask = top.get("yes_ask_cents")
        kalshi_txt = f"Kalshi {top_ask}%" if top_ask else "no Kalshi price"
        return waiting(
            f"No edge: top bucket {top['label']} model {(top['model_prob'] or 0.0) * 100:.0f}% vs {kalshi_txt}; "
            f"no NO bucket priced above our model.",
            mean_high,
        )

    parts = []
    if yes_trade:
        parts.append(
            f"YES {yes_trade['label']} (model {yes_trade['model_prob'] * 100:.0f}% "
            f"> Kalshi {yes_trade['kalshi_prob'] * 100:.0f}%)"
        )
    if no_trades:
        parts.append(f"NO x{len(no_trades)}: " + ", ".join(t["label"] for t in no_trades))

    return {
        "status":    "ready",
        "reason":    f"Forecast {mean_high:.1f}°F — " + " | ".join(parts),
        "mean":      mean_high,
        "yes_trade": yes_trade,
        "no_trades": no_trades,
    }


def _compute_ensemble_stats(results: dict, known_hi: float | None, current_temp_f: float | None) -> dict:
    """Ensemble daily-high forecast stats (mean/mode/p10/p90/dist) from model results."""
    horizon_pdfs = _get_horizon_pdfs(results)
    high_pdf      = _compute_daily_high_pdf(horizon_pdfs, known_hi, _get_path_high_pdf(results))
    if not high_pdf:
        return {}

    sorted_temps = sorted(high_pdf.keys())
    mean_high    = sum(t * high_pdf[t] for t in sorted_temps)
    mode_high    = max(high_pdf, key=high_pdf.get)
    cdf_val, p10, p90 = 0.0, None, None
    for t in sorted_temps:
        cdf_val += high_pdf[t]
        if p10 is None and cdf_val >= 0.10:
            p10 = t
        if p90 is None and cdf_val >= 0.90:
            p90 = t

    n_valid = sum(
        1 for name, data in results.items()
        if "error" not in data and "temps" in data
    )
    return {
        "current_temp_f": round(current_temp_f or 0, 1),
        "mean":           round(mean_high, 1),
        "mode":           mode_high,
        "mode_prob":      round(high_pdf[mode_high], 4),
        "p10":            p10,
        "p90":            p90,
        "n_models":       n_valid,
        "dist":           [{"temp": t, "prob": round(high_pdf[t], 4)} for t in sorted_temps],
    }


def get_ensemble_forecast(results=None) -> dict:
    """
    Ensemble daily-high forecast summary, independent of any specific markets.
    Used by the background forecast refresh loop to record history snapshots.
    """
    if results is None:
        results = get_forecast_cache().get("results", {})
    temp     = get_temp_snapshot()
    known_hi = _nws_known_high_f(temp.get("current_f"), temp.get("daily_high_f"))
    return _compute_ensemble_stats(results, known_hi, temp.get("current_f"))


# ── Master state ──────────────────────────────────────────────────────────────

def get_full_state(markets: list, paper_state=None, enabled_models=None) -> dict:
    cache       = get_forecast_cache()
    all_results = cache.get("results", {})
    cutoff_h    = hours_to_cutoff()
    nws_temp    = get_temp_snapshot()   # pure NWS — used for display only
    temp        = dict(nws_temp)        # algorithm copy, augmented below with model T0s

    model_t0s = []
    for data in all_results.values():
        if isinstance(data, dict) and "error" not in data and data.get("T0") is not None:
            try:
                model_t0s.append(float(data["T0"]))
            except (TypeError, ValueError):
                pass
    if model_t0s:
        observed_high = max([temp.get("daily_high_f") or 0.0, *model_t0s])
        temp = {**temp, "daily_high_f": observed_high}  # augmented for algorithm only

    # Filter to enabled models for recommendation and ensemble
    results = (
        {k: v for k, v in all_results.items() if k in enabled_models}
        if enabled_models is not None
        else all_results
    )

    # current_f is set by AccuWeather (every 15 min in _do_refresh, or on manual refresh).
    # NWS ASOS only advances daily_high_f — never sets current_f (Celsius-rounding issue).
    # Do not call update_temp() here — model T0s are already folded in via model_t0s above.

    analyses       = analyze_markets(markets)
    recommendation = get_recommendation(analyses, results=results)

    # Per-model summary — always show all cached models in UI
    models_summary = {}
    for name, data in all_results.items():
        if "error" in data:
            models_summary[name] = {"error": data["error"]}
        else:
            probs = data.get("probs", [])
            temps = data.get("temps", [])
            models_summary[name] = {
                "T0":         data.get("T0"),
                "mean":       data.get("mean"),
                "std":        data.get("std"),
                "mode":       data.get("mode"),
                "mode_prob":  round(max(probs), 4) if probs else None,
                "p10":        data.get("p10"),
                "p90":        data.get("p90"),
                "dist":       [
                    {"temp": int(t), "prob": round(float(p), 4)}
                    for t, p in zip(temps, probs)
                ],
                "fetched_at": data.get("fetched_at"),
                "fit_source": data.get("fit_source"),
            }

    # Ensemble stats derived from the daily-high PDF (using enabled models only)
    known_hi = _nws_known_high_f(temp.get("current_f"), temp.get("daily_high_f"))
    ensemble = _compute_ensemble_stats(results, known_hi, temp.get("current_f"))

    h_rem = int(cutoff_h)
    m_rem = int((cutoff_h - h_rem) * 60)

    return {
        "forecast": {
            "models":      models_summary,
            "ensemble":    ensemble,
            "last_run_at": cache.get("last_run_at"),
            "running":     cache.get("running", False),
            "loop":        cache.get("loop"),
        },
        "temp": {
            "current_f":    round(nws_temp["current_f"],    1) if nws_temp["current_f"]    is not None else None,
            "daily_high_f": nws_round_temp_f(nws_temp["daily_high_f"]) if nws_temp["daily_high_f"] is not None else None,
        },
        "analysis":       analyses,
        "recommendation": recommendation,
        "time_to_cutoff": {
            "hours":   round(cutoff_h, 2),
            "display": f"{h_rem}h {m_rem}m remaining" if cutoff_h > 0 else "Market closed",
        },
        "models_ready": bool(ensemble),
    }
