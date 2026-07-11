"""Shared dependency wiring for the stocks feature.

The factories every endpoint module reuses: the Alpaca price feed (the one
process-singleton the whole slice's price views ride on), the Finnhub
enrichment providers, the yfinance options chain, the DB-projected analyst
estimates, and the analysis result-cache TTL. Slice-specific wiring (a
Bedrock analyser, the logo vendor) lives in that slice's endpoint module —
this file holds only what is genuinely shared across endpoint modules, so
none of them ever has to import another's router.

Credentials are read from the environment (like DATABASE_URL in app/db.py).
Providers are built lazily so the app still boots without keys — the error
only surfaces when an endpoint is actually called.
"""

import os
from datetime import timedelta
from functools import lru_cache

from fastapi import Depends, HTTPException
from sqlalchemy.orm import Session

from app.db import get_db
from app.stocks.adapters.alpaca_adapter import AlpacaStockDataProvider
from app.stocks.adapters.annual_earnings_estimates_adapter import (
    AnnualEarningsEstimatesProvider,
)
from app.stocks.adapters.caching_company_profile_adapter import (
    CachingCompanyProfileProvider,
)
from app.stocks.adapters.finnhub_company_profile_adapter import (
    FinnhubCompanyProfileProvider,
)
from app.stocks.adapters.finnhub_fundamentals_adapter import (
    FinnhubFundamentalsProvider,
)
from app.stocks.adapters.yfinance_options_adapter import YfinanceOptionChainProvider
from app.stocks.earnings.annual.db_repository import SqlAnnualEarningsRepository
from app.stocks.ports import (
    AnalystEstimatesProvider,
    CompanyProfileProvider,
    StockFundamentalsProvider,
)


@lru_cache(maxsize=1)
def get_provider() -> AlpacaStockDataProvider:
    key = os.environ.get("APCA_API_KEY_ID")
    secret = os.environ.get("APCA_API_SECRET_KEY")
    if not key or not secret:
        raise HTTPException(
            503, "Stock data is not configured (APCA_API_KEY_ID / APCA_API_SECRET_KEY)."
        )
    return AlpacaStockDataProvider(key, secret)


@lru_cache(maxsize=1)
def get_fundamentals_provider() -> StockFundamentalsProvider | None:
    # Best-effort enrichment: without a key we simply omit market cap + dividend
    # (price + performance still serve). Free key from finnhub.io.
    key = os.environ.get("FINNHUB_API_KEY")
    return FinnhubFundamentalsProvider(key) if key else None


@lru_cache(maxsize=1)
def get_profile_provider() -> CompanyProfileProvider | None:
    # The clean display name comes from Finnhub's free profile endpoint — best-effort
    # enrichment, so without a key we simply omit the name override (the price feed's
    # legal title still serves). Wrapped in a TTL cache (a singleton, so it persists
    # across requests) to stay under Finnhub's per-minute rate limit; the name is
    # near-static.
    finnhub_key = os.environ.get("FINNHUB_API_KEY")
    if not finnhub_key:
        return None
    return CachingCompanyProfileProvider(FinnhubCompanyProfileProvider(finnhub_key))


@lru_cache(maxsize=1)
def get_options_provider() -> YfinanceOptionChainProvider:
    # The ticker card's options read comes from Yahoo via yfinance — keyless,
    # like the earnings timelines' live source, so there's no key gate here at
    # all. Best-effort enrichment: a blocked Yahoo call leaves the block null
    # rather than sinking the card, so the provider is always wired.
    return YfinanceOptionChainProvider()


def get_estimates_provider(
    db: Session = Depends(get_db),
) -> AnalystEstimatesProvider:
    # Forward analyst estimates back the AI analysis context — best-effort
    # enrichment. They're projected from the
    # annual-earnings slice's stored forward years (the same Yahoo consensus that
    # timeline serves), DB-only: a symbol whose timeline isn't cached yet just
    # omits the forward metrics until the annual read path or its cron fills the
    # rows. No second table, fetch, or cron.
    return AnnualEarningsEstimatesProvider(SqlAnnualEarningsRepository(db))


def analysis_cache_ttl() -> timedelta:
    # How long a stored analysis is served before it's regenerated. Config with a
    # sane default (30 min) — an analysis only drifts as its underlying figures do,
    # and every served read carries its own `generated_at` so the age is visible.
    minutes = os.environ.get("ANALYSIS_CACHE_TTL_MINUTES")
    try:
        return timedelta(minutes=float(minutes)) if minutes else timedelta(minutes=30)
    except ValueError:
        return timedelta(minutes=30)
