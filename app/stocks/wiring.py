import os
from datetime import timedelta
from functools import lru_cache

from fastapi import Depends, HTTPException
from sqlalchemy.orm import Session

from app.db import get_db
from app.stocks.adapters.alpaca.price_adapter_impl import PriceAdapterImpl as AlpacaPriceAdapterImpl
from app.stocks.adapters.db.analyst_estimates_adapter_impl import (
    AnalystEstimatesAdapterImpl,
)
from app.stocks.adapters.market_routing.price_adapter_impl import PriceAdapterImpl as MarketRoutingPriceAdapterImpl
from app.stocks.adapters.yfinance.price_adapter_impl import PriceAdapterImpl as YahooPriceAdapterImpl
from app.stocks.adapters.yfinance.option_chain_adapter_impl import OptionChainAdapterImpl
from app.stocks.company.earnings.annual.annual_earnings_repository_adapter_impl import AnnualEarningsRepositoryAdapterImpl
from app.stocks.interfaces import AnalystEstimatesAdapter


@lru_cache(maxsize=1)
def get_provider() -> AlpacaPriceAdapterImpl:
    key = os.environ.get("APCA_API_KEY_ID")
    secret = os.environ.get("APCA_API_SECRET_KEY")
    if not key or not secret:
        raise HTTPException(
            503, "Stock data is not configured (APCA_API_KEY_ID / APCA_API_SECRET_KEY)."
        )
    return AlpacaPriceAdapterImpl(key, secret)


@lru_cache(maxsize=1)
def get_yahoo_price_provider() -> YahooPriceAdapterImpl:
    # The Canadian (TSX/TSXV) price feed — keyless Yahoo via yfinance, like the earnings
    # timelines' live source. Always constructable (no key gate); best-effort at read.
    return YahooPriceAdapterImpl()


def get_price_provider() -> MarketRoutingPriceAdapterImpl:
    # The per-symbol price provider the ticker card and charts ride: routes a US symbol to
    # Alpaca (real-time, the primary market) and a Canadian-suffixed one (.TO/.V/…) to the
    # keyless Yahoo feed. A US-only deployment still needs the Alpaca keys (get_provider's 503
    # gate), so wiring the US leg keeps that hard requirement — the CA leg is always available.
    # Not @lru_cache'd: get_provider raises 503 without keys, and caching must not freeze that.
    return MarketRoutingPriceAdapterImpl(
        us=get_provider(), ca=get_yahoo_price_provider()
    )


@lru_cache(maxsize=1)
def get_options_provider() -> OptionChainAdapterImpl:
    # The ticker card's options read comes from Yahoo via yfinance — keyless,
    # like the earnings timelines' live source, so there's no key gate here at
    # all. Best-effort enrichment: a blocked Yahoo call leaves the block null
    # rather than sinking the card, so the provider is always wired.
    return OptionChainAdapterImpl()


def get_estimates_provider(
    db: Session = Depends(get_db),
) -> AnalystEstimatesAdapter:
    # Forward analyst estimates back the AI analysis context — best-effort
    # enrichment. They're projected from the
    # annual-earnings slice's stored forward years (the same Yahoo consensus that
    # timeline serves), DB-only: a symbol whose timeline isn't cached yet just
    # omits the forward metrics until the annual read path or its cron fills the
    # rows. No second table, fetch, or cron.
    return AnalystEstimatesAdapterImpl(AnnualEarningsRepositoryAdapterImpl(db))


# Per-kind default TTL for a stored AI analysis (minutes) — each tuned to how
# often that analysis's *input* data changes, so a stored read is served that long
# before it's regenerated. Override one kind via ANALYSIS_CACHE_TTL_MINUTES_<KIND>,
# or pin every kind at once with the global ANALYSIS_CACHE_TTL_MINUTES.
_ANALYSIS_TTL_DEFAULT_MINUTES = {
    "earnings": 720,       # ~quarterly reports; DB refreshed by a daily cron
    "ratings": 360,        # analyst actions; DB refreshed by a daily cron
    "etf": 360,            # profile ~quarterly rebalance; only the quote is live
    "stock": 240,          # slow inputs + a live-price valuation slice
    "fundamentals": 240,   # same shape as the stock scorecard
    "sector": 30,          # intraday leaders; ~zero token cost (one shared row)
    "market": 60,          # trailing-window narrative; only the day-move is fast
}
_ANALYSIS_TTL_FALLBACK_MINUTES = 30  # any kind not in the map above


def bedrock_recovery_model_id(specific_env: str | None = None) -> str | None:
    if specific_env:
        override = os.environ.get(specific_env)
        if override:
            return override
    return os.environ.get("BEDROCK_RECOVERY_MODEL_ID") or None


def analysis_cache_ttl(kind: str) -> timedelta:
    # How long a stored `kind` analysis is served before it's regenerated. The default per
    # kind reflects how often that analysis's input data changes (see the map above); a
    # per-kind env override wins if set (`ANALYSIS_CACHE_TTL_MINUTES_<KIND>`, e.g.
    # ANALYSIS_CACHE_TTL_MINUTES_EARNINGS), else a global ANALYSIS_CACHE_TTL_MINUTES pins
    # every kind at once, else the map default. A malformed value is skipped, not raised.
    default = _ANALYSIS_TTL_DEFAULT_MINUTES.get(kind, _ANALYSIS_TTL_FALLBACK_MINUTES)
    for var in (f"ANALYSIS_CACHE_TTL_MINUTES_{kind.upper()}", "ANALYSIS_CACHE_TTL_MINUTES"):
        raw = os.environ.get(var)
        if raw:
            try:
                return timedelta(minutes=float(raw))
            except ValueError:
                continue
    return timedelta(minutes=default)
