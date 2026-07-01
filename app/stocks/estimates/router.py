"""Composition root for the analyst-estimates read path.

Builds the provider the stock snapshot reads forward estimates through: the live FMP
adapter wrapped in the persistent DB cache. It lives here, beside the slice, so the
main stocks router just imports ``get_estimates_provider`` and wires it onto
``GetStockInfo`` — the estimates feature owns how its own provider is assembled.

Mirrors the wiring conventions in ``app/stocks/router.py``: credentials come from the
environment, providers are built lazily so the app boots without the key, and the
process-singleton HTTP client is memoized with ``@lru_cache`` while the DB cache is
built per request (it needs the request session).
"""

import os
from functools import lru_cache

from fastapi import Depends
from sqlalchemy.orm import Session

from app.db import get_db
from app.stocks.adapters.db_cached_estimates_adapter import (
    DbCachedAnalystEstimatesProvider,
)
from app.stocks.adapters.fmp_estimates_adapter import FmpEstimatesProvider
from app.stocks.estimates.estimates_ports import AnalystEstimatesProvider
from app.stocks.estimates.stock_estimates_repository import (
    SqlAnalystEstimatesRepository,
)


@lru_cache(maxsize=1)
def _fmp_estimates_provider() -> AnalystEstimatesProvider | None:
    # The live FMP client is a process singleton (one httpx connection pool); the
    # DB cache that wraps it is built per request, since it needs the request session.
    key = os.environ.get("FMP_API_KEY")
    return FmpEstimatesProvider(key) if key else None


def get_estimates_provider(
    db: Session = Depends(get_db),
) -> AnalystEstimatesProvider | None:
    # Forward analyst estimates back the snapshot's forward P/E — best-effort
    # enrichment, so without a key we simply omit the forward metrics (price +
    # trailing ratios still serve). A persistent DB cache (refreshed out of band by
    # the estimates cron endpoint + lazily on a miss) sits in front of FMP so the
    # endpoint rarely calls it, staying under the ~250/day free quota — and it serves
    # a stale row if FMP is down. Same FMP key the profile + constituents use.
    inner = _fmp_estimates_provider()
    if inner is None:
        return None
    return DbCachedAnalystEstimatesProvider(inner, SqlAnalystEstimatesRepository(db))
