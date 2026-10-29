"""
Shadow comparison of the two live sources (ASOS_SHADOW_COMPARE=true).

The PRIMARY source (ASOS_SOURCE) keeps feeding forecasts exactly as before. After each primary poll this module
fetches the ALTERNATE source on a separate daemon thread, compares the two, and records the result. It only
observes:

  * the shadow feed has its own cache and never reads or writes asos_last_good.json (persist=False);
  * it never touches the primary feed, the price tracker, the forecast cache or any trading state;
  * it runs at most once per ASOS_SHADOW_MIN_INTERVAL_SECONDS (default 60), one comparison at a time,
    and only looks at the current and previous UTC hour (a couple of conditional requests);
  * any failure (including a missing LDM token) is recorded as the shadow's error and nothing else.

Each comparison is printed as a `[ASOS-SHADOW]` line, kept in a bounded in-memory history (see summary(),
served by the source-health endpoint) and, if ASOS_SHADOW_LOG_PATH is set, appended as one JSON line.
"""
import json
import os
import statistics
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

from . import asos_parser, config as cfg
from .ldm_client import env_flag

DEFAULT_MIN_INTERVAL_SECONDS = 60
HISTORY_SIZE = 288                 # a day of comparisons at one per five minutes
TEMP_TOLERANCE_F = 0.05            # a difference smaller than this is rounding, not a mismatch
MAX_LOG_BYTES = 5 * 1024 * 1024
COMPARE_LAST_N = 24                # newest observations of each source considered for the overlap

_lock = threading.Lock()
_state = {"thread": None, "last_started": None, "feed": None, "feed_source": None}
_history = deque(maxlen=HISTORY_SIZE)


def enabled(env=None):
    return env_flag("ASOS_SHADOW_COMPARE", False, env)


def alternate_source(primary):
    return cfg.ASOS_SOURCE_PUBLIC if primary == cfg.ASOS_SOURCE_LDM else cfg.ASOS_SOURCE_LDM


def _min_interval(env=None):
    env = os.environ if env is None else env
    try:
        return max(5.0, float(env.get("ASOS_SHADOW_MIN_INTERVAL_SECONDS") or DEFAULT_MIN_INTERVAL_SECONDS))
    except ValueError:
        return float(DEFAULT_MIN_INTERVAL_SECONDS)


def reset():
    """Forget all shadow state (tests)."""
    with _lock:
        _state.update(thread=None, last_started=None, feed=None, feed_source=None)
        _history.clear()


# ── summaries and comparison (pure functions) ───────────────────────────────

def summarise(source, observations, info, now):
    """Plain-data view of one source's poll: newest observation, age, errors, QC counts."""
    from .asos_nowcast import freshness_status
    info = info or {}
    newest = observations[-1] if observations else None
    newest_dt = asos_parser.parse_utc(newest["observed_at"]) if newest else None
    age = round(max(0.0, (now - newest_dt).total_seconds()), 1) if newest_dt else None
    return {
        "source": source,
        "newest_observation_time": newest["observed_at"] if newest else None,
        "temp_f": newest["temp_f"] if newest else None,
        "age_seconds": age,
        "status": freshness_status(age),
        "errors": list(info.get("errors") or []),
        "error_category": info.get("source_error_category"),
        "rejected_count": len(info.get("rejected") or []),
        "valid_count": len(observations),
        "obs": {o["observed_at"]: o["temp_f"] for o in observations[-COMPARE_LAST_N:]},
        "latency": info.get("latency"),
    }


def compare(primary, shadow):
    """Compare two summaries. Positive newest_time_diff_seconds = the shadow source has a newer observation."""
    p_t = asos_parser.parse_utc(primary["newest_observation_time"])
    s_t = asos_parser.parse_utc(shadow["newest_observation_time"])
    diff = round((s_t - p_t).total_seconds(), 1) if p_t and s_t else None
    common = sorted(set(primary["obs"]) & set(shadow["obs"]))
    temp_diffs = [abs(primary["obs"][k] - shadow["obs"][k]) for k in common
                  if primary["obs"][k] is not None and shadow["obs"][k] is not None]
    same_newest = p_t is not None and p_t == s_t
    return {
        "primary_source": primary["source"], "shadow_source": shadow["source"],
        "primary_newest": primary["newest_observation_time"], "shadow_newest": shadow["newest_observation_time"],
        "newest_time_diff_seconds": diff,
        "leader": None if diff is None else "shadow" if diff > 0 else "primary" if diff < 0 else "tie",
        "primary_age_seconds": primary["age_seconds"], "shadow_age_seconds": shadow["age_seconds"],
        "primary_status": primary["status"], "shadow_status": shadow["status"],
        "newest_temp_diff_f": (round(abs(primary["temp_f"] - shadow["temp_f"]), 2)
                               if same_newest and primary["temp_f"] is not None and shadow["temp_f"] is not None
                               else None),
        "common_observations": len(common),
        "max_temp_diff_f": round(max(temp_diffs), 2) if temp_diffs else None,
        "temp_mismatches": sum(1 for d in temp_diffs if d > TEMP_TOLERANCE_F),
        "primary_rejected": primary["rejected_count"], "shadow_rejected": shadow["rejected_count"],
        "primary_valid": primary["valid_count"], "shadow_valid": shadow["valid_count"],
        "primary_errors": primary["errors"], "shadow_errors": shadow["errors"],
        "primary_latency": primary.get("latency"), "shadow_latency": shadow.get("latency"),
    }


def summary():
    """Aggregate of the recorded comparisons (for the source-health endpoint)."""
    with _lock:
        records = list(_history)
    if not records:
        return {"comparisons": 0, "last": None}
    diffs = [r["newest_time_diff_seconds"] for r in records if r["newest_time_diff_seconds"] is not None]

    def med(key):
        vals = [r[key] for r in records if r[key] is not None]
        return round(statistics.median(vals), 1) if vals else None

    return {
        "comparisons": len(records),
        "shadow_newer": sum(1 for d in diffs if d > 0),
        "primary_newer": sum(1 for d in diffs if d < 0),
        "tie": sum(1 for d in diffs if d == 0),
        "median_primary_age_seconds": med("primary_age_seconds"),
        "median_shadow_age_seconds": med("shadow_age_seconds"),
        "median_newest_time_diff_seconds": round(statistics.median(diffs), 1) if diffs else None,
        "temp_mismatch_total": sum(r["temp_mismatches"] for r in records),
        "primary_error_polls": sum(1 for r in records if r["primary_errors"]),
        "shadow_error_polls": sum(1 for r in records if r["shadow_errors"]),
        "last": records[-1],
    }


# ── running a comparison ────────────────────────────────────────────────────

def _record(entry, env=None):
    with _lock:
        _history.append(entry)
    line = " ".join(f"{k}={entry[k]}" for k in (
        "primary_source", "shadow_source", "primary_newest", "shadow_newest", "newest_time_diff_seconds",
        "leader", "primary_age_seconds", "shadow_age_seconds", "newest_temp_diff_f", "temp_mismatches",
        "primary_rejected", "shadow_rejected"))
    errors = entry["primary_errors"] + entry["shadow_errors"]
    print(f"[ASOS-SHADOW] {line}" + (f" errors={len(errors)}" if errors else ""), flush=True)
    env = os.environ if env is None else env
    p = cfg.resolve_log_path(env.get("ASOS_SHADOW_LOG_PATH"))
    if p is not None:
        path = str(p)
        try:
            if not p.exists() or p.stat().st_size < MAX_LOG_BYTES:
                p.parent.mkdir(parents=True, exist_ok=True)
                with p.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps({"at": datetime.now(timezone.utc).isoformat(), **entry}) + "\n")
        except Exception as exc:
            print(f"[ASOS-SHADOW] could not write {path}: {type(exc).__name__}", flush=True)


def _shadow_feed(source):
    from .asos_client import AsosFeed
    with _lock:
        if _state["feed"] is None or _state["feed_source"] != source:
            _state["feed"] = AsosFeed(source=source, persist=False)
            _state["feed_source"] = source
        return _state["feed"]


def run_comparison(primary_summary, now=None, feed=None, env=None):
    """Poll the alternate source once and record the comparison. Returns the record. Never raises."""
    now = now or datetime.now(timezone.utc)
    alt = alternate_source(primary_summary["source"])
    feed = feed or _shadow_feed(alt)
    try:
        observations, info = feed.poll(now=now, recent_only=True)
        shadow = summarise(alt, observations, info, now)
    except Exception as exc:                               # a shadow failure is only ever a recorded error
        shadow = summarise(alt, [], {"errors": [f"shadow poll crashed: {type(exc).__name__}"]}, now)
    entry = compare(primary_summary, shadow)
    _record(entry, env)
    return entry


def _run_safe(primary_summary, now):
    try:
        run_comparison(primary_summary, now)
    except Exception as exc:
        print(f"[ASOS-SHADOW] comparison failed: {type(exc).__name__}", flush=True)


def notify(primary_source, observations, info, now=None, env=None):
    """
    Called after every primary poll. Returns immediately; never raises. Starts at most one background
    comparison per ASOS_SHADOW_MIN_INTERVAL_SECONDS.
    """
    try:
        if not enabled(env) or primary_source not in cfg.ASOS_SOURCES:
            return False
        now = now or datetime.now(timezone.utc)
        with _lock:
            thread = _state["thread"]
            last = _state["last_started"]
            if (thread is not None and thread.is_alive()) or (
                    last is not None and time.monotonic() - last < _min_interval(env)):
                return False
            _state["last_started"] = time.monotonic()
            primary_summary = summarise(primary_source, observations, info, now)
            thread = threading.Thread(target=_run_safe, args=(primary_summary, now), daemon=True,
                                      name="asos-shadow")
            _state["thread"] = thread
        thread.start()
        return True
    except Exception:
        return False
