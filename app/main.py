"""A lightweight FastAPI backend backed by SQLite."""

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.stocks.endpoints.annual_earnings_endpoints import (
    router as annual_earnings_router,
)
from app.stocks.endpoints.cron_annual_earnings_endpoints import (
    router as annual_earnings_cron_router,
)
from app.stocks.endpoints.cron_quarterly_earnings_endpoints import (
    router as quarterly_earnings_cron_router,
)
from app.stocks.endpoints.cron_recommendations_endpoints import (
    router as recommendations_cron_router,
)
from app.stocks.endpoints.quarterly_earnings_endpoints import (
    router as quarterly_earnings_router,
)
from app.stocks.endpoints.recommendations_endpoints import (
    router as recommendations_router,
)
from app.stocks.endpoints.cron_universe_endpoints import (
    router as universe_cron_router,
)
from app.stocks.endpoints.ticker_endpoints import router as ticker_router
from app.stocks.endpoints.universe_search_endpoints import (
    router as universe_search_router,
)
from app.stocks.router import router as stocks_router

# Browser origins allowed to call this API (cross-origin). Comma-separated env
# var so prod and local dev differ without a code change; defaults to the
# namainsights site. Without this, a browser on namainsights.com is blocked.
_DEFAULT_ORIGINS = "https://namainsights.com,https://www.namainsights.com"
CORS_ALLOW_ORIGINS = [
    origin.strip()
    for origin in os.environ.get("CORS_ALLOW_ORIGINS", _DEFAULT_ORIGINS).split(",")
    if origin.strip()
]


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Database schema is owned by Alembic migrations (`alembic upgrade head`),
    # not created here — deploys manage the database explicitly, so there's
    # nothing to do on startup for now.
    yield


app = FastAPI(title="nama_backend", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ALLOW_ORIGINS,
    allow_methods=["*"],  # lets the OPTIONS preflight succeed instead of 405
    allow_headers=["*"],
)
app.include_router(stocks_router)
# The per-quarter earnings read endpoint (GET /stocks/{symbol}/earnings/quarterly):
# recent reported quarters + upcoming ones, served from the DB cache over yfinance. See
# app/stocks/endpoints/quarterly_earnings_endpoints.py.
app.include_router(quarterly_earnings_router)
# The per-year (annual) earnings read endpoint (GET /stocks/{symbol}/earnings/annual):
# recent reported fiscal years + upcoming estimated ones, served from the DB cache over
# yfinance. See app/stocks/endpoints/annual_earnings_endpoints.py.
app.include_router(annual_earnings_router)
# The analyst-recommendations read endpoint (GET /stocks/{symbol}/recommendations): the
# sell-side buy/hold/sell split by month, served from the DB cache over yfinance. See
# app/stocks/endpoints/recommendations_endpoints.py.
app.include_router(recommendations_router)
# The quarterly-earnings refresh cron endpoint (POST /internal/earnings/quarterly/sync);
# it drives the SyncQuarterlyEarnings use case out of band. See
# app/stocks/endpoints/cron_quarterly_earnings_endpoints.py.
app.include_router(quarterly_earnings_cron_router)
# The annual-earnings refresh cron endpoint (POST /internal/earnings/annual/sync); it
# drives the SyncAnnualEarnings use case out of band. See
# app/stocks/endpoints/cron_annual_earnings_endpoints.py.
app.include_router(annual_earnings_cron_router)
# The ticker-card read endpoint (GET /stocks/ticker/{ticker}): live quote + day move,
# name, exchange (DB-backed, learned once) and market cap always; dividend,
# performance and metrics (trailing PEG + margins + forward PEG) as ?include=
# opt-ins — computed per request from the live quote + the stored annual-earnings
# consensus (no table or cron of its own). See
# app/stocks/endpoints/ticker_endpoints.py.
app.include_router(ticker_router)
# The recommendations refresh cron endpoint (POST /internal/recommendations/sync); it
# drives the SyncRecommendations use case out of band. See
# app/stocks/endpoints/cron_recommendations_endpoints.py.
app.include_router(recommendations_cron_router)
# The stock-search read endpoint (GET /stocks/search): find companies in the screened
# ≥$5B universe by ticker or name, largest market cap first — the app's only discovery
# route. See app/stocks/endpoints/universe_search_endpoints.py.
app.include_router(universe_search_router)
# The universe refresh cron endpoint (POST /internal/universe/sync); it drives the
# SyncUniverse use case out of band (Nasdaq screen -> DB). See
# app/stocks/endpoints/cron_universe_endpoints.py.
app.include_router(universe_cron_router)


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}
