"""HTTP API for reading a stock's insider (Form 4) transactions.

``GET /stocks/ticker/{ticker}/insider-transactions`` — the read endpoint for the
insider-transactions slice: a stock's recent SEC Form 4 buys and sells, newest first, each
flagged as an open-market purchase (``P``) / sale (``S``) vs. the compensation/mechanics noise a
Form 4 also reports, with a net buy-vs-sell ``summary``. ``?open_market_only=true`` narrows the
feed to just the P/S conviction trades (the summary always reflects the full open-market rollup).
Grouped under the ``/stocks/ticker/{ticker}`` resource (like the ticker card and analyst-info),
since it's a per-ticker card the FE renders. Controller + presenter + wiring, the
composition-root way, sitting in ``app/stocks/endpoints/``.

Wiring mirrors the revenue-segments read path, with one divergence: the DB cache is **TTL-based**
(``INSIDER_CACHE_TTL_HOURS``, default 24). This slice has no out-of-band cron, so the TTL is what
keeps a stock's feed current — it self-refreshes on read once the stored rows age past the TTL,
rather than being kept fresh by a background sweep. The process-singleton live SEC provider is
memoized with ``@lru_cache`` while the DB cache is built per request (it needs the request
session). EDGAR needs no credential, so the endpoint is always wired; a cold cache for a stock
with no recent insider activity just yields an empty list (best-effort).
"""

import os
from datetime import timedelta
from functools import lru_cache

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.orm import Session

from app.db import get_db
from app.stocks.adapters.db_cached_insider_transactions_adapter import (
    DbCachedInsiderTransactionsProvider,
)
from app.stocks.adapters.sec_edgar_insider_transactions_adapter import (
    SecEdgarInsiderTransactionsProvider,
)
from app.stocks.exceptions import StockDataUnavailable, StockNotFound
from app.stocks.insider_transactions.db_repository import (
    SqlInsiderTransactionsRepository,
)
from app.stocks.insider_transactions.entities import (
    InsiderActivity,
    InsiderTransaction,
)
from app.stocks.insider_transactions.ports import InsiderTransactionsProvider
from app.stocks.insider_transactions.schemas import (
    InsiderActivityResponse,
    InsiderSummaryResponse,
    InsiderTransactionResponse,
)
from app.stocks.insider_transactions.use_cases import GetInsiderTransactions

router = APIRouter(tags=["insider-transactions"])

# Production pacing for the live SEC provider: the read path only fetches on a cold/stale miss
# (a handful of sequential EDGAR document reads), so a small per-request spacing keeps even a
# burst of misses under EDGAR's ~10 req/s fair-use ceiling.
_SEC_MIN_REQUEST_INTERVAL = 0.15

# How long a stock's stored feed is served before the read path re-fetches it (this slice has no
# cron, so the TTL is the freshness mechanism). Overridable via env; falls back on a bad value.
_DEFAULT_CACHE_TTL_HOURS = 24.0


@lru_cache(maxsize=1)
def _sec_insider_provider() -> InsiderTransactionsProvider:
    # One process-singleton live provider (no key; it caches the ticker->CIK map across calls);
    # the TTL DB cache that wraps it is built per request, since it needs the request session.
    return SecEdgarInsiderTransactionsProvider(
        min_request_interval_seconds=_SEC_MIN_REQUEST_INTERVAL
    )


def _cache_ttl() -> timedelta:
    raw = os.environ.get("INSIDER_CACHE_TTL_HOURS")
    try:
        hours = float(raw) if raw is not None else _DEFAULT_CACHE_TTL_HOURS
    except ValueError:
        hours = _DEFAULT_CACHE_TTL_HOURS
    if hours <= 0:
        hours = _DEFAULT_CACHE_TTL_HOURS
    return timedelta(hours=hours)


def get_insider_transactions_provider(
    db: Session = Depends(get_db),
) -> InsiderTransactionsProvider:
    # A TTL read-through DB cache sits in front of EDGAR so the endpoint rarely walks the filings;
    # past the TTL it re-fetches on read (no cron keeps it warm). SEC needs no key, so this is
    # always wired.
    return DbCachedInsiderTransactionsProvider(
        _sec_insider_provider(),
        SqlInsiderTransactionsRepository(db),
        ttl=_cache_ttl(),
    )


def get_insider_transactions_use_case(
    provider: InsiderTransactionsProvider = Depends(get_insider_transactions_provider),
) -> GetInsiderTransactions:
    return GetInsiderTransactions(provider)


def _present_transaction(txn: InsiderTransaction) -> InsiderTransactionResponse:
    return InsiderTransactionResponse(
        filing_date=txn.filing_date,
        transaction_date=txn.transaction_date,
        insider_name=txn.insider_name,
        role=txn.role,
        security_title=txn.security_title,
        transaction_code=txn.transaction_code,
        code_label=txn.code_label,
        acquired_disposed=txn.acquired_disposed,
        is_open_market=txn.is_open_market,
        is_open_market_buy=txn.is_open_market_buy,
        is_open_market_sale=txn.is_open_market_sale,
        shares=txn.shares,
        price_per_share=txn.price_per_share,
        value=txn.value,
        shares_owned_following=txn.shares_owned_following,
    )


def _present(
    activity: InsiderActivity, *, open_market_only: bool
) -> InsiderActivityResponse:
    """Presenter: insider-activity entity -> HTTP response DTO. The ``summary`` always reflects
    the full open-market rollup; ``open_market_only`` only narrows the transaction list."""
    summary = activity.summary
    txns = activity.open_market if open_market_only else activity.transactions
    return InsiderActivityResponse(
        symbol=activity.symbol,
        count=len(txns),
        summary=InsiderSummaryResponse(
            open_market_buy_count=summary.open_market_buy_count,
            open_market_sell_count=summary.open_market_sell_count,
            open_market_buy_value=summary.open_market_buy_value,
            open_market_sell_value=summary.open_market_sell_value,
            net_value=summary.net_value,
        ),
        transactions=[_present_transaction(t) for t in txns],
    )


@router.get(
    "/stocks/ticker/{ticker}/insider-transactions",
    response_model=InsiderActivityResponse,
)
def get_insider_transactions_endpoint(
    ticker: str,
    response: Response,
    open_market_only: bool = Query(
        False,
        description="Return only the open-market buys and sells (transaction codes P/S), "
        "dropping the grant/exercise/tax/gift transactions a Form 4 also reports.",
    ),
    use_case: GetInsiderTransactions = Depends(get_insider_transactions_use_case),
) -> InsiderActivityResponse:
    try:
        activity = use_case.execute(ticker)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except StockNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except StockDataUnavailable as exc:
        raise HTTPException(502, str(exc)) from exc
    # Insider filings trickle in and the feed is served from the DB cache, so cache briefly: a
    # burst of viewers collapses onto one response.
    response.headers["Cache-Control"] = "public, max-age=300"
    return _present(activity, open_market_only=open_market_only)
