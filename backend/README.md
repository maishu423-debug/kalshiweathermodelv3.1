# Kalshi Clone Backend

Small Django API wrapper around Kalshi public market data.

## Run

```powershell
..\kalshibot\Scripts\python.exe manage.py runserver 8000
```

## Endpoints

```txt
GET /api/kalshi/health/
GET /api/kalshi/miami-temperature/
GET /api/kalshi/miami-temperature/?market_ticker=KXHIGHMIA-26JUN15-B93.5
GET /api/kalshi/miami-temperature/stream/
GET /api/kalshi/paper/state/
POST /api/kalshi/paper/order/
POST /api/kalshi/paper/reset/
GET  /api/kalshi/autotrader/state/    (paper account, positions, trades, bot cycles, schedule)
POST /api/kalshi/autotrader/toggle/   {"enabled": true|false}
POST /api/kalshi/autotrader/reset/    (restore $10,000, clear positions/trades/bot log)
```

## Kalshi WebSocket credentials

Copy `.env.example` to `.env` and fill in your Kalshi API key id and private key path.

```powershell
Copy-Item .env.example .env
```

For fast KMIA current temperature and recorded high, also set:

```env
SYNOPTIC_TOKEN=your_synoptic_token
SYNOPTIC_STATION=KMIA
CRON_REFRESH_SECRET=choose-a-long-random-string
```

Do not put Kalshi credentials in the frontend.

## Paper-trading bot

Started with the other background tasks (`RUN_BACKGROUND_TASKS`, single worker). Every 15 minutes
from 12:30 PM to 3:30 PM ET it buys 100 contracts of YES on the highest-probability bucket and
100 contracts of NO on each live bucket the model rates below 5% (`kalshi_api/algorithm.py`,
`kalshi_api/autotrader.py`). It never sells; positions settle from Kalshi's resolved result.
Starting balance is $10,000.
