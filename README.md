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
└── test_stocks_provider.py  # Alpaca adapter tests (offline, faked SDK)
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
curl localhost:8080/stocks/AAPL
```

Uses Alpaca's free **IEX** feed. Without the Alpaca keys the endpoint returns
`503`; without `FINNHUB_API_KEY` it still serves price + performance, just with
market cap and dividend omitted. The rest of the app runs regardless.

### Secrets in AWS

Store the keys the same way as `DATABASE_URL`: as **SSM SecureString**
parameters (e.g. `/nama/dev/alpaca-api-key-id`, `/nama/dev/alpaca-api-secret-key`)
via the [`ssm-parameter`](infra/modules/ssm-parameter) module, and inject them
into the ECS task as `APCA_API_KEY_ID` / `APCA_API_SECRET_KEY`. The optional
`FINNHUB_API_KEY` follows the same pattern (e.g. `/nama/dev/finnhub-api-key`).
Never commit keys to the repo.

## Contributing

`main` is protected — push to a feature branch and open a pull request.
