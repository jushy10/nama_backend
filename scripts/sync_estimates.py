"""Refresh stored forward analyst estimates from FMP (stale-first, quota-capped).

The stock endpoint fills a symbol's estimates lazily the first time it's viewed (see
``DbCachedAnalystEstimatesProvider``); this job keeps the rows already stored fresh,
so users see current consensus without anyone's request having to wait on a refresh.

FMP's free tier allows only ~250 calls/day — far fewer than the ~600 index
constituents — so a full-universe sweep isn't possible in one run. Instead this
refreshes only rows already in ``stock_analyst_estimates``, oldest-fetched first, up
to ``SYNC_ESTIMATES_LIMIT`` per run (default 200). Combined with lazy-fill, the
symbols people actually look at stay current while staying under quota. Run monthly
(see .github/workflows/sync-estimates.yml):

    export FMP_API_KEY=...                          # free key from financialmodelingprep.com
    export DATABASE_URL=postgresql+psycopg://...    # omit for local sqlite:///./nama.db
    export SYNC_ESTIMATES_LIMIT=200                 # optional; rows to refresh per run
    alembic upgrade head                            # create the tables (once per DB)
    python scripts/sync_estimates.py

Needs the app installed (it writes through the app's SQLAlchemy models) and the
`stocks` / `stock_analyst_estimates` tables to exist (created by `alembic upgrade head`).
"""

from __future__ import annotations

import os

from sqlalchemy import select

from app.db import SessionLocal
from app.stocks.exceptions import StockDataUnavailable, StockNotFound
from app.stocks.fmp_estimates_provider import FmpEstimatesProvider
from app.stocks.stock_estimates_repository import (
    SqlAnalystEstimatesRepository,
    StockAnalystEstimatesRecord,
    StockRecord,
)

_DEFAULT_LIMIT = 200


def _api_key() -> str:
    key = os.environ.get("FMP_API_KEY")
    if not key:
        raise SystemExit(
            "FMP_API_KEY is not set. Get a free key at financialmodelingprep.com, "
            "then `export FMP_API_KEY=...` before running."
        )
    return key


def _limit() -> int:
    """How many rows to refresh this run. Capped to stay under FMP's daily quota."""
    raw = os.environ.get("SYNC_ESTIMATES_LIMIT")
    if not raw:
        return _DEFAULT_LIMIT
    try:
        return max(1, int(raw))
    except ValueError:
        raise SystemExit(f"SYNC_ESTIMATES_LIMIT must be an integer, got {raw!r}")


def main() -> None:
    key = _api_key()
    limit = _limit()
    provider = FmpEstimatesProvider(key)
    refreshed = 0
    failed = 0

    with SessionLocal() as session:
        repo = SqlAnalystEstimatesRepository(session)
        # Oldest-fetched first, so each capped run renews the stalest rows; symbols
        # never viewed (hence never stored) are filled lazily by the endpoint.
        targets = session.execute(
            select(StockRecord.symbol, StockRecord.name)
            .join(
                StockAnalystEstimatesRecord,
                StockAnalystEstimatesRecord.stock_id == StockRecord.id,
            )
            .order_by(StockAnalystEstimatesRecord.fetched_at.asc())
            .limit(limit)
        ).all()

        for symbol, name in targets:
            try:
                estimates = provider.get_estimates(symbol)
            except (StockNotFound, StockDataUnavailable) as exc:
                failed += 1
                print(f"  ! {symbol}: {exc}")
                continue
            # Re-stamp even an empty result so an uncovered symbol isn't retried
            # every run; pass the stored name through so it isn't lost.
            repo.upsert(symbol, name, estimates)
            refreshed += 1

    print(
        f"Refreshed {refreshed} estimate rows ({failed} failed) "
        f"-> stock_analyst_estimates (cap {limit})."
    )


if __name__ == "__main__":
    main()
