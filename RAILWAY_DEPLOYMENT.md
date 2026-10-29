# Railway Hobby deployment

This repository deploys as two Railway services in one project.

## 1. Create the services

Create an empty Railway project and add two services connected to this GitHub
repository:

| Service | Root directory | Config file |
| --- | --- | --- |
| `Backend` | `/backend` | `/backend/railway.json` |
| `Frontend` | `/frontend` | `/frontend/railway.json` |

Set each service's Railway config-file path explicitly. Railway config-file
paths are resolved from the repository root, not from the service root
directory.

Keep the Backend service at one replica. Its SQLite database and background
tasks are intentionally single-instance.

## 2. Add persistent Backend storage

Attach a Railway volume to `Backend` with this mount path:

```text
/data
```

Migrations are part of the Backend start command because Railway volumes are
not mounted in pre-deploy containers.

## 3. Configure Backend variables

Required:

```env
PORT=8000
DJANGO_DEBUG=false
DJANGO_SECRET_KEY=replace-with-a-long-random-value
DJANGO_ALLOWED_HOSTS=localhost,127.0.0.1,.railway.app,.railway.internal,healthcheck.railway.app
DATABASE_PATH=/data/db.sqlite3
RUN_BACKGROUND_TASKS=true
TZ=UTC
```

Weather integrations:

```env
ACCUWEATHER_API_KEY=
SYNOPTIC_TOKEN=
SYNOPTIC_STATION=KMIA
CRON_REFRESH_SECRET=
```

Optional authenticated Kalshi WebSocket access:

```env
KALSHI_API_KEY_ID=
KALSHI_PRIVATE_KEY_PEM=
```

Use `KALSHI_PRIVATE_KEY_PEM`, not a local key path. The value may contain real
newlines or escaped `\n` sequences. Never commit it.

Optional Google Sheets history:

```env
GOOGLE_SERVICE_ACCOUNT_JSON=
GOOGLE_SHEETS_ID=
GOOGLE_TRADES_SHEET_ID=
```

The Google service-account email must have access to the selected sheets.

### Live weather source: private NOAA MADIS LDM API

The live KMIA observations (HFMETAR) can come from NOAA's public files (the original source) or from our private
LDM receiver, served over authenticated HTTPS. Both feed the same decoder, parser, QC and nowcast.

```env
ASOS_SOURCE=ldm                              # ldm | public   (unset = public)
LDM_API_BASE_URL=https://data.getedgefront.com
LDM_API_TOKEN=<secret>
LDM_API_TIMEOUT_SECONDS=15
ASOS_SHADOW_COMPARE=false                    # true = also fetch the other source, compare, record
```

Optional tuning (defaults shown):

```env
LDM_API_MAX_RETRIES=2                        # retries of a transient failure (timeout, 5xx, 429)
LDM_BACKFILL_PUBLIC=true                     # see "Day coverage" below
ASOS_SHADOW_MIN_INTERVAL_SECONDS=60
ASOS_SHADOW_LOG_PATH=                        # e.g. /data/asos_shadow.jsonl to keep every comparison
```

**The token belongs only in the Backend service's environment.** Never put it in a Frontend variable (`NEXT_PUBLIC_*`,
`VITE_*`, ...), the repository, a log or a screenshot. The backend never returns it from any endpoint.

Rules the code enforces:

- `ASOS_SOURCE=ldm` without `LDM_API_TOKEN` is a configuration error. `python manage.py migrate` (part of the start
  command) fails with `weather.E001`, so a bad deploy never goes live. Requests are never sent without the token, and
  there is no silent fallback to the public source.
- Each poll makes one `/api/v1/hfmetar/files` listing request, then downloads only the hourly snapshots whose version
  (size + modified time) changed. The same filename is re-published as NOAA updates it, so the filename alone is not
  trusted. The snapshot's modified time is only a cache key, never an observation time.
- Freshness is judged from the observation's own time (fresh < 15 min, delayed < 25 min, unavailable beyond that),
  never from whether a request succeeded. A failed or invalid download never overwrites the last good observation
  (`asos_last_good.json`).
- **Day coverage:** the recorded daily high needs every hour since Miami midnight. Hours the LDM API no longer holds
  are fetched once from NOAA's public files (`LDM_BACKFILL_PUBLIC=true`, listed in `backfilled_from_public`). The
  current and previous hour are never substituted. With `LDM_BACKFILL_PUBLIC=false`, missing hours are listed in
  `coverage_missing_hours` and the recorded high may be understated, so confirm how many hours the API retains first.

Verify source health (no credentials needed or shown):

```text
https://YOUR-FRONTEND-DOMAIN/api/kalshi/weather/source/          # add ?live=1 to also ping the LDM /health
```

Check `configured_source`, `config_problems` (empty), `last_poll.success`, `last_poll.error_category`,
`latest_observation.age_seconds` and `latest_observation.freshness`. The backend log prints one
`[ASOS-LATENCY]` line per new observation (source, observation time, time received, observation age at receipt,
snapshot filename/version, download and parse milliseconds). With `ASOS_SHADOW_COMPARE=true` it also prints
`[ASOS-SHADOW]` lines and the endpoint adds a `shadow` section (which source is newer, median ages, temperature
differences, errors per source). The shadow only observes: it never changes forecasts or trading.

**Roll back to NOAA's public files** by changing one variable and letting Railway redeploy, no rebuild:

```env
ASOS_SOURCE=public
```

Rollout suggestion: deploy with `ASOS_SOURCE=public` and `ASOS_SHADOW_COMPARE=true` plus the LDM variables, compare for
a day or two on the health endpoint, then set `ASOS_SOURCE=ldm`.

## 4. Configure Frontend variables

```env
PORT=3000
NODE_ENV=production
BACKEND_URL=http://${{Backend.RAILWAY_PRIVATE_DOMAIN}}:${{Backend.PORT}}
```

`Backend` in the reference must exactly match the Backend service name.

Generate a public Railway domain for `Frontend`. The Backend can remain private.

## 5. Verify

The Backend deployment health check is:

```text
/api/kalshi/health/
```

Once the Frontend is public, the same endpoint should be reachable through its
rewrite:

```text
https://YOUR-FRONTEND-DOMAIN/api/kalshi/health/
```

It should return:

```json
{"ok": true}
```

Do not enable Railway Serverless for the Backend: sleeping stops its forecast,
temperature-monitor, and price-tracker threads.

The paper-order, paper-reset, and algorithm POST endpoints do not currently
authenticate users. Do not expose live-money order execution through this
application without first adding authentication and authorization.
