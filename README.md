# nama_backend

A very lightweight Python **FastAPI** backend backed by **SQLite**.

## Layout

```
app/
├── db.py       # SQLite engine, session, Base, get_db dependency
├── main.py     # FastAPI app and endpoints
└── stocks/     # Alpaca stock-info feature (clean-architecture vertical slice)
tests/
├── test_stocks.py           # stock entity/use-case/API tests (offline)
├── test_stocks_provider.py  # Alpaca adapter tests (offline, faked SDK)
└── test_constituents.py     # screener universe: repo mapping + data sanity
scripts/
└── build_constituents.py    # regenerate the screener's index-membership file
```

## Setup

```sh
python -m venv .venv
# Windows PowerShell:  .venv\Scripts\Activate.ps1
# macOS/Linux:         source .venv/bin/activate
pip install -e ".[dev]"
```

## Run

```sh
uvicorn app.main:app --reload
```

Creates a local `nama.db` on first run. Interactive docs at
<http://localhost:8080/docs>.

## Endpoints

| Method | Path          | Description      |
| ------ | ------------- | ---------------- |
| GET    | `/healthz`    | Liveness check   |
| GET    | `/stocks/{symbol}` | Stock info from Alpaca (e.g. `AAPL`) |
| GET    | `/stocks/{symbol}/logo` | Company logo image |
| GET    | `/stocks/{symbol}/candles` | OHLC candlestick chart data |
| GET    | `/stocks/{symbol}/earnings` | Quarterly earnings surprises (beat history) |
| GET    | `/stocks/screener` | Day's biggest gainers & losers, filter by index + sector |

## Test

```sh
pytest
```

Tests run against an in-memory SQLite database — no setup, no files.

## Database

The app picks its backend from the `DATABASE_URL` environment variable. Unset →
local SQLite (`sqlite:///./nama.db`). To run on PostgreSQL (e.g. the RDS instance
in [`infra/`](infra/README.md)):

```sh
pip install -e ".[postgres]"   # adds the psycopg driver
export DATABASE_URL="postgresql+psycopg://USER:PASSWORD@HOST:5432/nama?sslmode=require"
```

Tests ignore `DATABASE_URL` and always use in-memory SQLite, so they stay fast.

## Stocks (Alpaca)

`GET /stocks/{symbol}` returns a snapshot for a ticker, fetched from Alpaca via
the official [`alpaca-py`](https://alpaca.markets/sdks/python/) SDK. It's a
self-contained **clean-architecture vertical slice** under
[`app/stocks/`](app/stocks/): the use case depends on a `StockDataProvider`
port, and only `alpaca_provider.py` knows Alpaca exists — so the tests run fully
offline with a fake provider.

The response also carries best-effort enrichment: a **performance** object of
trailing price returns (`1w`, `1m`, `3m`, `6m`, `ytd`, `1y`) computed from
Alpaca daily bars, plus **market cap** and **dividend** (`dividend_per_share`,
`dividend_yield`) from [Finnhub](https://finnhub.io). These never fail the
request — if a source is down, unkeyed, or doesn't cover the symbol, that field
comes back `null` and the price still returns.

Credentials come from the environment (like `DATABASE_URL`):

```sh
export APCA_API_KEY_ID=...
export APCA_API_SECRET_KEY=...
export FINNHUB_API_KEY=...        # optional: enables market cap + dividend
export LOGODEV_TOKEN=...          # required for /logo: publishable key from logo.dev
curl localhost:8080/stocks/AAPL
```

Uses Alpaca's free **IEX** feed. Without the Alpaca keys the endpoint returns
`503`; without `FINNHUB_API_KEY` it still serves price + performance, just with
market cap and dividend omitted. The rest of the app runs regardless.

### Candlestick chart data

`GET /stocks/{symbol}/candles` returns OHLC candles for drawing a candlestick
chart (the green/red bars on a stock page). Each candle carries a `direction`
(`up`/`down`) for colouring and a `time` field in **UNIX epoch seconds**, the
format charting libraries such as [TradingView Lightweight
Charts](https://www.tradingview.com/lightweight-charts/) expect.

Query parameters:

| Param       | Values | Default | Notes |
| ----------- | ------ | ------- | ----- |
| `timeframe` | `1Min` `5Min` `15Min` `30Min` `1Hour` `4Hour` `1Day` `1Week` `1Month` | `1Day` | Granularity of each candle. |
| `range`     | `1D` `5D` `1M` `3M` `6M` `1Y` `2Y` `5Y` `YTD` `MAX` | `6M` | How far back to fetch. |
| `start`     | ISO 8601 datetime | – | Explicit window start (UTC); overrides `range`. |
| `end`       | ISO 8601 datetime | now | Explicit window end (UTC). |

Pick a `timeframe` for zoom level and a `range` (or an explicit `start`/`end`
window) for how much history to load. Candles come back oldest-first, split-
adjusted, and capped at the 10,000 most recent bars in the window.

```sh
# Last 6 months, daily candles (defaults)
curl localhost:8080/stocks/AAPL/candles

# Last 5 trading days, hourly candles
curl "localhost:8080/stocks/AAPL/candles?timeframe=1Hour&range=5D"

# An explicit window
curl "localhost:8080/stocks/AAPL/candles?start=2026-01-01T00:00:00Z&end=2026-02-01T00:00:00Z"
```

### Company logo

`GET /stocks/{symbol}/logo` returns the company logo as an image, sourced from
[Logo.dev](https://logo.dev) keyed by ticker. Logo.dev resolves to the *current*
logo through mergers, rebrands, and symbol changes, so the image stays up to date
rather than going stale. Only [`logodev_provider.py`](app/stocks/logodev_provider.py)
knows the source exists — swap that one adapter and nothing else changes.

It needs a free **publishable** token (logo.dev, 500k requests/month, no card).
The token is publishable by design — it rides in the request URL — so it isn't a
secret like the Alpaca keys, but it's still injected via `LOGODEV_TOKEN`. Without
it the `/logo` endpoint returns `503`; the rest of the app runs regardless. An
unknown ticker returns `404` (we request `fallback=404` so logo.dev 404s instead
of serving a monogram placeholder).

```sh
export LOGODEV_TOKEN=pk_...
curl localhost:8080/stocks/AAPL/logo --output aapl.png
```

### Earnings beat history

`GET /stocks/{symbol}/earnings` returns recent **quarterly earnings surprises** —
the reported EPS against the consensus estimate going into each quarter, newest
first — answering "does the company beat estimates consistently?". Each quarter
carries a `beat` flag (`actual >= estimate`, met counts as beat) and a
`surprise_percent`; the top level summarises with `beats`, `scored` (quarters
with both an actual and an estimate) and `beat_rate` (percent of scored quarters
that beat). Sourced from [Finnhub](https://finnhub.io)'s free `/stock/earnings`.

Unlike market cap and dividend (best-effort enrichment on `/stocks/{symbol}`),
this is the endpoint's primary data, so it needs `FINNHUB_API_KEY`: without it
the endpoint returns `503`, an unknown symbol returns `404`.

```sh
# Last 4 quarters (default)
curl localhost:8080/stocks/AAPL/earnings

# Last 12 quarters
curl "localhost:8080/stocks/AAPL/earnings?limit=12"
```

### Stock screener

`GET /stocks/screener` ranks a whole index's move on the day and returns the
biggest **gainers** and **losers** together, so a "top/bottom movers" board is a
single request. Narrow the field with `index` (`sp500` / `nasdaq100`) and/or
`sector` (a GICS sector, case-insensitive); omit both to screen the entire known
universe.

| Param    | Values | Default | Notes |
| -------- | ------ | ------- | ----- |
| `index`  | `sp500` `nasdaq100` | – (all) | Limit the universe to an index. |
| `sector` | a GICS sector, e.g. `Information Technology`, `Health Care`, `Energy` | – (all) | Case-insensitive. |
| `limit`  | `1`–`50` | `10` | How many names per side (gainers and losers). |

The universe — which symbols belong to each index, and each one's GICS sector —
is **static reference data** baked into the package at
[`app/stocks/data/constituents.json`](app/stocks/data/constituents.json), since
the live market-data feed (Alpaca) doesn't expose index membership. It's
generated from **Financial Modeling Prep**'s index-constituent endpoints — one
call per index, each returning symbol + name + sector — by
[`scripts/build_constituents.py`](scripts/build_constituents.py), with sectors
normalized to GICS. It's a build-time tool, so the key is **not** needed at
runtime. Regenerate when the indices reconstitute (~quarterly):

```sh
export FMP_API_KEY=...   # free key from financialmodelingprep.com
python scripts/build_constituents.py
```

The day's move for each name comes from a best-effort batch of Alpaca snapshots
(the same IEX feed as `/stocks/{symbol}`), so it needs the Alpaca keys (`503`
without them). Names the feed can't price are left out of the ranking;
`universe_count` (how many matched the filter) and `quoted_count` (how many could
be ranked) report the coverage. A symbol never appears as both a gainer and a
loser, and the board is briefly cached (`Cache-Control: max-age=15`).

```sh
# Top/bottom 10 across every known name
curl localhost:8080/stocks/screener

# Nasdaq-100 information-technology names, 5 per side
curl "localhost:8080/stocks/screener?index=nasdaq100&sector=Information%20Technology&limit=5"
```

### Secrets in AWS

Store the keys the same way as `DATABASE_URL`: as **SSM SecureString**
parameters (e.g. `/nama/dev/alpaca-api-key-id`, `/nama/dev/alpaca-api-secret-key`)
via the [`ssm-parameter`](infra/modules/ssm-parameter) module, and inject them
into the ECS task as `APCA_API_KEY_ID` / `APCA_API_SECRET_KEY`. The optional
`FINNHUB_API_KEY` and the `LOGODEV_TOKEN` follow the same pattern (e.g.
`/nama/dev/finnhub-api-key`, `/nama/dev/logodev-token`). Never commit keys to the
repo.

The screener's `FMP_API_KEY` is stored the same way (`/nama/dev/fmp-api-key`,
via the [`ssm-secret`](infra/modules/ssm-secret) module) but is **build-time
only** — [`scripts/build_constituents.py`](scripts/build_constituents.py) reads
it to regenerate the committed universe, so it is *not* injected into the running
ECS task. Fetch it from SSM when regenerating:

```sh
export FMP_API_KEY=$(aws ssm get-parameter --name /nama/dev/fmp-api-key \
  --with-decryption --query Parameter.Value --output text)
python scripts/build_constituents.py
```

## Contributing

`main` is protected — push to a feature branch and open a pull request.
