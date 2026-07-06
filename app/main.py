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
from app.stocks.endpoints.cron_index_membership_endpoints import (
    router as index_membership_cron_router,
)
from app.stocks.endpoints.cron_etf_endpoints import router as etf_cron_router
from app.stocks.endpoints.etf_endpoints import router as etf_router
from app.stocks.endpoints.ticker_endpoints import router as ticker_router
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
# The ticker endpoints (app/stocks/endpoints/ticker_endpoints.py): the card
# GET /stocks/ticker/{ticker} — live quote + day move, name, exchange (DB-backed) and
# market cap always; dividend/performance/metrics (trailing PEG + margins + forward PEG)
# as ?include= opt-ins, computed per request from the live quote + stored annual consensus —
# plus the universe read side that shares the resource: GET /stocks/ticker (paginated
# search/filter/sort over the screened `stocks` anchor — name/ticker substring,
# sector/industry, index membership; sort by market cap or trailing growth) and
# GET /stocks/classifications (the distinct sector/industry slugs for the FE's filter menus).
app.include_router(ticker_router)
# The recommendations refresh cron endpoint (POST /internal/recommendations/sync); it
# drives the SyncRecommendations use case out of band. See
# app/stocks/endpoints/cron_recommendations_endpoints.py.
app.include_router(recommendations_cron_router)
# The universe refresh cron endpoint (POST /internal/universe/sync); it drives the
# SyncUniverse use case out of band (yfinance screen -> stocks anchor, then per-ticker
# sector/industry enrichment), populating the stocks table with the ≥$1B US universe.
# Fire-and-forget like the earnings crons (202 + background thread). The read/search
# endpoint over it is deferred. See app/stocks/endpoints/cron_universe_endpoints.py.
app.include_router(universe_cron_router)
# The index-membership refresh cron endpoint (POST /internal/index-membership/sync); it drives
# the SyncIndexMembership use case out of band (Finnhub -> stocks anchor), reconciling the
# in_sp500 / in_nasdaq100 membership flags. See
# app/stocks/endpoints/cron_index_membership_endpoints.py.
app.include_router(index_membership_cron_router)
# The ETF read endpoint (GET /stocks/etfs): a paginated search/filter/sort over the screened
# top-US-ETF set (name/ticker substring; sort by net assets/AUM, YTD return, or expense ratio),
# served from the slice's own `etfs` table. See app/stocks/endpoints/etf_endpoints.py.
app.include_router(etf_router)
# The ETF refresh cron endpoint (POST /internal/etfs/sync); it drives the SyncEtfs use case out
# of band (yfinance top_etfs_us screen -> etfs table). Fire-and-forget like the other crons
# (202 + background thread). See app/stocks/endpoints/cron_etf_endpoints.py.
app.include_router(etf_cron_router)


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}
