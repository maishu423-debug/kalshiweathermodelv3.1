"""
NOAA MADIS OMO / HFMETAR client for KMIA (network + netCDF decoding + feed state).

Product : MADIS OMO, dataset family LDAD/hfmetar (high-frequency ASOS)
Access  : public hourly netCDF files
          https://madis-data.ncep.noaa.gov/madisPublic1/data/LDAD/hfmetar/netCDF/YYYYMMDD_HH00.gz
          (no credentials required for this path)

This is the ONLY live weather source.  There is deliberately no fallback to the
NWS station API, ordinary METAR, or any other provider.

Reliability rules (AsosFeed):
  * A failed poll is NOT stale weather.  The last valid KMIA observation is kept
    (in memory AND on disk) and its freshness is judged from its own observation
    time, never from whether the latest HTTP request succeeded.
  * The current and previous UTC hour files are re-checked on every poll (MADIS
    reprocesses both every ~5 minutes); older hours are cached once they are final.
  * A downloaded file only replaces/extends the cache after HTTP success,
    gzip decompression, netCDF decoding and record extraction all succeed.
    Cached records are merged by observation time, so they never shrink.

Two interchangeable transports feed the SAME decoder, parser and nowcast (ASOS_SOURCE):
  public  the NOAA files above (default).
  ldm     our private authenticated LDM API (ldm_client.py).  One cheap /files listing per poll says
          which hourly snapshots changed (size + modified time); only changed snapshots are downloaded.
          The snapshot's modified time is a cache key, never an observation time.
Both keep the rules above; neither ever falls back to the other for current data.
"""
import gzip
import io
import json
import logging
import os
import threading
import time
import zlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import requests

from . import asos_parser, config as cfg

log = logging.getLogger("weather.asos")

_FILL_ABS = 1e30
CACHE_KEEP_HOURS = 48
ARRIVAL_LOG_MAX_BYTES = 20 * 1024 * 1024
_arrival_lock = threading.Lock()
STATE_PATH = Path(os.getenv("ASOS_STATE_PATH")
                  or Path(__file__).resolve().parent.parent / "modelscripts" / "asos_last_good.json")


def _headers():
    return {"User-Agent": cfg.HTTP_USER_AGENT, "Cache-Control": "no-cache", "Pragma": "no-cache"}


def hours_needed(now=None, lookback_hours=cfg.ASOS_LOOKBACK_HOURS):
    """Hours back to cover both the nowcast lookback and today's Miami-local day."""
    now = now or datetime.now(timezone.utc)
    midnight = now.astimezone(ZoneInfo(cfg.LOCAL_TZ_NAME)).replace(
        hour=0, minute=0, second=0, microsecond=0)
    since_midnight = (now - midnight.astimezone(timezone.utc)).total_seconds() / 3600.0
    return max(float(lookback_hours), since_midnight) + 0.5


def hourly_files(now, hours):
    """Hourly file stamps (UTC hour starts) covering [now-hours, now], oldest first."""
    start = (now - timedelta(hours=hours)).replace(minute=0, second=0, microsecond=0)
    end = now.replace(minute=0, second=0, microsecond=0)
    out, t = [], start
    while t <= end:
        out.append(t)
        t += timedelta(hours=1)
    return out


def file_name(hour_start):
    return f"{hour_start:%Y%m%d_%H}00.gz"


# ── netCDF decoding ─────────────────────────────────────────────────────────

def _chars(arr):
    """char array (..., n) -> decoded stripped strings along the last axis."""
    a = np.ascontiguousarray(np.asarray(arr))
    n = a.shape[-1]
    flat = a.reshape(-1, n)
    out = [row.tobytes().replace(bytes(1), b"").decode("ascii", "ignore").strip() for row in flat]
    return np.array(out, dtype=object).reshape(a.shape[:-1])


def _val(x):
    if x is np.ma.masked:
        return None
    x = float(x)
    if x != x or abs(x) > _FILL_ABS or x == -9999.0:
        return None
    return x


def decode_hfmetar_ex(raw_gz, station):
    """gzip'd MADIS HFMETAR netCDF bytes -> (records for `station`, total records in file).

    Raises on a truncated/corrupt gzip or netCDF (callers must treat that as a failed download).
    """
    from scipy.io import netcdf_file          # classic netCDF3; no netCDF4/HDF5 dependency

    nc = netcdf_file(io.BytesIO(gzip.decompress(raw_gz)), mmap=False)
    v = nc.variables
    ids = _chars(v["stationId"][:])
    total = int(ids.shape[0])
    idx = np.where(ids == station)[0]
    if idx.size == 0:
        return [], total
    dd_vars = {"temperature": "temperatureDD", "dewpoint": "dewpointDD", "altimeter": "altimeterDD",
               "windDir": "windDirDD", "windSpeed": "windSpeedDD", "windGust": "windGustDD",
               "visibility": "visibilityDD"}
    dd = {name: _chars(v[var][:][idx][:, None]) for name, var in dd_vars.items() if var in v}
    sky = _chars(v["skyCvr"][:][idx]) if "skyCvr" in v else None
    wx = _chars(v["presWx"][:][idx]) if "presWx" in v else None

    def col(name):
        return v[name][:][idx] if name in v else None

    cols = {k: col(k) for k in ("observationTime", "temperature", "dewpoint", "windDir", "windSpeed",
                                "windGust", "altimeter", "visibility")}
    records = []
    for j in range(idx.size):
        t = _val(cols["observationTime"][j])
        if t is None:
            continue
        records.append({
            "station": station,
            "observation_time": t,
            "temperature": _val(cols["temperature"][j]),
            "dewpoint": _val(cols["dewpoint"][j]),
            "wind_dir": _val(cols["windDir"][j]),
            "wind_speed": _val(cols["windSpeed"][j]),
            "wind_gust": _val(cols["windGust"][j]) if cols["windGust"] is not None else None,
            "altimeter": _val(cols["altimeter"][j]),
            "visibility": _val(cols["visibility"][j]),
            "sky_cover": [c for c in (sky[j] if sky is not None else []) if c],
            "present_weather": (str(wx[j]) if wx is not None else "") or None,
            "dd": {name: str(arr[j]) for name, arr in dd.items()},
        })
    return records, total


def decode_hfmetar(raw_gz, station):
    return decode_hfmetar_ex(raw_gz, station)[0]


def _classify_decode_error(exc):
    """Why decode_hfmetar_ex failed: a broken gzip wrapper, or a payload that is not a readable netCDF."""
    return "bad_gzip" if isinstance(exc, (gzip.BadGzipFile, EOFError, zlib.error)) else "bad_netcdf"


# ── feed state ──────────────────────────────────────────────────────────────

class AsosFeed:
    """Stateful MADIS HFMETAR poller: per-file record cache + persisted last-good observation."""

    def __init__(self, state_path=None, source=None, ldm=None, persist=True, public_http=None):
        """
        source      'public' | 'ldm' | None (None = read ASOS_SOURCE at every poll)
        ldm         an LdmApi (tests); None = build one from the environment when needed
        persist     False = never read or write the last-good file (used by the shadow comparison)
        public_http object with .get() used for public NOAA files (default: requests)
        """
        self.lock = threading.RLock()
        self.state_path = Path(state_path) if state_path else STATE_PATH
        self.source = source
        self.persist = persist
        self._ldm = ldm
        self._ldm_cfg_key = None
        self.public_http = public_http
        self._ldm_auto = None
        self.latest_receipt = None   # when/how the current last-good observation first reached us
        self._advances = 0           # how many times last-good advanced in this process (arrival log: startup flag)
        self.cache = {}              # hour-file name -> {"last_modified", "records", "final", + origin/version/timing}
        self.last_good = None        # newest successfully decoded normalized KMIA observation
        self.last_poll = None        # info dict of the most recent poll
        self.failed_at = {}          # filename -> time of last failed fetch (backoff for old hours)
        self._last_fetch_at = None   # when NOAA was last actually contacted
        self._state_loaded = False

    # -- persistence --------------------------------------------------------
    def _load_state(self, now):
        if self._state_loaded:
            return
        self._state_loaded = True
        if not self.persist:
            return
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            obs = data.get("last_good")
            if not obs or obs.get("station") != cfg.ASOS_STATION:
                return
            age = (now - asos_parser.parse_utc(obs["observed_at"])).total_seconds()
            if age <= cfg.ASOS_MAX_CURRENT_AGE_MINUTES * 60:     # real age, timestamp untouched
                self.last_good = obs
                log.info("restored persisted last-good KMIA obs %s (age %.0fs)", obs["observed_at"], age)
            else:
                log.info("persisted last-good KMIA obs %s too old (%.0fs); ignored", obs["observed_at"], age)
        except FileNotFoundError:
            pass
        except Exception as exc:
            log.warning("could not read persisted ASOS state %s: %s", self.state_path, exc)

    def _save_state(self):
        if not self.persist:
            return
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"station": cfg.ASOS_STATION, "last_good": self.last_good}),
                           encoding="utf-8")
            os.replace(tmp, self.state_path)
        except Exception as exc:
            log.warning("could not persist ASOS state: %s", exc)

    # -- one file -----------------------------------------------------------
    def _fetch_file(self, name, hs, now, http):
        """Update the cache for one hourly file.  Returns a per-file result dict (never raises)."""
        res = {"file": name, "http_status": None, "bytes": 0, "decoded_records": None,
               "kmia_records": 0, "error": None}
        entry = self.cache.get(name)
        # current + previous hour are still being reprocessed; older hours only once final
        recheck = (now - hs) < timedelta(hours=cfg.MADIS_REFRESH_RECENT_HOURS) or not (entry and entry["final"])
        if entry and not recheck:
            res.update(http_status="cached", kmia_records=len(entry["records"]))
            return res
        is_recent = (now - hs) < timedelta(hours=cfg.MADIS_REFRESH_RECENT_HOURS)
        last_fail = self.failed_at.get(name)
        if not is_recent and last_fail and (now - last_fail) < timedelta(minutes=5):
            res.update(http_status="backoff", kmia_records=len(entry["records"]) if entry else 0)
            return res                           # old hour failed recently; do not hammer NOAA
        headers = _headers()
        if entry and entry.get("last_modified"):
            headers["If-Modified-Since"] = entry["last_modified"]
        try:
            t_download = time.perf_counter()
            resp = http.get(cfg.MADIS_HFMETAR_URL + name, headers=headers, timeout=cfg.HTTP_TIMEOUT_SEC)
            download_ms = round((time.perf_counter() - t_download) * 1000, 1)
            res["http_status"] = resp.status_code
            if resp.status_code == 304 and entry:
                res["kmia_records"] = len(entry["records"])
                return res
            if resp.status_code == 404:
                res["kmia_records"] = len(entry["records"]) if entry else 0
                return res                       # not published (yet): keep whatever is cached
            resp.raise_for_status()
            content = resp.content
            res["bytes"] = len(content)
            t_parse = time.perf_counter()
            records, total = decode_hfmetar_ex(content, cfg.ASOS_STATION)   # raises on corrupt gzip/netCDF
            parse_ms = round((time.perf_counter() - t_parse) * 1000, 1)
            res["decoded_records"] = total
            if total == 0:
                raise ValueError("file decoded but contains no records")
            merged = {r["observation_time"]: r for r in (entry["records"] if entry else [])}
            merged.update({r["observation_time"]: r for r in records})
            self.cache[name] = {
                "last_modified": resp.headers.get("Last-Modified"),
                "records": [merged[k] for k in sorted(merged)],
                "final": (now - hs) >= timedelta(hours=2),
                "origin": "public", "snapshot": name, "version": resp.headers.get("Last-Modified"),
                "received_at": now.isoformat(), "download_ms": download_ms, "parse_ms": parse_ms,
            }
            res["kmia_records"] = len(self.cache[name]["records"])
            res["origin"] = "public"
            self.failed_at.pop(name, None)
        except Exception as exc:
            self.failed_at[name] = now
            res["error"] = f"{type(exc).__name__}: {exc}"
            log.warning("MADIS HFMETAR %s failed (%s); keeping previous cache/last-good", name, res["error"])
            if entry:
                res["kmia_records"] = len(entry["records"])
        return res

    # -- source selection -----------------------------------------------------
    def _resolve_source(self):
        return self.source or cfg.get_asos_source()          # may raise AsosConfigError

    def _ldm_api(self, session=None):
        """LdmApi for the current environment; rebuilt only when the LDM settings change (keeps the TLS session)."""
        if self._ldm is not None:
            return self._ldm
        from . import ldm_client
        conf = ldm_client.load_ldm_config()                  # may raise AsosConfigError — before any request
        key = (conf.base_url, conf.token, conf.timeout, conf.max_retries, conf.backfill_public)
        if self._ldm_cfg_key != key:
            self._ldm_auto = ldm_client.LdmApi(conf, session=session)
            self._ldm_cfg_key = key
        return self._ldm_auto

    # -- one hourly snapshot from the LDM API ---------------------------------
    def _fetch_file_ldm(self, api, name, hs, meta, now, deadline):
        """Update the cache for one hour from the LDM API. Returns a per-file result dict (never raises)."""
        from . import ldm_client
        res = {"file": name, "http_status": None, "bytes": 0, "decoded_records": None, "kmia_records": 0,
               "error": None, "origin": "ldm", "snapshot": meta.name, "version": meta.version}
        entry = self.cache.get(name)
        # The same filename is republished as NOAA updates it, so the filename alone proves nothing:
        # the snapshot is reused only while its version (size + modified time) is exactly what we parsed.
        if (entry and entry.get("origin") == "ldm" and meta.version is not None
                and entry.get("version") == meta.version):
            res.update(http_status="cached", kmia_records=len(entry["records"]))
            return res
        is_recent = (now - hs) < timedelta(hours=cfg.MADIS_REFRESH_RECENT_HOURS)
        last_fail = self.failed_at.get(name)
        if not is_recent and last_fail and (now - last_fail) < timedelta(minutes=5):
            res.update(http_status="backoff", kmia_records=len(entry["records"]) if entry else 0)
            return res
        try:
            content, download_ms, digest = api.download(meta, deadline)
            res.update(http_status=200, bytes=len(content))
            t_parse = time.perf_counter()
            try:
                records, total = decode_hfmetar_ex(content, cfg.ASOS_STATION)   # the existing decoder, unchanged
            except Exception as exc:
                raise ldm_client.LdmError(
                    _classify_decode_error(exc), f"{meta.name}: {type(exc).__name__}: {exc}"[:200]) from None
            parse_ms = round((time.perf_counter() - t_parse) * 1000, 1)
            res["decoded_records"] = total
            if total == 0:
                raise ldm_client.LdmError("empty_dataset", f"{meta.name}: decoded but contains no records")
            if not records:
                res["note"] = "no_station_records"
                log.warning("LDM %s decoded %d records but none for %s", meta.name, total, cfg.ASOS_STATION)
            merged = {r["observation_time"]: r for r in (entry["records"] if entry else [])}
            merged.update({r["observation_time"]: r for r in records})   # a corrected record replaces the old one
            self.cache[name] = {
                "last_modified": None,
                "records": [merged[k] for k in sorted(merged)],
                "final": (now - hs) >= timedelta(hours=2),
                "origin": "ldm", "snapshot": meta.name, "version": meta.version, "sha256": digest,
                "received_at": now.isoformat(), "download_ms": download_ms, "parse_ms": parse_ms,
            }
            res["kmia_records"] = len(self.cache[name]["records"])
            self.failed_at.pop(name, None)
        except ldm_client.LdmError as exc:
            self.failed_at[name] = now
            res["error"] = f"[{exc.category}] {exc}"
            res["error_category"] = exc.category
            log.warning("LDM %s failed (%s); keeping previous cache/last-good", name, res["error"])
            if entry:
                res["kmia_records"] = len(entry["records"])
        except Exception as exc:
            self.failed_at[name] = now
            res["error"] = f"{type(exc).__name__}: {exc}"
            log.warning("LDM %s failed (%s); keeping previous cache/last-good", name, res["error"])
            if entry:
                res["kmia_records"] = len(entry["records"])
        return res

    def _fetch_hours_ldm(self, hour_starts, must, now, info, session):
        """One /files listing, then download only the snapshots whose version changed."""
        from . import ldm_client
        try:
            api = self._ldm_api(session)
        except cfg.AsosConfigError as exc:
            info["errors"].append(f"configuration: {exc}")
            info["config_error"] = True
            info["source_error_category"] = "config"
            return
        deadline = ldm_client.Deadline()
        try:
            listing, info["ldm_listing_ms"] = api.list_files(deadline)
        except ldm_client.LdmError as exc:
            info["errors"].append(f"LDM listing: [{exc.category}] {exc}")
            info["source_error_category"] = exc.category
            log.warning("LDM listing failed (%s); keeping cache/last-good", exc.category)
            return
        info["ldm_files_listed"] = len(listing)
        missing, backfilled = [], []
        for hs in hour_starts:
            name = file_name(hs)
            info["requested_files"].append(name)
            meta = listing.get(hs)
            if meta is not None:
                res = self._fetch_file_ldm(api, name, hs, meta, now, deadline)
            elif hs not in must and api.config.backfill_public:
                # An old hour the LDM API no longer holds: needed so the day's recorded high is complete.
                res = self._fetch_file(name, hs, now, self.public_http or requests)
                backfilled.append(name)
                if res.get("http_status") == 404 and name not in self.cache:
                    self.failed_at[name] = now          # not on NOAA either: back off instead of asking every poll
            else:
                # Not published (yet) / not retained: not an error, keep whatever is cached. Never substituted
                # for the current or previous hour.
                entry = self.cache.get(name)
                res = {"file": name, "http_status": 404, "bytes": 0, "decoded_records": None,
                       "kmia_records": len(entry["records"]) if entry else 0, "error": None,
                       "origin": "ldm", "note": "not_in_ldm_listing"}
                missing.append(name)
            info["file_results"].append(res)
            if res["error"]:
                info["errors"].append(f"{res.get('origin', 'LDM')} {name}: {res['error']}")
            elif res["http_status"] in (200, 304, "cached"):
                info["files"].append(name)
        info["ldm_missing_hours"] = missing
        info["backfilled_from_public"] = backfilled

    def _prune_cache(self, now):
        """Bound the cache: hours older than CACHE_KEEP_HOURS can never matter to the nowcast or the daily high."""
        from .ldm_client import hour_from_name
        limit = now - timedelta(hours=CACHE_KEEP_HOURS)
        for name in list(self.cache):
            hour = hour_from_name(name)
            if hour is not None and hour < limit:
                del self.cache[name]
                self.failed_at.pop(name, None)

    # -- poll ---------------------------------------------------------------
    def poll(self, lookback_hours=cfg.ASOS_LOOKBACK_HOURS, now=None, session=None, reuse_seconds=0,
             recent_only=False):
        """
        Returns (observations, info).  observations: QC'd, de-duplicated, ascending UTC KMIA
        public-5-minute observations built from the cached records plus last-good; never emptier
        than before because a download failed.

        reuse_seconds > 0: if NOAA was already contacted that recently, do not contact it again;
        answer from the cache (keeps secondary callers from polling NOAA aggressively).
        recent_only: only the current and previous UTC hour (used by the shadow comparison).
        session: HTTP session for the configured source (tests; default requests / our own session).
        """
        now = now or datetime.now(timezone.utc)
        with self.lock:
            self._load_state(now)
            if (reuse_seconds and self._last_fetch_at and self.last_poll is not None
                    and (now - self._last_fetch_at).total_seconds() < reuse_seconds):
                info = {**self.last_poll, "reused_last_poll": True}
                return self._assemble(now, info, lookback_hours), info

            hours = hours_needed(now, lookback_hours)
            info = {"poll_started_at": now.isoformat(), "hours": hours, "sources": [], "errors": [],
                    "rejected": [], "files": [], "file_results": [], "requested_files": [],
                    "records_received": 0, "fetched_at": now.isoformat(), "reused_last_poll": False}
            cur_hour = now.replace(minute=0, second=0, microsecond=0)
            must = {cur_hour, cur_hour - timedelta(hours=1)}           # current + previous UTC hour, always
            info["current_file"] = file_name(cur_hour)
            info["previous_file"] = file_name(cur_hour - timedelta(hours=1))
            hour_starts = sorted(must) if recent_only else sorted(set(hourly_files(now, hours)) | must)
            try:
                source = self._resolve_source()
            except cfg.AsosConfigError as exc:
                source = None
                info["errors"].append(f"configuration: {exc}")      # nothing is requested
                info["config_error"] = True
                info["source_error_category"] = "config"
            info["source"] = source
            if source == cfg.ASOS_SOURCE_LDM:
                self._fetch_hours_ldm(hour_starts, must, now, info, session)
            elif source == cfg.ASOS_SOURCE_PUBLIC:
                http = session or self.public_http or requests
                for hs in hour_starts:
                    name = file_name(hs)
                    res = self._fetch_file(name, hs, now, http)
                    info["requested_files"].append(name)
                    info["file_results"].append(res)
                    if res["error"]:
                        info["errors"].append(f"MADIS {name}: {res['error']}")
                    elif res["http_status"] in (200, 304, "cached"):
                        info["files"].append(name)
            # Hours of the day we should hold but do not: a partial day would understate the recorded high.
            if not recent_only:
                info["coverage_missing_hours"] = [file_name(hs) for hs in hour_starts
                                                  if file_name(hs) not in self.cache]
            self._prune_cache(now)
            self._last_fetch_at = now
            info["last_poll_success"] = not info["errors"]
            observations = self._assemble(now, info, lookback_hours)
            info["fetch_error"] = "; ".join(info["errors"]) or None
            info["source_status"] = ("fetch_error_using_last_good" if info["errors"] and self.last_good
                                     else "fetch_error_no_observation" if info["errors"] else "ok")
            self.last_poll = info
            return observations, info

    def _assemble(self, now, info, lookback_hours):
        """Cache + last-good -> observations; advance last-good only to a strictly newer observation."""
        records = [r for e in self.cache.values() for r in e["records"]]
        info["records_received"] = len(records)
        rejected = info.setdefault("rejected", [])
        observations = asos_parser.parse_madis_records(records, station=cfg.ASOS_STATION, rejected=rejected)
        horizon = now + timedelta(minutes=5)
        observations = [o for o in observations if asos_parser.parse_utc(o["observed_at"]) <= horizon]
        info["kmia_found"] = len(observations)
        newest_downloaded = observations[-1] if observations else None
        info["newest_downloaded_observation"] = newest_downloaded["observed_at"] if newest_downloaded else None
        if records:
            info.setdefault("sources", [])
            if "MADIS_HFMETAR" not in info["sources"]:
                info["sources"].append("MADIS_HFMETAR")

        info["cache_updated"] = False
        advance, corrected = False, False
        if newest_downloaded is not None:
            if self.last_good is None:
                advance = True
            else:
                new_t = asos_parser.parse_utc(newest_downloaded["observed_at"])
                old_t = asos_parser.parse_utc(self.last_good["observed_at"])
                if new_t > old_t:
                    advance = True                      # a strictly newer observation
                elif (new_t == old_t and newest_downloaded != self.last_good
                      and asos_parser._completeness(newest_downloaded) >= asos_parser._completeness(self.last_good)):
                    advance, corrected = True, True     # NOAA re-issued the same observation with changed values
        if advance:
            # Every observation that is new to this feed (a poll can bring several), for the arrival log.
            prev_t = asos_parser.parse_utc(self.last_good["observed_at"]) if self.last_good else None
            fresh = [newest_downloaded] if corrected else [
                o for o in observations if prev_t is None or asos_parser.parse_utc(o["observed_at"]) > prev_t]
            self.last_good = newest_downloaded
            self._save_state()
            info["cache_updated"] = True
            info["last_good_corrected"] = corrected
            self._record_receipt(newest_downloaded, now, info)
            self._log_arrivals(fresh, now, info, corrected)
        if self.last_good is not None:
            observations = asos_parser.deduplicate(observations + [self.last_good])
        info["last_good_observation"] = self.last_good["observed_at"] if self.last_good else None
        info["last_good_age_seconds"] = (
            round(max(0.0, (now - asos_parser.parse_utc(self.last_good["observed_at"])).total_seconds()), 1)
            if self.last_good else None)
        info["latency"] = self._latency_report(now)
        return observations

    # -- latency diagnostics --------------------------------------------------
    def _entry_containing(self, obs_time):
        """The cached snapshot that carries the observation at `obs_time` (the most recently received one)."""
        entry = None
        for e in self.cache.values():
            if any(abs(r["observation_time"] - obs_time.timestamp()) < 1 for r in e["records"]):
                if entry is None or (e.get("received_at") or "") >= (entry.get("received_at") or ""):
                    entry = e
        return entry or {}

    def _log_arrivals(self, fresh, now, info, corrected):
        """
        Append one JSON line per newly delivered observation to ASOS_ARRIVAL_LOG_PATH (if set): when it reached this
        feed, which snapshot carried it, how old it was. Used by tools/ldm_vs_public_report.py.
        `startup` marks the first batch after the process started: those observations may have been available
        long before we looked, so they are excluded from latency statistics.
        """
        startup = self._advances == 0
        self._advances += 1
        path = cfg.resolve_log_path(os.getenv("ASOS_ARRIVAL_LOG_PATH"))
        if path is None or not fresh:
            return
        rows = []
        for o in fresh:
            obs_time = asos_parser.parse_utc(o["observed_at"])
            entry = self._entry_containing(obs_time)
            rows.append({
                "logged_at": now.isoformat(),
                "source": info.get("source") or entry.get("origin"),
                "role": "primary" if self.persist else "shadow",
                "observation_time_utc": o["observed_at"],
                "backend_received_at_utc": now.isoformat(),
                "age_at_receipt_seconds": round(max(0.0, (now - obs_time).total_seconds()), 1),
                "temp_f": o.get("temp_f"),
                "snapshot_filename": entry.get("snapshot"),
                "snapshot_version": entry.get("version"),
                "download_ms": entry.get("download_ms"),
                "parse_ms": entry.get("parse_ms"),
                "startup": startup,
                "corrected": corrected,
            })
        try:
            with _arrival_lock:
                if path.exists() and path.stat().st_size > ARRIVAL_LOG_MAX_BYTES:
                    return
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("a", encoding="utf-8") as fh:
                    fh.write("".join(json.dumps(r) + "\n" for r in rows))
        except Exception as exc:
            log.warning("could not write arrival log %s: %s", path, type(exc).__name__)

    def _record_receipt(self, obs, now, info):
        """Remember when/how the newest observation first reached us, and log it (never any credentials)."""
        obs_time = asos_parser.parse_utc(obs["observed_at"])
        entry = self._entry_containing(obs_time)
        self.latest_receipt = {
            "source_name": info.get("source") or entry.get("origin"),
            "station_id": cfg.ASOS_STATION,
            "observation_time_utc": obs["observed_at"],
            "backend_received_at_utc": now.isoformat(),
            "observation_age_at_receipt_seconds": round(max(0.0, (now - obs_time).total_seconds()), 1),
            "snapshot_filename": entry.get("snapshot"),
            "snapshot_version": entry.get("version"),
            "download_duration_ms": entry.get("download_ms"),
            "parse_duration_ms": entry.get("parse_ms"),
        }
        if self.persist:
            line = " ".join(f"{k}={v}" for k, v in {
                **self.latest_receipt,
                "observation_age_seconds": self.latest_receipt["observation_age_at_receipt_seconds"],
                "latest_valid_observation_time": obs["observed_at"],
                "corrected": info.get("last_good_corrected", False)}.items())
            print(f"[ASOS-LATENCY] {line}", flush=True)

    def _latency_report(self, now):
        """Current observation age plus how the newest observation arrived."""
        if self.last_good is None:
            return None
        obs_time = asos_parser.parse_utc(self.last_good["observed_at"])
        return {**(self.latest_receipt or {}),
                "observation_age_seconds": round(max(0.0, (now - obs_time).total_seconds()), 1),
                "latest_valid_observation_time": self.last_good["observed_at"]}

    def reset(self, state_path=None):
        with self.lock:
            self.cache.clear()
            self.last_good = None
            self.last_poll = None
            self.failed_at.clear()
            self._last_fetch_at = None
            self.latest_receipt = None
            self._advances = 0
            self._state_loaded = False
            if state_path is not None:
                self.state_path = Path(state_path)


_feed = AsosFeed()
_cache = _feed.cache        # kept for tests / introspection


def get_feed():
    return _feed


def fetch_recent_observations(lookback_hours=cfg.ASOS_LOOKBACK_HOURS, now=None, session=None, reuse_seconds=0):
    observations, info = _feed.poll(lookback_hours=lookback_hours, now=now, session=session,
                                    reuse_seconds=reuse_seconds)
    # Optional shadow comparison (ASOS_SHADOW_COMPARE): observes only, on its own thread, never raises.
    try:
        from . import asos_shadow
        asos_shadow.notify(info.get("source"), observations, info, now)
    except Exception:
        pass
    return observations, info


def get_latest_asos_observation(station_id=None, now=None, session=None, poll=True, reuse_seconds=0):
    """
    Public interface: the latest valid public 5-minute MADIS HFMETAR observation, or None if none has
    ever been obtained.  Callers need not know about gzip/netCDF/filenames.

    Never returns None merely because a download failed: the last good observation is kept, and
    `is_stale` says whether it is older than MADIS_STALE_MINUTES.
    """
    station_id = (station_id or cfg.ASOS_STATION).upper()
    if station_id != cfg.ASOS_STATION:
        raise ValueError(f"only the configured station {cfg.ASOS_STATION} is supported, got {station_id}")
    now = now or datetime.now(timezone.utc)
    if poll:
        _feed.poll(now=now, session=session, reuse_seconds=reuse_seconds)
    with _feed.lock:
        obs = _feed.last_good
        fetched = _feed._last_fetch_at
    if obs is None:
        return None
    obs_time = asos_parser.parse_utc(obs["observed_at"])
    age = max(0.0, (now - obs_time).total_seconds())
    return {
        "station": cfg.ASOS_STATION,
        "temperature_f": obs["temp_f"],
        "temperature_k": round(obs["temp_c"] + 273.15, 2),
        "observation_time_utc": obs_time,
        "fetched_at_utc": fetched or now,
        "age_seconds": round(age, 1),
        "source": cfg.SOURCE_LABEL_SHORT,
        "is_stale": age > cfg.ASOS_FRESH_MINUTES * 60,
    }


# ── diagnostics ─────────────────────────────────────────────────────────────

def cadence_distribution(observations):
    """Share of consecutive-observation gaps that are 1, 5, 10, 60 minutes or other."""
    ts = sorted(asos_parser.parse_utc(o["observed_at"]) for o in observations)
    gaps = [round((b - a).total_seconds() / 60.0, 2) for a, b in zip(ts, ts[1:])]
    n = len(gaps)
    buckets = {"1min": 0, "5min": 0, "10min": 0, "60min": 0, "other": 0}
    for g in gaps:
        key = {1: "1min", 5: "5min", 10: "10min", 60: "60min"}.get(round(g))
        if key is None or abs(g - round(g)) > 0.1:
            key = "other"
        buckets[key] += 1
    return {"intervals": n, "counts": buckets,
            "percent": {k: (round(100.0 * c / n, 1) if n else None) for k, c in buckets.items()}}


def diagnostics(now=None, observations=None, info=None, n_recent=10):
    from .asos_nowcast import freshness_status
    now = now or datetime.now(timezone.utc)
    if observations is None:
        observations, info = fetch_recent_observations(now=now, reuse_seconds=30)
    info = info or {}
    latest = observations[-1] if observations else None
    latest_dt = asos_parser.parse_utc(latest["observed_at"]) if latest else None
    age = round((now - latest_dt).total_seconds(), 1) if latest_dt else None
    return {
        "station": cfg.ASOS_STATION,
        "source": cfg.SOURCE_ID,
        "transport": info.get("source"),              # 'public' | 'ldm' (None = configuration error)
        "latency": info.get("latency"),
        "source_error_category": info.get("source_error_category"),
        "coverage_missing_hours": info.get("coverage_missing_hours", []),
        "backfilled_from_public": info.get("backfilled_from_public", []),
        "server_checked_at": now.isoformat(),
        "last_poll_at": info.get("poll_started_at", now.isoformat()),
        "last_poll_success": info.get("last_poll_success", not info.get("errors")),
        "last_poll_error": info.get("fetch_error") or (
            "; ".join(info["errors"]) if info.get("errors") else None),
        "source_status": info.get("source_status"),
        "requested_files": info.get("requested_files", []),
        "file_results": info.get("file_results", []),
        "kmia_records_found": info.get("records_received", len(observations)),
        "newest_downloaded_observation": info.get("newest_downloaded_observation"),
        "last_good_observation": ({"observed_at": latest["observed_at"], "temp_f": latest["temp_f"]}
                                  if latest else None),
        "latest_observed_at": latest["observed_at"] if latest else None,
        "observation_age_seconds": age,
        "freshness_status": freshness_status(age),
        "temperature_f": latest["temp_f"] if latest else None,
        "recent_observation_times": [o["observed_at"] for o in observations[-n_recent:]],
        "observations_received": len(observations),
        "cadence": cadence_distribution(observations),
        "errors": info.get("errors", []),
    }


def log_refresh(observations, info, now, debug=False):
    """[MADIS] diagnostic lines for every poll, including exactly why current would be None."""
    info = info or {}
    d = diagnostics(now, observations, info)
    P = "[MADIS]"
    lines = [f"station={d['station']}"]
    if info.get("reused_last_poll"):
        lines.append("reusing the last NOAA poll (queried recently)")
    lines += [f"checking current file={info.get('current_file')}",
              f"checking previous file={info.get('previous_file')}"]
    for r in info.get("file_results", []):
        if r["file"] in (info.get("current_file"), info.get("previous_file")):
            extra = f" error={r['error']}" if r["error"] else ""
            lines.append(f"{r['file']}: http={r['http_status']} bytes={r['bytes']} "
                         f"decoded_records={r['decoded_records']} kmia_records={r['kmia_records']}{extra}")
    for e in info.get("errors", []):
        lines.append(f"download failed: {e}")
    lines.append(f"found {info.get('kmia_found', len(observations))} {d['station']} public 5-minute observations")
    lg = info.get("last_good_observation")
    if lg is not None and observations:
        newest = observations[-1]
        if info.get("cache_updated"):
            lines += [f"latest valid 5m observation={newest['observed_at']}",
                      f"temperature={newest['temp_f']}F",
                      f"observation age={d['observation_age_seconds']}s",
                      "cache updated"]
        else:
            lines += [f"latest NOAA observation still {asos_parser.parse_utc(lg).strftime('%H:%MZ')}",
                      "cached observation retained"]
            if info.get("errors"):
                lines.append(f"retaining cached observation {asos_parser.parse_utc(lg).strftime('%H:%MZ')} / "
                             f"{newest['temp_f']}F (age {d['observation_age_seconds']}s)")
    lines.append(f"freshness={d['freshness_status']} source_status={info.get('source_status')}")
    if d["freshness_status"] == "unavailable":
        if not observations:
            lines.append("current=None because: no KMIA observation has ever been decoded and no usable "
                         "persisted last-good exists")
        else:
            lines.append(f"models paused: last good observation {d['latest_observed_at']} is "
                         f"{d['observation_age_seconds']}s old (> {cfg.ASOS_MAX_CURRENT_AGE_MINUTES:g} min); "
                         "temperature kept for display, marked stale")
    if debug:
        lines.append("last_10_observation_times=" + ",".join(
            asos_parser.parse_utc(o["observed_at"]).strftime("%H:%M") for o in observations[-10:]))
    for line in lines:
        print(f"{P} {line}", flush=True)
