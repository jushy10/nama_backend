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
from app.stocks.endpoints.cron_fundamentals_endpoints import (
    router as fundamentals_cron_router,
)
from app.stocks.endpoints.cron_performance_endpoints import (
    router as stock_performance_cron_router,
)
from app.stocks.endpoints.quarterly_earnings_endpoints import (
    router as quarterly_earnings_router,
)
from app.stocks.endpoints.analyst_endpoints import router as analyst_router
from app.stocks.endpoints.news_endpoints import router as news_router
from app.stocks.endpoints.cron_universe_endpoints import (
    router as universe_cron_router,
)
from app.stocks.endpoints.cron_index_membership_endpoints import (
    router as index_membership_cron_router,
)
from app.stocks.endpoints.cron_etf_endpoints import router as etf_cron_router
from app.stocks.endpoints.etf_endpoints import router as etf_router
from app.stocks.endpoints.revenue_segments_endpoints import (
    router as revenue_segments_router,
)
from app.stocks.endpoints.cron_revenue_segments_endpoints import (
    router as revenue_segments_cron_router,
)
from app.stocks.endpoints.insider_transactions_endpoints import (
    router as insider_transactions_router,
)
from app.stocks.endpoints.cron_insider_transactions_endpoints import (
    router as insider_transactions_cron_router,
)
from app.stocks.endpoints.institutional_ownership_endpoints import (
    router as institutional_ownership_router,
)
from app.stocks.endpoints.cron_institutional_ownership_endpoints import (
    router as institutional_ownership_cron_router,
)
from app.stocks.endpoints.ticker_endpoints import router as ticker_router
from app.stocks.endpoints.heatmap_endpoints import router as heatmap_router
from app.stocks.endpoints.analysis_endpoints import router as analysis_router
from app.stocks.endpoints.chart_endpoints import router as chart_router
from app.stocks.endpoints.logo_endpoints import router as logo_router
from app.stocks.endpoints.market_endpoints import router as market_router
from app.stocks.endpoints.market_brief_endpoints import router as market_brief_router
from app.stocks.endpoints.cron_market_brief_endpoints import (
    router as market_brief_cron_router,
)
from app.stocks.endpoints.earnings_calendar_endpoints import (
    router as earnings_calendar_router,
)
from app.stocks.endpoints.seo_endpoints import router as seo_router

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
# RateLimitExceeded and the handler returns HTTP 429. These limits sit under API
# Gateway's global throttle: that caps total load/cost, this stops any single IP
# from consuming it. Tune as traffic grows.
#
# The counter defaults to in-process ("memory://"), which is exact for a single
# task. Under autoscaling the service can run several tasks, and an in-process
# counter is then per-task — a single IP can reach up to (task count) * the limit,
# with the API Gateway throttle as the hard global backstop. Set
# RATE_LIMIT_STORAGE_URI to a shared store (e.g. redis://host:6379) to make the
# count exact across tasks; it's a one-env-var flip, no code change.
_rate_limit_storage = os.environ.get("RATE_LIMIT_STORAGE_URI", "memory://")
limiter = Limiter(
    key_func=_client_ip,
    default_limits=["20/second", "600/minute"],
    storage_uri=_rate_limit_storage,
)
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
# The gen-1 flat stocks router was dissolved into per-slice endpoint modules:
# charts (candles/EMA/support levels), the market boards (/sectors), every
# AI-analysis read (per-stock / earnings / ratings / sector / market summary),
# and the logo image.
app.include_router(chart_router)
app.include_router(market_router)
app.include_router(analysis_router)
app.include_router(logo_router)
# The per-quarter earnings read endpoint (GET /stocks/{symbol}/earnings/quarterly):
# recent reported quarters + upcoming ones, served from the DB cache over yfinance. See
# app/stocks/endpoints/quarterly_earnings_endpoints.py.
app.include_router(quarterly_earnings_router)
# The per-year (annual) earnings read endpoint (GET /stocks/{symbol}/earnings/annual):
# recent reported fiscal years + upcoming estimated ones, served from the DB cache over
# yfinance. See app/stocks/endpoints/annual_earnings_endpoints.py.
app.include_router(annual_earnings_router)
# The analyst-info read endpoint (GET /stocks/ticker/{ticker}/analyst-info): a stock's full
# analyst coverage in one payload — the sell-side buy/hold/sell trends by month, the consensus
# price target, and the discrete upgrade/downgrade events — served from the DB cache over
# yfinance. Consolidates the former /recommendations + /rating-changes reads. See
# app/stocks/endpoints/analyst_endpoints.py.
app.include_router(analyst_router)
# The news read endpoint (GET /stocks/{symbol}/news): the stock's recent headlines
# (title/publisher/link/published time), served from the DB cache over yfinance. See
# app/stocks/endpoints/news_endpoints.py.
app.include_router(news_router)
# The revenue-segments read endpoint (GET /stocks/{symbol}/revenue-segments): a company's
# revenue broken down by operating segment, product/service line, and geography — parsed from
# its latest 10-K on SEC EDGAR and served from the DB cache. See
# app/stocks/endpoints/revenue_segments_endpoints.py.
app.include_router(revenue_segments_router)
# The insider-transactions read endpoint (GET /stocks/ticker/{ticker}/insider-transactions): a stock's
# recent SEC Form 4 buys and sells — open-market purchases/sales flagged apart from the
# grant/exercise/tax noise, with a net buy-vs-sell summary. Served from a read-through DB cache
# over SEC EDGAR, kept warm by the weekly sync cron. See
# app/stocks/endpoints/insider_transactions_endpoints.py.
app.include_router(insider_transactions_router)
# The institutional-ownership read endpoint (GET /stocks/ticker/{ticker}/institutional-ownership): a
# stock's top 13F holders (institutions + funds) with each one's quarter-over-quarter position change
# (the "big money buys and sells"), the "institutions own X%" breakdown, and a net buy-vs-sell flow.
# Served from the DB cache over yfinance. See
# app/stocks/endpoints/institutional_ownership_endpoints.py.
app.include_router(institutional_ownership_router)
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
# market cap always; dividend/performance/metrics (trailing P/E + margins + trailing YoY
# growth) as ?include= opt-ins, computed per request from the live quote + stored facts —
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
# The fundamentals refresh cron endpoint (POST /internal/fundamentals/sync); it drives the
# SyncFundamentals use case out of band (yfinance .info -> stocks anchor), seeding + refreshing
# each stock's trailing margins/ROE/liquidity/leverage/beta + the per-share P/B / P/S / dividend
# inputs. See app/stocks/endpoints/cron_fundamentals_endpoints.py.
app.include_router(fundamentals_cron_router)
# The stock-performance refresh cron endpoint (POST /internal/performance/sync); it drives the
# SyncStockPerformance use case out of band (Alpaca daily bars -> stocks anchor), materializing
# each screened stock's trailing-window returns (1W..1Y, YTD) so the heat map reads them DB-only
# instead of recomputing a year of bars per index on every request. See
# app/stocks/endpoints/cron_performance_endpoints.py.
app.include_router(stock_performance_cron_router)
# The institutional-ownership refresh cron endpoint (POST /internal/institutional-ownership/sync); it
# drives the SyncInstitutionalOwnership use case out of band (yfinance 13F holders -> DB), seeding +
# refreshing each stock's top institutional/mutual-fund holders and the ownership breakdown. See
# app/stocks/endpoints/cron_institutional_ownership_endpoints.py.
app.include_router(institutional_ownership_cron_router)
# The revenue-segments refresh cron endpoint (POST /internal/revenue-segments/sync); it drives
# the SyncRevenueSegments use case out of band (SEC EDGAR 10-K -> DB), seeding + refreshing each
# stock's revenue disaggregation. See app/stocks/endpoints/cron_revenue_segments_endpoints.py.
app.include_router(revenue_segments_cron_router)
# The insider-transactions refresh cron endpoint (POST /internal/insider-transactions/sync); it
# drives the SyncInsiderTransactions use case out of band (SEC EDGAR Form 4 -> DB), seeding +
# refreshing each stock's recent insider buys/sells. Weekly; the read cache is plain read-through
# (no TTL) so a synced stock is served from the DB and never walks the filings in a user request.
# See app/stocks/endpoints/cron_insider_transactions_endpoints.py.
app.include_router(insider_transactions_cron_router)
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
# The daily market brief (GET /market/brief + /market/brief/{date}): a once-a-day, AI-written
# plain-language read of the whole US market (headline indices + sector rotation + the day's
# movers), stored one row per date and served DB-only. Generated out of band by the
# market-brief cron. See app/stocks/endpoints/market_brief_endpoints.py.
app.include_router(market_brief_router)
# The market-brief generation cron (POST /internal/market-brief/sync): gathers the day's
# whole-market reads, asks the model for a brief, and upserts today's row. Fire-and-forget like
# the other crons. See app/stocks/endpoints/cron_market_brief_endpoints.py.
app.include_router(market_brief_cron_router)
# The market-wide earnings calendar (GET /market/earnings-calendar?from=&to=): which companies
# are scheduled to report on which upcoming days, aggregated across the universe from the
# scheduled dates the quarterly-earnings sync stores, grouped by day. Table-less DB read. See
# app/stocks/endpoints/earnings_calendar_endpoints.py.
app.include_router(earnings_calendar_router)
# The SEO / server-rendered content pages (GET /stock/{ticker}): public, crawlable HTML
# per stock, rendered server-side from DB-only anchor facts so search AND AI crawlers that
# don't run JavaScript see real content (the React app can't give them that). A singular
# /stock/ prefix keeps it clear of the /stocks/ (plural) JSON API. See
# app/stocks/endpoints/seo_endpoints.py and app/stocks/seo/README.md.
app.include_router(seo_router)


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}
