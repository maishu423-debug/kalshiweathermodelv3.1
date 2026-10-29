"""
Private LDM API as the live HFMETAR source (ASOS_SOURCE=ldm) next to the public NOAA source.

No network: the LDM API and NOAA are replaced by in-memory fakes serving real (small) netCDF files, so the
EXISTING decoder, parser, QC and nowcast are what run on them.
"""
import gzip
import json
import os
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import requests

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
os.environ["DATABASE_PATH"] = ":memory:"
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import hfmetar_fixture as fx  # noqa: E402
from weather import asos_client, asos_nowcast, asos_shadow, config as wcfg, ldm_client  # noqa: E402

UTC = timezone.utc
TOKEN = "s3cr3t-test-token-9f2a"
NOW = datetime(2026, 10, 29, 9, 32, tzinfo=UTC)          # 05:32 in Miami (EDT)


# ── fakes ────────────────────────────────────────────────────────────────────

_NO_JSON = object()


class FakeResponse:
    def __init__(self, status=200, content=b"", json_data=_NO_JSON, headers=None):
        self.status_code, self.content, self._json, self.headers = status, content, json_data, headers or {}

    def json(self):
        if self._json is _NO_JSON:
            raise ValueError("not json")
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


def hour_of(when):
    return when.replace(minute=0, second=0, microsecond=0)


def ldm_name(hour):
    return f"hfmetar.{hour:%Y%m%d_%H}00.gz"


def obs(hour, minutes, base=84.0, step=0.1):
    return [fx.row(hour + timedelta(minutes=m), round(base + i * step, 1)) for i, m in enumerate(minutes)]


class FakeLdm:
    """Stands in for requests.Session talking to the LDM API."""

    def __init__(self):
        self.files = {}          # name -> {"content", "modified_at"}
        self.log = []            # (path, Authorization header)
        self.script = {}         # path -> FIFO of responses / exceptions served before the normal behaviour

    def publish(self, hour, rows_or_bytes, modified_at="2026-10-29T09:30:00+00:00"):
        content = rows_or_bytes if isinstance(rows_or_bytes, bytes) else fx.build_gz(rows_or_bytes)
        self.files[ldm_name(hour)] = {"content": content, "modified_at": modified_at}

    def downloads(self):
        return [p.rsplit("/", 1)[1] for p, _ in self.log if p.startswith("/api/v1/hfmetar/hfmetar.")]

    def listings(self):
        return [p for p, _ in self.log if p == "/api/v1/hfmetar/files"]

    def get(self, url, headers=None, timeout=None):
        from urllib.parse import urlsplit
        path = urlsplit(url).path
        auth = (headers or {}).get("Authorization")
        self.log.append((path, auth))
        queued = self.script.get(path)
        if queued:
            item = queued.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        if path == "/health":
            return FakeResponse(200, json_data={"status": "ok"})
        if auth != f"Bearer {TOKEN}":
            return FakeResponse(401)
        if path == "/api/v1/hfmetar/files":
            return FakeResponse(200, json_data={"files": [
                {"name": n, "size_bytes": len(f["content"]), "modified_at": f["modified_at"]}
                for n, f in sorted(self.files.items())]})
        name = path.rsplit("/", 1)[1]
        if path.startswith("/api/v1/hfmetar/") and name in self.files:
            return FakeResponse(200, self.files[name]["content"])
        return FakeResponse(404)


class FakePublic:
    """Stands in for requests talking to NOAA's public MADIS files."""

    def __init__(self):
        self.files = {}          # "YYYYMMDD_HH00.gz" -> bytes
        self.urls = []

    def publish(self, hour, rows):
        self.files[f"{hour:%Y%m%d_%H}00.gz"] = fx.build_gz(rows)

    def get(self, url, headers=None, timeout=None):
        self.urls.append(url)
        name = url.rsplit("/", 1)[1]
        if name not in self.files:
            return FakeResponse(404)
        if (headers or {}).get("If-Modified-Since") == "stamp":
            return FakeResponse(304)
        return FakeResponse(200, self.files[name], headers={"Last-Modified": "stamp"})


def make_api(server, retries=0, backfill=False, sleeps=None):
    conf = ldm_client.LdmConfig(base_url="https://data.getedgefront.com", token=TOKEN, timeout=5.0,
                                max_retries=retries, backfill_public=backfill)
    return ldm_client.LdmApi(conf, session=server, sleep=(sleeps.append if sleeps is not None else lambda s: None))


def ldm_feed(server, tmp_path, **kw):
    public_http = kw.pop("public_http", None)
    persist = kw.pop("persist", True)
    return asos_client.AsosFeed(state_path=tmp_path / "last_good.json", source="ldm", ldm=make_api(server, **kw),
                                persist=persist, public_http=public_http)


def public_feed(public, tmp_path, persist=True):
    return asos_client.AsosFeed(state_path=tmp_path / "last_good_public.json", source="public",
                                public_http=public, persist=persist)


def newest(observations):
    return observations[-1]["observed_at"]


H09 = hour_of(NOW)
H08 = H09 - timedelta(hours=1)


@pytest.fixture(autouse=True)
def isolate(monkeypatch):
    for name in ("ASOS_SOURCE", "LDM_API_TOKEN", "LDM_API_BASE_URL", "ASOS_SHADOW_COMPARE", "ASOS_SHADOW_LOG_PATH",
                 "ASOS_SHADOW_MIN_INTERVAL_SECONDS", "LDM_BACKFILL_PUBLIC", "LDM_API_TIMEOUT_SECONDS",
                 "LDM_API_MAX_RETRIES"):
        monkeypatch.delenv(name, raising=False)
    asos_shadow.reset()
    yield
    asos_shadow.reset()


# ── 1. valid authenticated response ──────────────────────────────────────────

def test_valid_authenticated_poll_feeds_the_existing_pipeline(tmp_path):
    server = FakeLdm()
    server.publish(H09, obs(H09, [0, 5, 10, 15, 20, 25, 30], base=84.0))
    feed = ldm_feed(server, tmp_path)
    observations, info = feed.poll(now=NOW)
    assert info["source"] == "ldm" and info["errors"] == [] and info["source_status"] == "ok"
    assert newest(observations) == "2026-10-29T09:30:00+00:00"
    assert observations[-1]["temp_f"] == pytest.approx(84.6)          # fixture value after the existing QC/units
    assert server.log and all(a == f"Bearer {TOKEN}" for _, a in server.log)
    lat = info["latency"]
    assert lat["source_name"] == "ldm" and lat["station_id"] == "KMIA"
    assert lat["snapshot_filename"] == ldm_name(H09) and lat["snapshot_version"]
    assert lat["download_duration_ms"] is not None and lat["parse_duration_ms"] is not None
    assert lat["observation_age_seconds"] == pytest.approx(120.0)      # 09:30 -> 09:32, measured from the observation


# ── 2. token problems ────────────────────────────────────────────────────────

def test_missing_token_is_a_clear_configuration_error(monkeypatch):
    monkeypatch.setenv("ASOS_SOURCE", "ldm")
    problems = ldm_client.validate_source_config()
    assert len(problems) == 1 and "LDM_API_TOKEN" in problems[0] and "ASOS_SOURCE=public" in problems[0]
    with pytest.raises(wcfg.AsosConfigError):
        ldm_client.load_ldm_config()


def test_missing_token_never_sends_an_unauthenticated_request(monkeypatch, tmp_path):
    monkeypatch.setenv("ASOS_SOURCE", "ldm")
    server = FakeLdm()
    server.publish(H09, obs(H09, [0, 5]))
    feed = asos_client.AsosFeed(state_path=tmp_path / "lg.json")               # source read from the environment
    observations, info = feed.poll(now=NOW, session=server)
    assert server.log == []                                                    # not even a listing request
    assert info["config_error"] and info["source_error_category"] == "config"
    assert "LDM_API_TOKEN" in info["errors"][0] and observations == []


def test_invalid_source_value_is_rejected(monkeypatch):
    monkeypatch.setenv("ASOS_SOURCE", "ldn")
    assert "not valid" in ldm_client.validate_source_config()[0]


@pytest.mark.parametrize("env, fragment", [
    ({"LDM_API_BASE_URL": "http://data.example.com"}, "https"),
    ({"LDM_API_BASE_URL": "https://user:pw@data.example.com"}, "plain base URL"),
    ({"LDM_API_TIMEOUT_SECONDS": "abc"}, "number of seconds"),
    ({"LDM_API_TIMEOUT_SECONDS": "0"}, "between 0 and 120"),
    ({"LDM_API_MAX_RETRIES": "9"}, "between 0 and 5"),
])
def test_bad_ldm_settings_are_rejected(env, fragment):
    with pytest.raises(wcfg.AsosConfigError, match=fragment):
        ldm_client.load_ldm_config({"LDM_API_TOKEN": TOKEN, **env})


def test_wrong_token_is_reported_as_auth_and_keeps_the_last_good_observation(tmp_path, capsys):
    server = FakeLdm()
    server.publish(H09, obs(H09, [0, 5, 10]))
    feed = ldm_feed(server, tmp_path)
    feed.poll(now=NOW)
    good = feed.last_good
    server.script["/api/v1/hfmetar/files"] = [FakeResponse(401)]
    observations, info = feed.poll(now=NOW + timedelta(minutes=1))
    assert info["source_error_category"] == "auth" and "LDM_API_TOKEN" in info["errors"][0]
    assert info["source_status"] == "fetch_error_using_last_good"
    assert feed.last_good == good and newest(observations) == good["observed_at"]
    assert not any("hfmetar.2026" in p for p, _ in server.log[-1:])           # no data request after the 401
    assert TOKEN not in json.dumps(info, default=str) and TOKEN not in capsys.readouterr().out


def test_token_never_appears_in_config_repr_or_error_text():
    conf = ldm_client.load_ldm_config({"LDM_API_TOKEN": TOKEN})
    assert TOKEN not in repr(conf) and TOKEN not in str(conf)
    api = ldm_client.LdmApi(conf, session=FakeLdm())
    assert TOKEN not in api._redact(f"oops Bearer {TOKEN} and {TOKEN}")


# ── 3/4. current + previous hour ─────────────────────────────────────────────

def test_current_and_previous_hour_files_are_both_retrieved_and_combined(tmp_path):
    server = FakeLdm()
    server.publish(H08, obs(H08, [45, 50, 55], base=82.0))
    server.publish(H09, obs(H09, [0, 5, 10], base=83.0))
    feed = ldm_feed(server, tmp_path)
    observations, info = feed.poll(now=NOW)
    assert sorted(server.downloads()) == [ldm_name(H08), ldm_name(H09)]
    times = [o["observed_at"][11:16] for o in observations]
    assert times == ["08:45", "08:50", "08:55", "09:00", "09:05", "09:10"]
    assert info["current_file"].endswith("20261029_0900.gz") and info["previous_file"].endswith("20261029_0800.gz")


def test_previous_hour_only_when_the_current_hour_is_not_published_yet(tmp_path):
    now = datetime(2026, 10, 29, 10, 1, tzinfo=UTC)
    server = FakeLdm()
    server.publish(H09, obs(H09, [45, 50, 55], base=84.0))
    feed = ldm_feed(server, tmp_path)
    observations, info = feed.poll(now=now)
    assert info["errors"] == [] and newest(observations) == "2026-10-29T09:55:00+00:00"   # missing hour is not an error
    server.publish(H09 + timedelta(hours=1), obs(H09 + timedelta(hours=1), [0, 5], base=85.0))
    observations, _ = feed.poll(now=now + timedelta(minutes=2))
    assert newest(observations) == "2026-10-29T10:05:00+00:00"


# ── 5/6. updated snapshots under the same filename; caching ──────────────────

def test_unchanged_snapshot_is_not_downloaded_again(tmp_path):
    server = FakeLdm()
    server.publish(H09, obs(H09, [0, 5, 10]))
    feed = ldm_feed(server, tmp_path)
    feed.poll(now=NOW)
    for i in range(1, 4):
        _, info = feed.poll(now=NOW + timedelta(minutes=i))
    assert server.downloads() == [ldm_name(H09)]                  # one download...
    assert len(server.listings()) == 4                            # ...but the cheap listing every poll
    assert [r["http_status"] for r in info["file_results"] if r["file"].endswith("0900.gz")] == ["cached"]


def test_same_filename_with_new_contents_is_detected_by_size(tmp_path):
    server = FakeLdm()
    server.publish(H09, obs(H09, [0, 5, 10]), modified_at="2026-10-29T09:20:00+00:00")
    feed = ldm_feed(server, tmp_path)
    observations, _ = feed.poll(now=NOW)
    assert newest(observations) == "2026-10-29T09:10:00+00:00"
    server.publish(H09, obs(H09, [0, 5, 10, 15, 20, 25]), modified_at="2026-10-29T09:20:00+00:00")   # same mtime, new size
    observations, info = feed.poll(now=NOW + timedelta(minutes=1))
    assert server.downloads() == [ldm_name(H09)] * 2
    assert newest(observations) == "2026-10-29T09:25:00+00:00" and info["cache_updated"]


def test_same_filename_with_new_modified_time_is_detected_even_if_the_size_is_equal(tmp_path):
    server = FakeLdm()
    server.publish(H09, obs(H09, [0, 5, 10], base=84.0), modified_at="2026-10-29T09:20:00+00:00")
    feed = ldm_feed(server, tmp_path)
    feed.poll(now=NOW)
    server.publish(H09, obs(H09, [0, 5, 10], base=85.0), modified_at="2026-10-29T09:25:00+00:00")   # same length
    observations, _ = feed.poll(now=NOW + timedelta(minutes=1))
    assert len(server.downloads()) == 2 and observations[-1]["temp_f"] == pytest.approx(85.2)


def test_snapshot_mtime_is_never_used_as_an_observation_time(tmp_path):
    server = FakeLdm()
    server.publish(H09, obs(H09, [0, 5, 10]), modified_at="2026-10-29T09:31:59+00:00")   # file touched at 09:31
    feed = ldm_feed(server, tmp_path)
    observations, info = feed.poll(now=NOW)
    assert newest(observations) == "2026-10-29T09:10:00+00:00"
    assert info["latency"]["observation_age_seconds"] == pytest.approx(22 * 60)         # not 1 second


def test_cache_is_bounded(tmp_path):
    server = FakeLdm()
    old = NOW - timedelta(hours=60)
    server.publish(hour_of(old), obs(hour_of(old), [0, 5]))
    server.publish(H09, obs(H09, [0, 5]))
    feed = ldm_feed(server, tmp_path)
    feed.cache[f"{hour_of(old):%Y%m%d_%H}00.gz"] = {"records": [], "final": True, "origin": "ldm", "version": "x"}
    feed.poll(now=NOW)
    assert f"{hour_of(old):%Y%m%d_%H}00.gz" not in feed.cache
    assert f"{H09:%Y%m%d_%H}00.gz" in feed.cache


# ── 7-9. bad payloads ────────────────────────────────────────────────────────

def test_malformed_gzip_is_an_error_and_keeps_last_good(tmp_path):
    server = FakeLdm()
    server.publish(H09, obs(H09, [0, 5, 10]))
    feed = ldm_feed(server, tmp_path)
    feed.poll(now=NOW)
    good = feed.last_good
    server.publish(H09, b"this is not gzip at all", modified_at="2026-10-29T09:40:00+00:00")
    observations, info = feed.poll(now=NOW + timedelta(minutes=1))
    assert "[bad_gzip]" in info["errors"][0] and feed.last_good == good
    assert newest(observations) == good["observed_at"]                      # the earlier good snapshot is kept


def test_truncated_gzip_is_an_error(tmp_path):
    server = FakeLdm()
    blob = fx.build_gz(obs(H09, [0, 5, 10]))
    server.publish(H09, blob[: len(blob) // 2])
    _, info = ldm_feed(server, tmp_path).poll(now=NOW)
    assert "[bad_gzip]" in info["errors"][0]


def test_valid_gzip_that_is_not_netcdf_is_an_error(tmp_path):
    server = FakeLdm()
    server.publish(H09, gzip.compress(b"plain text, not a netCDF file"))
    feed = ldm_feed(server, tmp_path)
    observations, info = feed.poll(now=NOW)
    assert "[bad_netcdf]" in info["errors"][0] and observations == [] and feed.last_good is None


def test_empty_dataset_is_an_error(tmp_path):
    server = FakeLdm()
    server.publish(H09, fx.build_gz([]))
    _, info = ldm_feed(server, tmp_path).poll(now=NOW)
    assert "[empty_dataset]" in info["errors"][0]


def test_file_without_kmia_records_is_flagged_but_invents_nothing(tmp_path):
    server = FakeLdm()
    server.publish(H09, [fx.row(H09 + timedelta(minutes=5 * i), 80.0, station="KFLL") for i in range(4)])
    feed = ldm_feed(server, tmp_path)
    observations, info = feed.poll(now=NOW)
    res = next(r for r in info["file_results"] if r["file"].endswith("0900.gz"))
    assert res["note"] == "no_station_records" and res["decoded_records"] == 4 and res["kmia_records"] == 0
    assert observations == [] and feed.last_good is None and info["errors"] == []


def test_empty_response_body_is_an_error(tmp_path):
    server = FakeLdm()
    server.publish(H09, b"")
    _, info = ldm_feed(server, tmp_path).poll(now=NOW)
    assert "[empty_response]" in info["errors"][0]


def test_checksum_mismatch_is_rejected(tmp_path):
    server = FakeLdm()
    server.publish(H09, obs(H09, [0, 5]))
    original = server.get
    def with_sha(url, headers=None, timeout=None):
        resp = original(url, headers, timeout)
        if url.endswith("/files"):
            for f in resp.json()["files"]:
                f["sha256"] = "0" * 64
        return resp
    server.get = with_sha
    _, info = ldm_feed(server, tmp_path).poll(now=NOW)
    assert "[checksum]" in info["errors"][0]


# ── 10/11. duplicates and corrections ────────────────────────────────────────

def test_duplicate_observations_collapse_to_one(tmp_path):
    server = FakeLdm()
    rows = obs(H09, [0, 5, 10])
    server.publish(H09, rows + [dict(rows[1])] + obs(H09, [10]))                 # a repeated 09:05 and 09:10
    observations, _ = ldm_feed(server, tmp_path).poll(now=NOW)
    assert [o["observed_at"][11:16] for o in observations] == ["09:00", "09:05", "09:10"]


def test_a_corrected_newest_observation_replaces_last_good(tmp_path):
    server = FakeLdm()
    server.publish(H09, obs(H09, [0, 5, 10], base=84.0), modified_at="2026-10-29T09:20:00+00:00")
    feed = ldm_feed(server, tmp_path)
    feed.poll(now=NOW)
    assert feed.last_good["temp_f"] == pytest.approx(84.2)
    corrected = obs(H09, [0, 5], base=84.0) + [fx.row(H09 + timedelta(minutes=10), 87.0)]
    server.publish(H09, corrected, modified_at="2026-10-29T09:26:00+00:00")
    observations, info = feed.poll(now=NOW + timedelta(minutes=1))
    assert info["cache_updated"] and info["last_good_corrected"]
    assert feed.last_good["temp_f"] == pytest.approx(87.0) and observations[-1]["temp_f"] == pytest.approx(87.0)
    assert json.loads((tmp_path / "last_good.json").read_text())["last_good"]["temp_f"] == pytest.approx(87.0)


def test_a_corrected_older_observation_is_replaced_in_the_series(tmp_path):
    server = FakeLdm()
    server.publish(H09, obs(H09, [0, 5, 10], base=84.0), modified_at="2026-10-29T09:20:00+00:00")
    feed = ldm_feed(server, tmp_path)
    feed.poll(now=NOW)
    fixed = [fx.row(H09, 84.0), fx.row(H09 + timedelta(minutes=5), 86.0), fx.row(H09 + timedelta(minutes=10), 84.2)]
    server.publish(H09, fixed, modified_at="2026-10-29T09:26:00+00:00")
    observations, _ = feed.poll(now=NOW + timedelta(minutes=1))
    assert [o["temp_f"] for o in observations] == [pytest.approx(84.0), pytest.approx(86.0), pytest.approx(84.2)]


# ── 12. last-good behaviour ──────────────────────────────────────────────────

def test_outage_keeps_the_last_good_observation_and_does_not_make_it_fresher(tmp_path):
    server = FakeLdm()
    server.publish(H09, obs(H09, [0, 5, 10]))
    feed = ldm_feed(server, tmp_path)
    feed.poll(now=NOW)
    later = NOW + timedelta(minutes=10)
    server.script["/api/v1/hfmetar/files"] = [FakeResponse(503)]
    observations, info = feed.poll(now=later)
    assert info["source_error_category"] == "server_error" and info["last_poll_success"] is False
    assert newest(observations) == "2026-10-29T09:10:00+00:00"
    assert info["last_good_age_seconds"] == pytest.approx(32 * 60)         # ages with the clock, not with the request


def test_an_older_snapshot_never_replaces_a_newer_last_good(tmp_path):
    server = FakeLdm()
    server.publish(H09, obs(H09, [0, 5, 10, 15, 20]), modified_at="2026-10-29T09:30:00+00:00")
    feed = ldm_feed(server, tmp_path)
    feed.poll(now=NOW)
    server.publish(H09, obs(H09, [0, 5]), modified_at="2026-10-29T09:31:00+00:00")   # an older, shorter re-issue
    observations, info = feed.poll(now=NOW + timedelta(minutes=1))
    assert feed.last_good["observed_at"] == "2026-10-29T09:20:00+00:00" and not info["cache_updated"]
    assert newest(observations) == "2026-10-29T09:20:00+00:00"


def test_last_good_survives_a_restart_and_is_not_stale_by_download_time(tmp_path):
    server = FakeLdm()
    server.publish(H09, obs(H09, [0, 5, 10]))
    ldm_feed(server, tmp_path).poll(now=NOW)
    restarted = ldm_feed(FakeLdm(), tmp_path)                                # empty API: only the disk state remains
    observations, info = restarted.poll(now=NOW + timedelta(minutes=2))
    assert newest(observations) == "2026-10-29T09:10:00+00:00"
    assert info["last_good_age_seconds"] == pytest.approx(24 * 60)             # real age; the download time is irrelevant
    too_late = ldm_feed(FakeLdm(), tmp_path).poll(now=NOW + timedelta(minutes=5))[0]
    assert too_late == []                                                      # > 25 min old: the persisted value is dropped


# ── 13/14. UTC hour rollover, UTC and Miami day rollover, DST ────────────────

def test_utc_midnight_rollover_uses_yesterdays_23h_file_as_previous_hour(tmp_path):
    now = datetime(2026, 10, 29, 0, 3, tzinfo=UTC)
    h23, h00 = datetime(2026, 10, 28, 23, tzinfo=UTC), datetime(2026, 10, 29, 0, tzinfo=UTC)
    server = FakeLdm()
    server.publish(h23, obs(h23, [50, 55], base=80.0))
    server.publish(h00, obs(h00, [0], base=81.0))
    observations, info = ldm_feed(server, tmp_path).poll(now=now)
    assert info["previous_file"] == "20261028_2300.gz" and info["current_file"] == "20261029_0000.gz"
    assert ldm_name(h23) in server.downloads() and newest(observations) == "2026-10-29T00:00:00+00:00"


def test_recorded_high_survives_utc_midnight_and_resets_at_miami_midnight(tmp_path):
    server = FakeLdm()
    h20 = datetime(2026, 10, 28, 20, tzinfo=UTC)               # 16:00 Miami, the hot part of the day
    h00 = datetime(2026, 10, 29, 0, tzinfo=UTC)                # 20:00 Miami
    server.publish(h20, [fx.row(h20, 91.0), fx.row(h20 + timedelta(minutes=5), 90.0)])
    server.publish(h00, obs(h00, [0, 5, 10], base=84.0))
    feed = ldm_feed(server, tmp_path, backfill=False)
    now = datetime(2026, 10, 29, 0, 20, tzinfo=UTC)            # UTC date has rolled over; Miami is still Oct 28
    observations, _ = feed.poll(now=now)
    today = asos_nowcast.todays_high(observations, now)
    assert today["miami_date"] == "2026-10-28" and today["high_f"] == pytest.approx(91.0)
    # Miami midnight is 04:00Z on Oct 29: the new day must not inherit yesterday's 91
    h04 = datetime(2026, 10, 29, 4, tzinfo=UTC)
    server.publish(h04, obs(h04, [5, 10], base=75.0))
    later = datetime(2026, 10, 29, 4, 15, tzinfo=UTC)
    observations, _ = feed.poll(now=later)
    today = asos_nowcast.todays_high(observations, later)
    assert today["miami_date"] == "2026-10-29" and today["high_f"] == pytest.approx(75.1)


def test_hours_needed_covers_the_miami_day_across_dst():
    assert asos_client.hours_needed(datetime(2026, 11, 1, 12, 0, tzinfo=UTC)) == pytest.approx(8.5)    # EDT midnight 04:00Z
    assert asos_client.hours_needed(datetime(2026, 11, 2, 12, 0, tzinfo=UTC)) == pytest.approx(7.5)    # EST midnight 05:00Z


def test_hours_the_ldm_api_no_longer_holds_are_reported_when_backfill_is_off(tmp_path):
    server = FakeLdm()
    server.publish(H08, obs(H08, [50, 55]))
    server.publish(H09, obs(H09, [0, 5]))
    feed = ldm_feed(server, tmp_path, backfill=False)
    _, info = feed.poll(now=NOW)
    assert info["errors"] == [] and info["backfilled_from_public"] == []
    assert "20261029_0400.gz" in info["coverage_missing_hours"]             # the early-morning hours are absent


def test_old_hours_missing_from_ldm_are_backfilled_once_from_public(tmp_path):
    server, public = FakeLdm(), FakePublic()
    server.publish(H08, obs(H08, [50, 55]))
    server.publish(H09, obs(H09, [0, 5]))
    h05 = datetime(2026, 10, 29, 5, tzinfo=UTC)
    public.publish(h05, [fx.row(h05, 79.0)])
    feed = ldm_feed(server, tmp_path, backfill=True, public_http=public)
    _, info = feed.poll(now=NOW)
    assert "20261029_0500.gz" in info["backfilled_from_public"]
    n_urls = len(public.urls)
    feed.poll(now=NOW + timedelta(minutes=1))
    assert not any(u.endswith("0900.gz") or u.endswith("0800.gz") for u in public.urls)   # current/previous: never public
    assert info["coverage_missing_hours"] is not None and n_urls >= 1


# ── 15. staleness ────────────────────────────────────────────────────────────

def test_a_successful_download_does_not_make_old_weather_fresh(tmp_path):
    server = FakeLdm()
    server.publish(H09, obs(H09, [0, 5, 10]))
    feed = ldm_feed(server, tmp_path)
    observations, info = feed.poll(now=NOW)
    assert asos_nowcast.build_shared_input(observations, info, NOW)["metadata"]["status"] == "delayed"   # 22 min old
    later = NOW + timedelta(minutes=10)
    observations, info = feed.poll(now=later)                          # API fine, snapshot unchanged
    shared = asos_nowcast.build_shared_input(observations, info, later)
    assert info["last_poll_success"] is True and info["latency"]["observation_age_seconds"] == pytest.approx(32 * 60)
    assert shared["metadata"]["status"] == "unavailable" and shared["current"] is None
    assert shared["last_known"]["observed_at"] == "2026-10-29T09:10:00+00:00"       # shown, but never fed to a model


def test_fresh_observation_is_healthy(tmp_path):
    server = FakeLdm()
    server.publish(H09, obs(H09, [0, 5, 10, 15, 20, 25, 30]))
    observations, info = ldm_feed(server, tmp_path).poll(now=NOW)
    assert asos_nowcast.build_shared_input(observations, info, NOW)["metadata"]["status"] == "healthy"


# ── 16. network failures ─────────────────────────────────────────────────────

def _listing_ok(server):
    return FakeResponse(200, json_data={"files": [
        {"name": n, "size_bytes": len(f["content"]), "modified_at": f["modified_at"]} for n, f in server.files.items()]})


def test_transient_failures_are_retried_with_bounded_backoff(tmp_path):
    server = FakeLdm()
    server.publish(H09, obs(H09, [0, 5, 10]))
    server.script["/api/v1/hfmetar/files"] = [requests.exceptions.ConnectTimeout("slow"), FakeResponse(502)]
    sleeps = []
    feed = ldm_feed(server, tmp_path, retries=2, sleeps=sleeps)
    observations, info = feed.poll(now=NOW)
    assert info["errors"] == [] and newest(observations) == "2026-10-29T09:10:00+00:00"
    assert sleeps == [0.5, 1.0] and len(server.listings()) == 3


def test_retries_are_bounded_then_reported(tmp_path):
    server = FakeLdm()
    server.script["/api/v1/hfmetar/files"] = [requests.exceptions.ConnectionError("down")] * 10
    sleeps = []
    _, info = ldm_feed(server, tmp_path, retries=2, sleeps=sleeps).poll(now=NOW)
    assert len(server.listings()) == 3 and info["source_error_category"] == "connection"
    assert "LDM listing: [connection]" in info["errors"][0]


def test_timeout_is_reported_as_timeout(tmp_path):
    server = FakeLdm()
    server.script["/api/v1/hfmetar/files"] = [requests.exceptions.ReadTimeout("slow")]
    _, info = ldm_feed(server, tmp_path, retries=0).poll(now=NOW)
    assert info["source_error_category"] == "timeout"


def test_tls_failure_is_not_retried(tmp_path):
    server = FakeLdm()
    server.script["/api/v1/hfmetar/files"] = [requests.exceptions.SSLError("bad certificate")] * 5
    _, info = ldm_feed(server, tmp_path, retries=3).poll(now=NOW)
    assert info["source_error_category"] == "tls" and len(server.listings()) == 1


def test_auth_and_not_found_are_not_retried(tmp_path):
    server = FakeLdm()
    server.publish(H09, obs(H09, [0, 5]))
    server.script["/api/v1/hfmetar/files"] = [FakeResponse(403)]
    _, info = ldm_feed(server, tmp_path, retries=3).poll(now=NOW)
    assert info["source_error_category"] == "auth" and len(server.listings()) == 1
    server2 = FakeLdm()
    server2.publish(H09, obs(H09, [0, 5]))
    server2.script[f"/api/v1/hfmetar/{ldm_name(H09)}"] = [FakeResponse(404)]
    _, info2 = ldm_feed(server2, tmp_path, retries=3).poll(now=NOW)
    assert "[not_found]" in info2["errors"][0] and server2.downloads() == [ldm_name(H09)]


def test_rate_limit_honours_a_short_retry_after_and_gives_up_on_a_long_one(tmp_path):
    server = FakeLdm()
    server.publish(H09, obs(H09, [0, 5]))
    server.script["/api/v1/hfmetar/files"] = [FakeResponse(429, headers={"Retry-After": "2"}), _listing_ok(server)]
    sleeps = []
    _, info = ldm_feed(server, tmp_path, retries=1, sleeps=sleeps).poll(now=NOW)
    assert sleeps == [2.0] and info["errors"] == []
    server2 = FakeLdm()
    server2.script["/api/v1/hfmetar/files"] = [FakeResponse(429, headers={"Retry-After": "120"})] * 3
    sleeps2 = []
    _, info2 = ldm_feed(server2, tmp_path, retries=2, sleeps=sleeps2).poll(now=NOW)
    assert sleeps2 == [] and info2["source_error_category"] == "rate_limited" and len(server2.listings()) == 1


def test_invalid_listing_is_reported(tmp_path):
    server = FakeLdm()
    server.script["/api/v1/hfmetar/files"] = [FakeResponse(200, b"<html>", json_data=_NO_JSON)]
    _, info = ldm_feed(server, tmp_path).poll(now=NOW)
    assert info["source_error_category"] == "bad_listing"
    server.script["/api/v1/hfmetar/files"] = [FakeResponse(200, json_data={"unexpected": 1})]
    _, info = ldm_feed(server, tmp_path).poll(now=NOW)
    assert info["source_error_category"] == "bad_listing"


def test_truncated_download_is_rejected(tmp_path):
    server = FakeLdm()
    server.publish(H09, obs(H09, [0, 5]))
    blob = server.files[ldm_name(H09)]["content"]
    server.script[f"/api/v1/hfmetar/{ldm_name(H09)}"] = [FakeResponse(200, blob[:-10], headers={"Content-Length": str(len(blob))})]
    _, info = ldm_feed(server, tmp_path).poll(now=NOW)
    assert "[truncated]" in info["errors"][0]


def test_time_budget_stops_a_slow_poll(tmp_path):
    api = make_api(FakeLdm(), retries=5)
    clock = {"t": 0.0}
    deadline = ldm_client.Deadline(seconds=10, clock=lambda: clock["t"])
    clock["t"] = 11.0
    with pytest.raises(ldm_client.LdmError) as exc:
        api._request("/api/v1/hfmetar/files", deadline)
    assert exc.value.category == "timeout"


def test_listing_parser_tolerates_shapes_and_ignores_bad_names():
    listing = ldm_client.parse_listing({"files": [
        {"name": "hfmetar.20261029_0900.gz", "size_bytes": 10, "modified_at": "2026-10-29T09:30:00+00:00"},
        {"name": "hfmetar.20261029_0900.gz", "size_bytes": 12, "modified_at": "2026-10-29T09:35:00+00:00"},
        {"name": "../../etc/passwd"}, {"name": "notes.txt"}, "junk", {"name": "hfmetar.20261029_0800.gz"}]})
    assert sorted(m.name for m in listing.values()) == ["hfmetar.20261029_0800.gz", "hfmetar.20261029_0900.gz"]
    assert listing[datetime(2026, 10, 29, 9, tzinfo=UTC)].size == 12                     # the newer duplicate wins
    assert ldm_client.parse_listing([{"name": "hfmetar.20261029_0700.gz"}])                 # bare list accepted
    with pytest.raises(ldm_client.LdmError):
        ldm_client.parse_listing({"files": "nope"})


# ── 17. public source stays available and is the default ─────────────────────

def test_public_is_the_default_and_never_calls_the_ldm_api(monkeypatch, tmp_path):
    public, ldm = FakePublic(), FakeLdm()
    public.publish(H09, obs(H09, [0, 5, 10]))
    feed = asos_client.AsosFeed(state_path=tmp_path / "lg.json", public_http=public)        # ASOS_SOURCE unset
    observations, info = feed.poll(now=NOW)
    assert info["source"] == "public" and newest(observations) == "2026-10-29T09:10:00+00:00"
    assert public.urls and all(u.startswith(wcfg.MADIS_HFMETAR_URL) for u in public.urls) and ldm.log == []


def test_switching_back_is_only_an_environment_change(monkeypatch, tmp_path):
    public = FakePublic()
    public.publish(H09, obs(H09, [0, 5, 10]))
    monkeypatch.setenv("ASOS_SOURCE", "public")
    monkeypatch.setenv("LDM_API_TOKEN", TOKEN)                         # a leftover token changes nothing
    feed = asos_client.AsosFeed(state_path=tmp_path / "lg.json", public_http=public)
    _, info = feed.poll(now=NOW)
    assert info["source"] == "public" and info["errors"] == []


def test_public_source_still_uses_conditional_requests_and_caches(tmp_path):
    public = FakePublic()
    public.publish(H09, obs(H09, [0, 5, 10]))
    feed = public_feed(public, tmp_path)
    feed.poll(now=NOW)
    _, info = feed.poll(now=NOW + timedelta(minutes=1))
    res = next(r for r in info["file_results"] if r["file"].endswith("0900.gz"))
    assert res["http_status"] == 304 and info["errors"] == []
    entry = feed.cache["20261029_0900.gz"]
    assert entry["origin"] == "public" and entry["download_ms"] is not None


def test_public_404_and_corrupt_files_still_keep_the_last_good(tmp_path):
    public = FakePublic()
    public.publish(H09, obs(H09, [0, 5, 10]))
    feed = public_feed(public, tmp_path)
    feed.poll(now=NOW)
    public.files["20261029_0900.gz"] = b"garbage"
    public.files.pop("20261029_0800.gz", None)
    feed.cache["20261029_0900.gz"]["last_modified"] = None
    observations, info = feed.poll(now=NOW + timedelta(minutes=1))
    assert info["errors"] and newest(observations) == "2026-10-29T09:10:00+00:00"


# ── 18. shadow comparison ────────────────────────────────────────────────────

def summaries(tmp_path, ldm_minutes, public_minutes, ldm_base=84.0, public_base=84.0):
    server, public = FakeLdm(), FakePublic()
    server.publish(H09, obs(H09, ldm_minutes, base=ldm_base))
    public.publish(H09, obs(H09, public_minutes, base=public_base))
    lobs, linfo = ldm_feed(server, tmp_path, persist=False).poll(now=NOW)
    pobs, pinfo = public_feed(public, tmp_path, persist=False).poll(now=NOW)
    return (asos_shadow.summarise("ldm", lobs, linfo, NOW), asos_shadow.summarise("public", pobs, pinfo, NOW))


def test_comparison_reports_which_source_is_newer_and_temperature_differences(tmp_path):
    ldm_s, public_s = summaries(tmp_path, [0, 5, 10, 15, 20], [0, 5, 10], ldm_base=84.0, public_base=84.5)
    rec = asos_shadow.compare(public_s, ldm_s)                          # primary = public, shadow = ldm
    assert rec["leader"] == "shadow" and rec["newest_time_diff_seconds"] == 600.0
    assert rec["primary_age_seconds"] == pytest.approx(22 * 60) and rec["shadow_age_seconds"] == pytest.approx(12 * 60)
    assert rec["common_observations"] == 3 and rec["max_temp_diff_f"] == pytest.approx(0.5) and rec["temp_mismatches"] == 3
    assert rec["newest_temp_diff_f"] is None                            # newest times differ, so no like-for-like value


def test_comparison_of_identical_data_is_a_tie_with_no_mismatches(tmp_path):
    ldm_s, public_s = summaries(tmp_path, [0, 5, 10], [0, 5, 10])
    rec = asos_shadow.compare(ldm_s, public_s)
    assert rec["leader"] == "tie" and rec["temp_mismatches"] == 0 and rec["newest_temp_diff_f"] == 0.0


def test_comparison_records_qc_counts(tmp_path):
    server, public = FakeLdm(), FakePublic()
    bad = obs(H09, [0, 5, 10])
    bad[1]["temp_k"] = 5000.0                                            # impossible: the parser rejects it
    server.publish(H09, bad)
    public.publish(H09, obs(H09, [0, 5, 10]))
    lobs, linfo = ldm_feed(server, tmp_path, persist=False).poll(now=NOW)
    pobs, pinfo = public_feed(public, tmp_path, persist=False).poll(now=NOW)
    rec = asos_shadow.compare(asos_shadow.summarise("public", pobs, pinfo, NOW),
                              asos_shadow.summarise("ldm", lobs, linfo, NOW))
    assert rec["shadow_rejected"] >= 1 and rec["primary_rejected"] == 0 and rec["shadow_valid"] == 2


def test_shadow_run_records_errors_separately_and_never_raises(tmp_path):
    public = FakePublic()
    public.publish(H09, obs(H09, [0, 5, 10]))
    pobs, pinfo = public_feed(public, tmp_path, persist=False).poll(now=NOW)
    primary = asos_shadow.summarise("public", pobs, pinfo, NOW)
    broken = FakeLdm()
    broken.script["/api/v1/hfmetar/files"] = [FakeResponse(500)] * 5
    rec = asos_shadow.run_comparison(primary, NOW, feed=ldm_feed(broken, tmp_path, persist=False))
    assert rec["shadow_errors"] and rec["primary_errors"] == [] and rec["shadow_newest"] is None
    assert asos_shadow.summary()["comparisons"] == 1 and asos_shadow.summary()["shadow_error_polls"] == 1


def test_a_crashing_shadow_feed_is_contained(tmp_path):
    class Boom:
        def poll(self, **kw):
            raise RuntimeError("kaboom")
    public = FakePublic()
    public.publish(H09, obs(H09, [0, 5]))
    pobs, pinfo = public_feed(public, tmp_path, persist=False).poll(now=NOW)
    rec = asos_shadow.run_comparison(asos_shadow.summarise("public", pobs, pinfo, NOW), NOW, feed=Boom())
    assert rec["shadow_errors"] == ["shadow poll crashed: RuntimeError"]


def test_shadow_without_a_token_is_a_recorded_error_and_sends_nothing(tmp_path):
    public = FakePublic()
    public.publish(H09, obs(H09, [0, 5, 10]))
    pobs, pinfo = public_feed(public, tmp_path, persist=False).poll(now=NOW)
    feed = asos_client.AsosFeed(source="ldm", persist=False)                # no injected API, no token in env
    rec = asos_shadow.run_comparison(asos_shadow.summarise("public", pobs, pinfo, NOW), NOW, feed=feed)
    assert "LDM_API_TOKEN" in rec["shadow_errors"][0] and rec["primary_errors"] == []


def test_shadow_feed_never_touches_the_last_good_file(tmp_path, monkeypatch):
    state = tmp_path / "must_not_exist.json"
    monkeypatch.setattr(asos_client, "STATE_PATH", state)
    server = FakeLdm()
    server.publish(H09, obs(H09, [0, 5, 10]))
    feed = asos_client.AsosFeed(source="ldm", ldm=make_api(server), persist=False)
    feed.poll(now=NOW, recent_only=True)
    assert feed.last_good is not None and not state.exists()
    assert asos_shadow._shadow_feed("ldm").persist is False


def test_shadow_only_requests_the_recent_hours(tmp_path):
    server = FakeLdm()
    server.publish(H08, obs(H08, [50]))
    server.publish(H09, obs(H09, [0, 5]))
    server.publish(datetime(2026, 10, 29, 5, tzinfo=UTC), [fx.row(datetime(2026, 10, 29, 5, tzinfo=UTC), 79.0)])
    ldm_feed(server, tmp_path, persist=False).poll(now=NOW, recent_only=True)
    assert sorted(server.downloads()) == [ldm_name(H08), ldm_name(H09)]


def test_shadow_is_off_by_default_and_rate_limited_when_on(monkeypatch):
    started = []
    monkeypatch.setattr(asos_shadow, "_run_safe", lambda summary, now: started.append(summary["source"]))
    assert asos_shadow.notify("ldm", [], {}, NOW) is False and started == []
    monkeypatch.setenv("ASOS_SHADOW_COMPARE", "true")
    assert asos_shadow.notify("ldm", [], {}, NOW) is True
    for _ in range(50):                                                  # let the thread run
        if started:
            break
        import time as _t
        _t.sleep(0.01)
    assert started == ["ldm"]
    assert asos_shadow.notify("ldm", [], {}, NOW) is False               # inside the minimum interval
    assert asos_shadow.notify(None, [], {}, NOW) is False                # a configuration error has no source to shadow


def test_shadow_alternate_source_and_log_file(tmp_path, monkeypatch):
    assert asos_shadow.alternate_source("ldm") == "public" and asos_shadow.alternate_source("public") == "ldm"
    log_path = tmp_path / "shadow.jsonl"
    monkeypatch.setenv("ASOS_SHADOW_LOG_PATH", str(log_path))
    ldm_s, public_s = summaries(tmp_path, [0, 5, 10, 15], [0, 5, 10])
    asos_shadow._record(asos_shadow.compare(public_s, ldm_s))
    line = json.loads(log_path.read_text().splitlines()[0])
    assert line["leader"] == "shadow" and "at" in line and TOKEN not in log_path.read_text()


def test_a_shadow_poll_does_not_change_what_the_primary_returns(tmp_path, monkeypatch):
    server, public = FakeLdm(), FakePublic()
    server.publish(H09, obs(H09, [0, 5, 10, 15]))
    public.publish(H09, obs(H09, [0, 5, 10]))
    primary = ldm_feed(server, tmp_path)
    monkeypatch.setattr(asos_client, "_feed", primary)
    monkeypatch.setenv("ASOS_SHADOW_COMPARE", "true")
    monkeypatch.setattr(asos_shadow, "_shadow_feed", lambda alt: public_feed(public, tmp_path, persist=False))
    observations, info = asos_client.fetch_recent_observations(now=NOW)
    for _ in range(100):
        if asos_shadow.summary()["comparisons"]:
            break
        import time as _t
        _t.sleep(0.02)
    assert asos_shadow.summary()["comparisons"] == 1
    assert newest(observations) == "2026-10-29T09:15:00+00:00" and primary.last_good["observed_at"] == "2026-10-29T09:15:00+00:00"
    assert asos_shadow.summary()["last"]["leader"] == "primary"          # ldm (primary) is 5 minutes ahead of public


# ── 19. identical model input for identical data ─────────────────────────────

def test_identical_observations_give_identical_model_input_from_either_source(tmp_path):
    rows = obs(H08, [50, 55], base=82.0) + obs(H09, [0, 5, 10, 15, 20, 25, 30], base=83.0)
    server, public = FakeLdm(), FakePublic()
    server.publish(H08, rows[:2])
    server.publish(H09, rows[2:])
    public.publish(H08, rows[:2])
    public.publish(H09, rows[2:])
    lobs, linfo = ldm_feed(server, tmp_path).poll(now=NOW)
    pobs, pinfo = public_feed(public, tmp_path).poll(now=NOW)
    assert lobs == pobs
    a = asos_nowcast.build_shared_input(lobs, linfo, NOW)
    b = asos_nowcast.build_shared_input(pobs, pinfo, NOW)
    for key in ("station", "current", "last_known", "recent_observations", "features", "future_features", "today"):
        assert a[key] == b[key], key
    for key in ("source", "weather_source", "forecast_input_mode", "status", "latest_observation_at",
                "latest_observation_age_seconds", "observations_loaded", "lookback_minutes"):
        assert a["metadata"][key] == b["metadata"][key], key


def test_forecast_shared_input_is_built_from_the_same_observation_structure(tmp_path, monkeypatch):
    """forecast._fetch_hfmetar_shared() reads only fields the existing parser produces, whichever source ran."""
    import weather as weather_pkg
    server = FakeLdm()
    server.publish(H09, obs(H09, [0, 5, 10, 15, 20, 25, 30]))
    feed = ldm_feed(server, tmp_path)
    observations, info = feed.poll(now=NOW)
    shared = asos_nowcast.build_shared_input(observations, info, NOW)
    assert shared["current"] and set(shared["current"]) >= {"temp_f", "relative_humidity", "wind_speed_mph",
                                                            "wind_dir_deg", "pressure_hpa", "cloud_fraction", "observed_at"}
    assert shared["today"]["high_f"] == pytest.approx(84.6)


# ── 20. nothing else changed ─────────────────────────────────────────────────

PROTECTED = ["algorithm.py", "client.py", "stream.py", "price_tracker.py", "auth.py", "autotrader.py", "paper.py",
             "notifier.py", "forecast.py", "temp_monitor.py", "live_trading.py"]


@pytest.mark.parametrize("name", PROTECTED)
def test_trading_forecasting_and_notification_code_knows_nothing_about_the_source_switch(name):
    path = HERE.parent / "kalshi_api" / name
    if not path.exists():
        pytest.skip(f"{name} not present in this project")
    text = path.read_text(encoding="utf-8")
    for needle in ("ldm_client", "asos_shadow", "ASOS_SOURCE", "LDM_API", "ASOS_SHADOW"):
        assert needle not in text, f"{name} mentions {needle}"


def test_model_scripts_do_not_mention_the_source_switch():
    for path in (HERE.parent / "modelscripts").glob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "ldm_client" not in text and "ASOS_SOURCE" not in text and "LDM_API" not in text, path.name


# ── Django: startup check and health endpoint ────────────────────────────────

def test_django_check_fails_clearly_for_ldm_without_a_token(monkeypatch):
    import django
    django.setup()
    from kalshi_api import checks as kchecks
    monkeypatch.setenv("ASOS_SOURCE", "ldm")
    errors = kchecks.weather_source_configuration(None)
    assert len(errors) == 1 and errors[0].id == "weather.E001" and "LDM_API_TOKEN" in errors[0].msg
    monkeypatch.setenv("LDM_API_TOKEN", TOKEN)
    assert kchecks.weather_source_configuration(None) == []
    monkeypatch.setenv("ASOS_SOURCE", "public")
    assert kchecks.weather_source_configuration(None) == []
    assert TOKEN not in str(errors)


def test_health_endpoint_reports_the_source_and_never_the_token(monkeypatch, tmp_path):
    import django
    sys.modules.setdefault("websockets", types.ModuleType("websockets"))      # only used by the unrelated stream module
    django.setup()
    from django.test import RequestFactory
    from kalshi_api import views

    server = FakeLdm()
    server.publish(H09, obs(H09, [0, 5, 10]))
    feed = ldm_feed(server, tmp_path)
    feed.poll(now=NOW)
    monkeypatch.setattr(asos_client, "_feed", feed)
    monkeypatch.setenv("ASOS_SOURCE", "ldm")
    monkeypatch.setenv("LDM_API_TOKEN", TOKEN)
    response = views.weather_source_health(RequestFactory().get("/api/kalshi/weather/source/"))
    body = response.content.decode()
    data = json.loads(body)
    assert data["configured_source"] == "ldm" and data["config_problems"] == []
    assert data["ldm_host"] == "data.getedgefront.com" and data["last_poll"]["source"] == "ldm"
    assert data["latest_observation"]["observed_at"] == "2026-10-29T09:10:00+00:00"
    assert data["shadow"]["enabled"] is False
    assert TOKEN not in body


# ── arrival log (one line per newly delivered observation) ───────────────────

def read_log(path):
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]


def test_arrival_log_has_one_row_per_new_observation_and_flags_the_startup_batch(tmp_path, monkeypatch):
    log_path = tmp_path / "arr.jsonl"
    monkeypatch.setenv("ASOS_ARRIVAL_LOG_PATH", str(log_path))
    server = FakeLdm()
    server.publish(H09, obs(H09, [0, 5, 10], base=84.0), modified_at="2026-10-29T09:20:00+00:00")
    feed = ldm_feed(server, tmp_path)
    feed.poll(now=NOW)
    first = read_log(log_path)
    assert [r["observation_time_utc"][11:16] for r in first] == ["09:00", "09:05", "09:10"]
    assert all(r["startup"] and r["source"] == "ldm" and r["role"] == "primary" and not r["corrected"] for r in first)
    assert first[0]["snapshot_filename"] == ldm_name(H09) and first[0]["snapshot_version"].endswith("09:20:00+00:00")
    assert first[2]["age_at_receipt_seconds"] == pytest.approx(22 * 60) and first[2]["temp_f"] == pytest.approx(84.2)
    later = NOW + timedelta(minutes=8)
    server.publish(H09, obs(H09, [0, 5, 10, 15, 20, 25, 30]), modified_at="2026-10-29T09:39:00+00:00")
    feed.poll(now=later)
    rows = read_log(log_path)[3:]
    assert [r["observation_time_utc"][11:16] for r in rows] == ["09:15", "09:20", "09:25", "09:30"]   # all four, not just the newest
    assert not any(r["startup"] for r in rows)
    assert rows[-1]["age_at_receipt_seconds"] == pytest.approx(10 * 60) and rows[-1]["snapshot_version"].endswith("09:39:00+00:00")
    feed.poll(now=later + timedelta(minutes=1))                          # nothing new -> nothing logged
    assert len(read_log(log_path)) == 7


def test_arrival_log_records_corrections_and_shadow_role(tmp_path, monkeypatch):
    log_path = tmp_path / "arr.jsonl"
    monkeypatch.setenv("ASOS_ARRIVAL_LOG_PATH", str(log_path))
    server, public = FakeLdm(), FakePublic()
    server.publish(H09, obs(H09, [0, 5, 10]), modified_at="2026-10-29T09:20:00+00:00")
    public.publish(H09, obs(H09, [0, 5]))
    feed = ldm_feed(server, tmp_path)
    feed.poll(now=NOW)
    public_feed(public, tmp_path, persist=False).poll(now=NOW)
    server.publish(H09, obs(H09, [0, 5], base=84.0) + [fx.row(H09 + timedelta(minutes=10), 88.0)],
                   modified_at="2026-10-29T09:30:00+00:00")
    feed.poll(now=NOW + timedelta(minutes=1))
    rows = read_log(log_path)
    assert {(r["source"], r["role"]) for r in rows} == {("ldm", "primary"), ("public", "shadow")}
    corrected = [r for r in rows if r["corrected"]]
    assert len(corrected) == 1 and corrected[0]["temp_f"] == pytest.approx(88.0) and not corrected[0]["startup"]


def test_arrival_log_is_off_unless_configured_and_never_breaks_polling(tmp_path, monkeypatch):
    server = FakeLdm()
    server.publish(H09, obs(H09, [0, 5, 10]))
    ldm_feed(server, tmp_path).poll(now=NOW)
    assert not list(tmp_path.glob("*.jsonl"))                             # unset: no file
    monkeypatch.setenv("ASOS_ARRIVAL_LOG_PATH", str(tmp_path))            # a directory: writing must fail quietly
    observations, info = ldm_feed(server, tmp_path).poll(now=NOW)
    assert info["errors"] == [] and newest(observations) == "2026-10-29T09:10:00+00:00"


def test_log_paths_are_resolved_against_the_backend_folder():
    backend = HERE.parent
    assert wcfg.resolve_log_path("") is None and wcfg.resolve_log_path(None) is None
    assert wcfg.resolve_log_path("logs/a.jsonl") == backend / "logs" / "a.jsonl"
    absolute = Path(os.path.abspath(os.sep)) / "x" / "a.jsonl"
    assert wcfg.resolve_log_path(str(absolute)) == absolute
