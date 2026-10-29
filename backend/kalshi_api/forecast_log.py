"""
Append-only local CSV log of every ensemble forecast, so the model's bias can be measured against settled daily
highs (see backend/tools/score_forecasts.py).  Never raises: logging must not interfere with forecasting.

Path: FORECAST_LOG_PATH env var, default backend/forecast_log.csv.  On hosts with an ephemeral disk the file is
lost on redeploy; point FORECAST_LOG_PATH at a persistent volume if you want a long record.
"""
import csv
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

HEADER = ["ts_utc", "local_date", "local_hour", "current_f", "known_high_f",
          "ensemble_mean", "ensemble_mode", "p10", "p90", "n_models"]
DEFAULT_PATH = Path(__file__).resolve().parent.parent / "forecast_log.csv"
_lock = threading.Lock()


def _path() -> Path:
    return Path(os.getenv("FORECAST_LOG_PATH") or DEFAULT_PATH)


def append_forecast(ensemble: dict, current_f, known_high_f, now=None):
    """Append one row. `ensemble` is algorithm.get_ensemble_forecast() output."""
    try:
        if not ensemble or ensemble.get("mean") is None:
            return
        now = now or datetime.now(timezone.utc)
        local = now.astimezone(ZoneInfo("America/New_York"))
        row = [now.isoformat(), local.date().isoformat(), round(local.hour + local.minute / 60.0, 2),
               current_f, known_high_f, ensemble.get("mean"), ensemble.get("mode"),
               ensemble.get("p10"), ensemble.get("p90"), ensemble.get("n_models")]
        path = _path()
        with _lock:
            new_file = not path.exists()
            with open(path, "a", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                if new_file:
                    w.writerow(HEADER)
                w.writerow(row)
    except Exception as exc:
        print(f"[forecast_log] failed to log forecast ({exc})")
