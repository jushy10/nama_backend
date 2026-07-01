"""Composition root for the analyst-estimates read path.

Builds the provider the stock snapshot reads forward estimates through: the live
yfinance (Yahoo) adapter wrapped in the persistent DB cache. It lives here, beside the
slice, so the main stocks router just imports ``get_estimates_provider`` and wires it
onto ``GetStockInfo`` — the estimates feature owns how its own provider is assembled.

Mirrors the wiring conventions in ``app/stocks/router.py``: the process-singleton live
provider is memoized with ``@lru_cache`` while the DB cache is built per request (it
needs the request session). Unlike the vendors that need a key, yfinance reads Yahoo's
public data with no credential, so estimates are always wired — a cold cache on a host
Yahoo blocks just yields no estimates (best-effort), it never fails the app boot.
"""

from functools import lru_cache

from fastapi import Depends
from sqlalchemy.orm import Session

from app.db import get_db
from app.stocks.adapters.db_cached_estimates_adapter import (
    DbCachedAnalystEstimatesProvider,
)
from app.stocks.adapters.yfinance_estimates_adapter import YfinanceEstimatesProvider
from app.stocks.estimates.ports import AnalystEstimatesProvider
from app.stocks.estimates.db_repository import SqlAnalystEstimatesRepository


@lru_cache(maxsize=1)
def _yfinance_estimates_provider() -> AnalystEstimatesProvider:
    # One process-singleton live provider (no key, no connection pool to share); the
    # DB cache that wraps it is built per request, since it needs the request session.
    return YfinanceEstimatesProvider()


def get_estimates_provider(
    db: Session = Depends(get_db),
) -> AnalystEstimatesProvider:
    # Forward analyst estimates back the snapshot's forward P/E — best-effort
    # enrichment. A persistent DB cache (refreshed out of band by the estimates cron
    # endpoint + lazily on a miss) sits in front of Yahoo so the endpoint rarely calls
    # it — Yahoo rate-limits, so the fewer live hits the better — and it serves a stale
    # row if the live refresh fails. yfinance needs no key, so this is always wired.
    return DbCachedAnalystEstimatesProvider(
        _yfinance_estimates_provider(), SqlAnalystEstimatesRepository(db)
    )
