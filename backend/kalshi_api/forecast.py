"""
Background forecast runner.

Spawns all 6 ML model scripts as subprocesses in parallel every FORECAST_REFRESH_SECONDS (default 5 minutes),
caches their JSON output in memory, and exposes helpers for the algorithm.

The only live weather source is NOAA MADIS HFMETAR.  When it is unavailable the
runner does not run the models and does not fall back to anything else: it keeps
the last forecast, reports "waiting for HFMETAR", and retries every
HFMETAR_RETRY_SECONDS until observations are available again.
"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path


MODELS_DIR = Path(__file__).resolve().parent.parent / "modelscripts"

MODELS = [
    {"name": "accuweather_1h", "script": "accuweather_forecast.py"},
    {"name": "accuweather_2h", "script": "accuweather_forecast_2hour.py"},
    {"name": "accuweather_3h", "script": "accuweather_forecast_3hour.py"},
    {"name": "var_1h",         "script": "variable_combined.py"},
    {"name": "var_2h",         "script": "variable_combined_2hour.py"},
    {"name": "var_3h",         "script": "variable_combined_3hour.py"},
]

REFRESH_INTERVAL = int(os.getenv("FORECAST_REFRESH_SECONDS", "300"))  # seconds (default 5 minutes)
HFMETAR_RETRY_SECONDS = int(os.getenv("HFMETAR_RETRY_SECONDS", "30"))  # retry cadence while waiting for HFMETAR
STARTUP_DELAY = 30      # let lightweight /ping/ respond before CPU-heavy models start

_lock = threading.Lock()
_started = False
_loop = {
    "started": False,
    "started_at": None,
    "last_cycle_started_at": None,
    "last_cycle_finished_at": None,
    "next_run_at": None,
    "last_error": None,
    "waiting_for_hfmetar": None,   # {"reason": str, "since": iso} while HFMETAR is unavailable
}
_cache = {
    "results":      {},     # model_name -> dict (or {"error": ...})
    "last_run_at":  None,   # ISO string
    "running":      False,
    "asos_current": None,   # {"t0_f": float, "today_high_f": float} or None
}


def _apply_asos_current(asos: dict):
    """Push the latest KMIA ASOS reading into the shared temp tracker."""
    if not asos:
        return
    with _lock:
        _cache["asos_current"] = asos

    from .price_tracker import set_daily_high_nws, update_temp

    if asos.get("t0_f") is not None:
        update_temp(float(asos["t0_f"]))
    if asos.get("today_high_f") is not None:
        set_daily_high_nws(float(asos["today_high_f"]))


def _cached_model_high_f():
    with _lock:
        results = dict(_cache.get("results") or {})
    highs = []
    for data in results.values():
        if not isinstance(data, dict) or "error" in data:
            continue
        try:
            t0 = data.get("T0")
            if t0 is not None:
                highs.append(float(t0))
        except (TypeError, ValueError):
            pass
    return max(highs) if highs else None


def _wind_block(speed, direction, imperial):
    """
    AccuWeather-shaped wind dict that OMITS unknown values.  The model scripts
    default a missing key to NaN, but float(None) raises, and a calm METAR
    (0 mph) has no wind direction.
    """
    spd = {"Value": speed}
    if imperial:
        spd = {"Imperial": spd}
    block = {}
    if speed is not None:
        block["Speed"] = spd
    if direction is not None:
        block["Direction"] = {"Degrees": direction}
    return block


def _fetch_hfmetar_shared():
    """
    One NOAA MADIS HFMETAR fetch per cycle, shared across all model subprocesses.

    The model scripts read their inputs from a JSON file (AW_SHARED_DATA) shaped as
    {"curr_raw", "fore_raw", "asos_current"}.  The HFMETAR observation and its
    ASOS-trend nowcast are written into exactly that shape, so the scripts need no changes.
    Returns (shared_dict_or_None, weather_metadata).  None means HFMETAR is unavailable
    and the models must not run (they would otherwise fall back to a different provider).
    """
    from weather import build_live_shared_input
    from weather.asos_nowcast import STATUS_UNAVAILABLE, build_future_features
    from weather.asos_parser import parse_utc

    shared = build_live_shared_input()
    md = shared["metadata"]
    cur = shared.get("current")
    if md["status"] == STATUS_UNAVAILABLE or not cur:
        return None, md

    now = datetime.now(timezone.utc)
    obs_dt = parse_utc(cur["observed_at"])

    def _num(x):
        return None if x is None else float(x)

    pressure_hpa = _num(cur.get("pressure_hpa"))
    curr_raw = {
        "EpochTime": int(obs_dt.timestamp()),
        "Temperature": {"Imperial": {"Value": float(cur["temp_f"])}},
        "CloudCover": None if cur.get("cloud_fraction") is None else float(cur["cloud_fraction"]) * 100.0,
        "RelativeHumidity": None if cur.get("relative_humidity") is None else float(cur["relative_humidity"]) * 100.0,
        "Wind": _wind_block(_num(cur.get("wind_speed_mph")), _num(cur.get("wind_dir_deg")), imperial=True),
        "Pressure": {"Imperial": {"Value": None if pressure_hpa is None else pressure_hpa / 33.8639}},
        "UVIndex": 0.0,
    }

    # Hourly covariate path (+1h .. +6h) from the HFMETAR trend nowcast, not a forecast provider.
    obs_all = sorted(shared.get("recent_observations") or [], key=lambda o: o["observed_at"])
    fore_raw = []
    for row in build_future_features(obs_all, now, steps=[60, 120, 180, 240, 300, 360]):
        rh = row.get("relative_humidity")
        cc = row.get("cloud_fraction")
        fore_raw.append({
            "EpochDateTime": int(parse_utc(row["valid_at"]).timestamp()),
            "Temperature": {"Value": float(cur["temp_f"])},
            "CloudCover": None if cc is None else cc * 100.0,
            "RelativeHumidity": {"Value": None if rh is None else rh * 100.0},
            "Wind": _wind_block(row.get("wind_speed_mph"), row.get("wind_dir_deg"), imperial=False),
        })

    return {
        "curr_raw": curr_raw,
        "fore_raw": fore_raw,
        "asos_current": {"t0_f": float(cur["temp_f"]),
                         "today_high_f": (shared.get("today") or {}).get("high_f"),
                         "observed_at": cur["observed_at"],
                         # Live pressure tendency (hPa/h) so the models don't reuse a stale cached value.
                         "pressure_trend_hpa_per_hr": (shared.get("features") or {}).get("pressure_trend_hpa_per_hr"),
                         "source": md["source"]},
    }, md



def _run_one_model(model, aw_shared_path=None):
    script = MODELS_DIR / model["script"]
    env = {**os.environ}
    if aw_shared_path:
        env["AW_SHARED_DATA"] = aw_shared_path
    try:
        proc = subprocess.run(
            [sys.executable, str(script), "--json-output"],
            capture_output=True,
            text=True,
            timeout=180,
            cwd=str(MODELS_DIR),
            env=env,
        )
        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout or "")[-1200:]
            return model["name"], {"error": f"exit {proc.returncode}: {tail}"}
        data = json.loads(proc.stdout.strip())
        return model["name"], data
    except subprocess.TimeoutExpired:
        return model["name"], {"error": "timeout after 180s"}
    except json.JSONDecodeError as exc:
        return model["name"], {"error": f"JSON parse error: {exc}"}
    except Exception as exc:
        return model["name"], {"error": str(exc)}


def _do_refresh():
    """
    Run all models once.  Returns True if they ran, False if HFMETAR is unavailable
    (nothing ran; the caller should retry soon), None if a refresh was already running.
    """
    with _lock:
        if _cache["running"]:
            return None
        _cache["running"] = True

    # Fetch NOAA MADIS HFMETAR once and share it across all model subprocesses.
    aw_shared_path = None
    unavailable = None
    try:
        raw, weather_md = _fetch_hfmetar_shared()
        if raw is None:
            unavailable = (f"weather_unavailable: {weather_md.get('unavailable_reason')}")
        else:
            _apply_asos_current(raw["asos_current"])
            tmp = tempfile.NamedTemporaryFile(
                mode="w", suffix=".json", delete=False, prefix="hfmetar_shared_"
            )
            json.dump(raw, tmp)
            tmp.close()
            aw_shared_path = tmp.name
    except Exception as exc:
        unavailable = f"weather_unavailable: HFMETAR fetch failed ({exc})"
        print(f"[forecast] {unavailable}")

    if unavailable:
        # No fallback provider: keep the last forecast, flag the wait, and let the caller retry soon.
        with _lock:
            waiting = _loop["waiting_for_hfmetar"]
            first_time = waiting is None
            _loop["waiting_for_hfmetar"] = {
                "reason": unavailable,
                "since": waiting["since"] if waiting else datetime.utcnow().isoformat(),
            }
            _cache["running"] = False
        if first_time:
            print(f"[forecast] waiting for HFMETAR — {unavailable}; retrying every {HFMETAR_RETRY_SECONDS}s")
        return False

    with _lock:
        if _loop["waiting_for_hfmetar"] is not None:
            print("[forecast] HFMETAR is back — running models")
        _loop["waiting_for_hfmetar"] = None

    results = {}
    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = {pool.submit(_run_one_model, m, aw_shared_path): m for m in MODELS}
            for future in as_completed(futures):
                name, data = future.result()
                results[name] = data
    finally:
        if aw_shared_path:
            try:
                os.unlink(aw_shared_path)
            except OSError:
                pass
        # Always release the running lock — even if an exception killed the executor
        with _lock:
            _cache["results"]      = results
            _cache["last_run_at"]  = datetime.utcnow().isoformat()
            _cache["running"]      = False

    _record_forecast_snapshot(results)
    return True


def _record_forecast_snapshot(results: dict):
    """Persist a history row: current temp + ensemble forecast at this refresh cycle."""
    try:
        from .algorithm import get_ensemble_forecast
        from .price_tracker import get_temp_snapshot
        from .sheets_history import append_snapshot

        ensemble = get_ensemble_forecast(results)
        current_f = get_temp_snapshot().get("current_f")
        if not ensemble or current_f is None:
            return
        from .forecast_log import append_forecast
        append_forecast(ensemble, current_f, get_temp_snapshot().get("daily_high_f"))
        append_snapshot(
            current_temp_f=current_f,
            model_forecast_f=ensemble.get("mean"),
        )
    except Exception as exc:
        print(f"[forecast] failed to record history snapshot ({exc})")


def _background_loop():
    with _lock:
        _loop["started"] = True
        _loop["started_at"] = datetime.utcnow().isoformat()
        _loop["next_run_at"] = datetime.utcfromtimestamp(
            time.time() + STARTUP_DELAY
        ).isoformat()
    time.sleep(STARTUP_DELAY)
    while True:
        started_at = time.time()
        with _lock:
            _loop["last_cycle_started_at"] = datetime.utcnow().isoformat()
            _loop["next_run_at"] = None
            _loop["last_error"] = None
        interval = REFRESH_INTERVAL
        try:
            if _do_refresh() is False:
                interval = HFMETAR_RETRY_SECONDS   # HFMETAR unavailable — keep polling for it
        except Exception as exc:
            error = str(exc)
            with _lock:
                _loop["last_error"] = error
            print(f"[forecast] background refresh crashed ({exc}); will retry in {REFRESH_INTERVAL}s")
        elapsed = time.time() - started_at
        sleep_for = max(0, interval - elapsed)
        with _lock:
            _loop["last_cycle_finished_at"] = datetime.utcnow().isoformat()
            _loop["next_run_at"] = datetime.utcfromtimestamp(
                time.time() + sleep_for
            ).isoformat()
        time.sleep(sleep_for)


def start_background_refresh():
    """Call once at Django startup (AppConfig.ready)."""
    global _started
    with _lock:
        if _started:
            return
        _started = True
    t = threading.Thread(target=_background_loop, daemon=True, name="forecast-bg")
    t.start()


def trigger_refresh():
    """Fire-and-forget manual refresh (used by API endpoint)."""
    t = threading.Thread(target=_do_refresh, daemon=True, name="forecast-manual")
    t.start()


def _legacy_refresh_temp_now() -> dict | None:
    from .temp_monitor import refresh_temp_now
    return refresh_temp_now()


def get_forecast_cache():
    """Return a snapshot of the cache (thread-safe copy)."""
    with _lock:
        return {
            "results":      dict(_cache["results"]),
            "last_run_at":  _cache["last_run_at"],
            "running":      _cache["running"],
            "asos_current": _cache["asos_current"],
            "loop":         dict(_loop),
        }


def ensemble_prob_above(strike_f, results=None):
    """
    Return the average P(T_forecast > strike_f) across all model runs that
    succeeded.  Returns None when no model data is available yet.
    """
    if results is None:
        with _lock:
            results = dict(_cache["results"])

    probs = []
    for data in results.values():
        if "error" in data or "temps" not in data:
            continue
        p = sum(prob for t, prob in zip(data["temps"], data["probs"]) if t > strike_f)
        probs.append(p)

    return (sum(probs) / len(probs)) if probs else None
