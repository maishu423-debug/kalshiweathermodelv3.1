"""
Score logged ensemble forecasts against the settled KMIA daily high (Iowa Mesonet daily summary).

Usage (from backend/):
    python tools/score_forecasts.py [path/to/forecast_log.csv] [--hours 11 14]

Reports bias (forecast - actual), MAE and how often the forecast was >=1F too high / too low, by month and by
local decision hour.  Use it to check/tune SIGMA_SCALE in modelscripts/*3hour.py: if the overall bias is
persistently positive lower it, if negative raise it.  Days still in progress are skipped.
"""
import argparse
import io
import os
import sys
from datetime import date

import pandas as pd
import requests

DEFAULT_LOG = os.path.join(os.path.dirname(__file__), "..", "forecast_log.csv")


def fetch_daily_highs(start: date, end: date) -> pd.Series:
    resp = requests.get(
        "https://mesonet.agron.iastate.edu/cgi-bin/request/daily.py",
        params={"network": "FL_ASOS", "stations": "MIA",
                "year1": start.year, "month1": start.month, "day1": start.day,
                "year2": end.year, "month2": end.month, "day2": end.day, "format": "csv"},
        timeout=60)
    resp.raise_for_status()
    d = pd.read_csv(io.StringIO(resp.text), parse_dates=["day"])
    return d.set_index(d["day"].dt.date)["max_temp_f"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("log", nargs="?", default=DEFAULT_LOG)
    ap.add_argument("--hours", nargs=2, type=float, default=[11.0, 15.0], help="local decision-hour window")
    a = ap.parse_args()

    log = pd.read_csv(a.log, parse_dates=["local_date"])
    log["local_date"] = log["local_date"].dt.date
    log = log[(log.local_hour >= a.hours[0]) & (log.local_hour <= a.hours[1])]
    today = pd.Timestamp.now(tz="America/New_York").date()
    log = log[log.local_date < today]                      # settled days only
    if log.empty:
        sys.exit("No settled days in the log yet.")

    actual = fetch_daily_highs(min(log.local_date), max(log.local_date))
    log["actual"] = log.local_date.map(actual)
    log = log.dropna(subset=["actual"])
    log["err"] = log.ensemble_mean - log.actual
    log["month"] = pd.to_datetime(log.local_date).dt.to_period("M")
    log["hour"] = log.local_hour.astype(int)

    def summarize(g):
        return pd.Series({"n": len(g), "days": g.local_date.nunique(), "bias": g.err.mean(), "MAE": g.err.abs().mean(),
                          "over>=1F": (g.err >= 1).mean(), "under<=-1F": (g.err <= -1).mean()})

    print(f"Scored {len(log)} forecasts over {log.local_date.nunique()} settled days "
          f"(local hours {a.hours[0]:g}-{a.hours[1]:g})\n")
    print("Overall:\n", summarize(log).round(2).to_string(), "\n")
    print("By month:\n", log.groupby("month").apply(summarize).round(2).to_string(), "\n")
    print("By local hour:\n", log.groupby("hour").apply(summarize).round(2).to_string())


if __name__ == "__main__":
    main()
