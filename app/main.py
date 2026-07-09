"""A lightweight FastAPI backend backed by SQLite."""

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from starlette.requests import Request

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
from app.stocks.endpoints.cron_news_endpoints import router as news_cron_router
from app.stocks.endpoints.quarterly_earnings_endpoints import (
    router as quarterly_earnings_router,
)
from app.stocks.endpoints.recommendations_endpoints import (
    router as recommendations_router,
)
from app.stocks.endpoints.rating_changes_endpoints import (
    router as rating_changes_router,
)
from app.stocks.endpoints.news_endpoints import router as news_router
from app.stocks.endpoints.cron_universe_endpoints import (
    router as universe_cron_router,
)
from app.stocks.endpoints.cron_index_membership_endpoints import (
    router as index_membership_cron_router,
)
from app.stocks.endpoints.cron_etf_endpoints import router as etf_cron_router
from app.stocks.endpoints.etf_endpoints import router as etf_router
from app.stocks.endpoints.ticker_endpoints import router as ticker_router
from app.stocks.endpoints.heatmap_endpoints import router as heatmap_router
from app.stocks.router import router as stocks_router

# The web server (uvicorn/gunicorn) installs handlers only on its own `uvicorn*`
# loggers and leaves the root logger at its default WARNING level, so an app-level
# `logger.info(...)` — e.g. the sector-analysis timing line — is filtered out before
# it is ever emitted. Install a root stream handler and raise just our own `app`
# logger tree to INFO: our INFO lines reach CloudWatch without turning on the noisy
# INFO chatter of third-party libraries (botocore, httpx, yfinance). Root records
# only gate what's logged *to* root; a child's INFO record still propagates to the
# root handler regardless of root's level. This mirrors the `logging.basicConfig`
# call in app/sync/__main__.py that does the same for the `python -m app.sync` tasks.
logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logging.getLogger("app").setLevel(logging.INFO)

# Browser origins allowed to call this API (cross-origin). Comma-separated env
# var so prod and local dev differ without a code change; defaults to the
# namainsights site. Without this, a browser on namainsights.com is blocked.
_DEFAULT_ORIGINS = "https://namainsights.com,https://www.namainsights.com"
CORS_ALLOW_ORIGINS = [
    origin.strip()
    for origin in os.environ.get("CORS_ALLOW_ORIGINS", _DEFAULT_ORIGINS).split(",")
    if origin.strip()
]


def _client_ip(request: Request) -> str:
    """Identify the caller for rate limiting.

    Behind the API Gateway VPC link the socket peer is the gateway's ENI — the
    same address for every caller — so keying on ``request.client.host`` would
    lump all traffic into one bucket. The real client IP arrives in the
    ``X-Client-IP`` header, which the gateway *overwrites* with the observed
    source IP (see the integration's request_parameters in infra), so it's
    trustworthy and can't be spoofed by a client-supplied header. (It's a custom
    header rather than X-Forwarded-For because API Gateway v2 forbids mapping
    operations on XFF.)

    The X-Forwarded-For fallback covers running without the gateway in front
    (local dev, tests); off the gateway there's nothing to overwrite the header,
    so treat it as untrusted best-effort keying, not a security boundary.
    """
    stamped = request.headers.get("x-client-ip")
    if stamped:
        return stamped.strip()
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "anonymous"


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Database schema is owned by Alembic migrations (`alembic upgrade head`),
    # not created here — deploys manage the database explicitly, so there's
    # nothing to do on startup for now.
    yield


app = FastAPI(title="nama_backend", lifespan=lifespan)

# Per-client (per-IP) rate limiting so one abusive caller can't exhaust the
# service — a token bucket per client IP; over it, SlowAPI raises
# RateLimitExceeded and the handler returns HTTP 429. The counter is in-process,
# which is exactly right while we run a single task; if desired_count ever goes
# above 1, point the Limiter at Redis via storage_uri so the count is shared.
# These limits sit under API Gateway's global 50 req/s throttle: that caps total
# load/cost, this stops any single IP from consuming it. Tune as traffic grows.
limiter = Limiter(key_func=_client_ip, default_limits=["20/second", "600/minute"])
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(SlowAPIMiddleware)

# CORS is added last so it stays the outermost middleware: a 429 from the limiter
# above still gets CORS headers, so a browser can read the response instead of
# reporting an opaque cross-origin failure.
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
# The analyst rating-changes read endpoint (GET /stocks/{symbol}/rating-changes): the
# sell-side's individual upgrade/downgrade actions, newest first, served from the DB cache
# over yfinance. See app/stocks/endpoints/rating_changes_endpoints.py.
app.include_router(rating_changes_router)
# The news read endpoint (GET /stocks/{symbol}/news): the stock's recent headlines
# (title/publisher/link/published time), served from the DB cache over yfinance. See
# app/stocks/endpoints/news_endpoints.py.
app.include_router(news_router)
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
# The news refresh cron endpoint (POST /internal/news/sync); it drives the SyncStockNews
# use case out of band (yfinance -> DB), seeding + refreshing each stock's recent
# headlines. See app/stocks/endpoints/cron_news_endpoints.py.
app.include_router(news_cron_router)
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
# The ETF read endpoints (GET /stocks/etfs — a paginated search/filter/sort over the screened
# top-US-ETF set: name/ticker substring, a category/type filter, sort by net assets/AUM or
# expense ratio; and GET /stocks/etfs/categories — the distinct category slugs for the FE's
# filter menu), served from the slice's own `etfs` table. See app/stocks/endpoints/etf_endpoints.py.
app.include_router(etf_router)
# The ETF refresh cron endpoint (POST /internal/etfs/sync); it drives the SyncEtfs use case out
# of band (yfinance ETF screen, US funds with AUM >= $1B -> etfs table, then per-ticker category
# enrichment).
# Fire-and-forget like the other crons (202 + background thread). See
# app/stocks/endpoints/cron_etf_endpoints.py.
app.include_router(etf_cron_router)
# The market heat map (GET /market/heatmap): a Finviz-style treemap of an index (S&P 500 /
# Nasdaq-100) — every stock a tile sized by market cap and coloured by the day's change, grouped
# sector -> industry -> stock. Structure + size come from the screened universe on the `stocks`
# anchor; the colours are best-effort live Alpaca quotes. See
# app/stocks/endpoints/heatmap_endpoints.py.
app.include_router(heatmap_router)


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}
