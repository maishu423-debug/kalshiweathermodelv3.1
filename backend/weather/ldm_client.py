"""
HTTP adapter for our private NOAA MADIS LDM API (the transport only — no weather parsing here).

The VPS runs the LDM receiver and serves the same gzip'd HFMETAR netCDF snapshots NOAA publishes, over
authenticated HTTPS:

    GET /health                           (no auth)
    GET /api/v1/hfmetar/files             -> {"files": [{"name", "size_bytes", "modified_at"}, ...]}
    GET /api/v1/hfmetar/latest            (not used: we need the current AND previous hour, and older hours)
    GET /api/v1/hfmetar/{filename}        -> gzip'd netCDF

Downloaded bytes go to the existing decoder in asos_client.py unchanged.

Environment (backend service ONLY — the token must never reach the frontend, a log or an error message):
    ASOS_SOURCE               ldm | public (default public)
    LDM_API_BASE_URL          default https://data.getedgefront.com
    LDM_API_TOKEN             required when ASOS_SOURCE=ldm
    LDM_API_TIMEOUT_SECONDS   per-request timeout, default 15
    LDM_API_MAX_RETRIES       retries of a transient failure, default 2
    LDM_BACKFILL_PUBLIC       default true: hours older than the previous hour that the LDM API no longer
                              holds are fetched once from NOAA's public files, so the day's recorded high is
                              never computed from a partial day. Current/previous hours are never backfilled.

Failure categories (LdmError.category): auth, not_found, rate_limited, server_error, timeout, connection,
tls, http_error, bad_listing, truncated, empty_response, checksum, plus the decode categories bad_gzip,
bad_netcdf, empty_dataset used by asos_client.
"""
import hashlib
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import urlsplit

import requests

from . import config as cfg
from .asos_parser import parse_utc
from .config import AsosConfigError

DEFAULT_BASE_URL = "https://data.getedgefront.com"
DEFAULT_TIMEOUT_SECONDS = 15.0
DEFAULT_MAX_RETRIES = 2
POLL_BUDGET_SECONDS = 40.0          # no new request or retry is started once a poll has used this long
RETRY_BACKOFF_SECONDS = (0.5, 1.0, 2.0)
MAX_RETRY_AFTER_SECONDS = 5.0       # a longer Retry-After is not waited for: the next poll retries instead

_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")      # a filename we are willing to put in a URL path
_HOUR_RE = re.compile(r"(\d{8})_(\d{2})00")
_TRUTHY = {"1", "true", "yes", "on"}


def env_flag(name, default=False, env=None):
    import os
    env = os.environ if env is None else env
    raw = env.get(name)
    return default if raw is None or not str(raw).strip() else str(raw).strip().lower() in _TRUTHY


# ── configuration ───────────────────────────────────────────────────────────

@dataclass(frozen=True)
class LdmConfig:
    base_url: str
    token: str = field(repr=False)            # never shown by repr()/logging
    timeout: float = DEFAULT_TIMEOUT_SECONDS
    max_retries: int = DEFAULT_MAX_RETRIES
    backfill_public: bool = True


def load_ldm_config(env=None):
    """Read and validate the LDM settings. Raises AsosConfigError (never makes a request)."""
    import os
    env = os.environ if env is None else env

    token = (env.get("LDM_API_TOKEN") or "").strip()
    if not token:
        raise AsosConfigError(
            "ASOS_SOURCE=ldm needs LDM_API_TOKEN on the backend service. Set it, or set ASOS_SOURCE=public. "
            "Requests are never sent without it.")

    base = (env.get("LDM_API_BASE_URL") or DEFAULT_BASE_URL).strip().rstrip("/")
    parts = urlsplit(base)
    local = parts.hostname in ("localhost", "127.0.0.1", "::1")
    if parts.scheme != "https" and not (parts.scheme == "http" and local):
        raise AsosConfigError("LDM_API_BASE_URL must be an https:// URL (the token is sent in a header).")
    if not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
        raise AsosConfigError("LDM_API_BASE_URL must be a plain base URL such as https://data.getedgefront.com")

    try:
        timeout = float(env.get("LDM_API_TIMEOUT_SECONDS") or DEFAULT_TIMEOUT_SECONDS)
    except ValueError:
        raise AsosConfigError("LDM_API_TIMEOUT_SECONDS must be a number of seconds.") from None
    if not 0 < timeout <= 120:
        raise AsosConfigError("LDM_API_TIMEOUT_SECONDS must be between 0 and 120.")

    try:
        retries = int(env.get("LDM_API_MAX_RETRIES") or DEFAULT_MAX_RETRIES)
    except ValueError:
        raise AsosConfigError("LDM_API_MAX_RETRIES must be a whole number.") from None
    if not 0 <= retries <= 5:
        raise AsosConfigError("LDM_API_MAX_RETRIES must be between 0 and 5.")

    return LdmConfig(base_url=base, token=token, timeout=timeout, max_retries=retries,
                     backfill_public=env_flag("LDM_BACKFILL_PUBLIC", True, env))


def validate_source_config(env=None):
    """List of human-readable configuration problems (empty = fine). Used by the Django startup check."""
    try:
        source = cfg.get_asos_source(env)
    except AsosConfigError as exc:
        return [str(exc)]
    if source == cfg.ASOS_SOURCE_LDM:
        try:
            load_ldm_config(env)
        except AsosConfigError as exc:
            return [str(exc)]
    return []


# ── errors / helpers ────────────────────────────────────────────────────────

class LdmError(Exception):
    """A classified failure. The message never contains the token."""

    def __init__(self, category, message, status=None, retryable=False):
        super().__init__(message)
        self.category = category
        self.status = status
        self.retryable = retryable


class Deadline:
    """Time budget for one poll, so a slow API cannot hold the feed lock for minutes."""

    def __init__(self, seconds=POLL_BUDGET_SECONDS, clock=time.monotonic):
        self._clock = clock
        self._end = clock() + seconds

    def remaining(self):
        return self._end - self._clock()


@dataclass(frozen=True)
class FileMeta:
    """One entry of /api/v1/hfmetar/files. `modified_at` is when the SNAPSHOT changed — never an observation time."""
    name: str
    hour_start: datetime
    size: int = None
    modified_at: str = None
    sha256: str = None

    @property
    def version(self):
        """Identity of this snapshot's contents. None means the server gave us nothing to compare."""
        if self.size is None and self.modified_at is None:
            return None
        return f"{self.size}|{self.modified_at}"


def hour_from_name(name):
    m = _HOUR_RE.search(name or "")
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def parse_listing(payload):
    """JSON from /files -> {hour_start: FileMeta}. When several files map to one hour, the newest wins."""
    items = payload.get("files") if isinstance(payload, dict) else payload
    if not isinstance(items, list):
        raise LdmError("bad_listing", "the /files response has no 'files' list")
    by_hour = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        name = item.get("name") or item.get("filename")
        hour = hour_from_name(name) if isinstance(name, str) else None
        if hour is None or not _NAME_RE.match(name):
            continue
        size = item.get("size_bytes", item.get("size"))
        meta = FileMeta(
            name=name, hour_start=hour,
            size=size if isinstance(size, int) and not isinstance(size, bool) else None,
            modified_at=str(item["modified_at"]) if item.get("modified_at") else None,
            sha256=str(item["sha256"]).lower() if item.get("sha256") else None)
        old = by_hour.get(hour)
        if old is None or _newer(meta, old):
            by_hour[hour] = meta
    return by_hour


def _newer(a, b):
    ta, tb = parse_utc(a.modified_at), parse_utc(b.modified_at)
    if ta is None or tb is None:
        return ta is not None or tb is None      # unknown loses to known; ties keep the later listing entry
    return ta >= tb


# ── the API ─────────────────────────────────────────────────────────────────

class LdmApi:
    """Authenticated, retrying client. One keep-alive session, so polls do not repeat the TLS handshake."""

    def __init__(self, config, session=None, sleep=time.sleep):
        self.config = config
        self._session = session
        self._sleep = sleep

    # -- plumbing -----------------------------------------------------------
    def _http(self):
        if self._session is None:
            self._session = requests.Session()
        return self._session

    def _redact(self, text):
        text = str(text)
        token = self.config.token
        if token:
            text = text.replace(token, "***")
        return re.sub(r"(?i)bearer\s+\S+", "Bearer ***", text)

    def _request(self, path, deadline, authenticated=True):
        url = self.config.base_url + path
        headers = {"User-Agent": cfg.HTTP_USER_AGENT, "Cache-Control": "no-cache"}
        if authenticated:
            headers["Authorization"] = f"Bearer {self.config.token}"
        attempts = self.config.max_retries + 1
        for attempt in range(attempts):
            remaining = deadline.remaining()
            if remaining <= 0:
                raise LdmError("timeout", f"poll time budget exhausted before {path}")
            retry_after = None
            try:
                resp = self._http().get(url, headers=headers, timeout=min(self.config.timeout, remaining))
            except requests.exceptions.SSLError as exc:
                raise LdmError("tls", self._redact(f"TLS failure for {path}: {type(exc).__name__}")) from None
            except requests.exceptions.Timeout:
                err = LdmError("timeout", f"timeout after {self.config.timeout:g}s for {path}", retryable=True)
            except requests.exceptions.RequestException as exc:
                err = LdmError("connection", self._redact(f"connection problem for {path}: {type(exc).__name__}"),
                               retryable=True)
            else:
                status = resp.status_code
                if status == 200:
                    return resp
                if status in (401, 403):
                    raise LdmError("auth", f"HTTP {status}: the LDM API rejected the token (check LDM_API_TOKEN)",
                                   status=status)
                if status == 404:
                    raise LdmError("not_found", f"HTTP 404 for {path}", status=status)
                if status == 429:
                    retry_after = _retry_after(resp)
                    err = LdmError("rate_limited", f"HTTP 429 (rate limited) for {path}", status=status, retryable=True)
                elif 500 <= status <= 599:
                    err = LdmError("server_error", f"HTTP {status} from the LDM API for {path}", status=status,
                                   retryable=True)
                else:
                    raise LdmError("http_error", f"unexpected HTTP {status} for {path}", status=status)
            if not err.retryable or attempt == attempts - 1:
                raise err
            wait = RETRY_BACKOFF_SECONDS[min(attempt, len(RETRY_BACKOFF_SECONDS) - 1)]
            if retry_after is not None:
                if retry_after > MAX_RETRY_AFTER_SECONDS:
                    raise err                         # asked to wait too long: let the next poll try again
                wait = max(wait, retry_after)
            if wait >= deadline.remaining():
                raise err
            self._sleep(wait)
        raise LdmError("connection", f"no response for {path}")   # not reachable; keeps the function total

    # -- endpoints ----------------------------------------------------------
    def list_files(self, deadline):
        """-> ({hour_start: FileMeta}, duration_ms)"""
        started = time.perf_counter()
        resp = self._request("/api/v1/hfmetar/files", deadline)
        try:
            payload = resp.json()
        except ValueError:
            raise LdmError("bad_listing", "the /files response is not valid JSON") from None
        return parse_listing(payload), round((time.perf_counter() - started) * 1000, 1)

    def download(self, meta, deadline):
        """-> (gzip bytes, duration_ms, sha256 hex). Raises LdmError for any failure or an unusable body."""
        started = time.perf_counter()
        resp = self._request(f"/api/v1/hfmetar/{meta.name}", deadline)
        content = resp.content
        if not content:
            raise LdmError("empty_response", f"{meta.name}: empty response body", retryable=False)
        declared = (getattr(resp, "headers", None) or {}).get("Content-Length")
        if declared and str(declared).isdigit() and int(declared) != len(content):
            raise LdmError("truncated", f"{meta.name}: got {len(content)} bytes, expected {declared}")
        digest = hashlib.sha256(content).hexdigest()
        if meta.sha256 and meta.sha256 != digest:
            raise LdmError("checksum", f"{meta.name}: SHA-256 does not match the server's value")
        return content, round((time.perf_counter() - started) * 1000, 1), digest

    def health(self):
        """Unauthenticated liveness probe for diagnostics. Never raises."""
        started = time.perf_counter()
        try:
            self._request("/health", Deadline(self.config.timeout), authenticated=False)
            return {"ok": True, "duration_ms": round((time.perf_counter() - started) * 1000, 1)}
        except LdmError as exc:
            return {"ok": False, "category": exc.category, "error": self._redact(exc)}


def _retry_after(resp):
    raw = (getattr(resp, "headers", None) or {}).get("Retry-After")
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return None
