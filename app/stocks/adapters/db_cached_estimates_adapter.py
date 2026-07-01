"""Interface Adapter: a database cache in front of any AnalystEstimatesProvider.

Persistent, unlike the in-process TTL cache decorators used elsewhere (company
profile, revenue). Backed by an ``AnalystEstimatesRepository``, this cache is shared across
every app instance and survives restarts, so the endpoint reaches Yahoo (via
``yfinance``) only when a symbol is missing or its stored row has aged out. Yahoo
needs no API key and has no hard quota, but it is an unofficial feed that rate-limits
aggressively and blocks many data-centre IPs — keeping live calls rare is what makes
it usable from a hosted box at all. Out of band, a monthly job refreshes the stored
rows.

Resilience is the point of going through the DB:

- A cache *read* failure (DB hiccup) is swallowed and treated as a miss, so a
  database problem never sinks the best-effort estimates — the request just falls
  through to the live source.
- A cache *write* failure is swallowed too: the caller still gets the fresh estimate.
- When the row is stale *and* the live refresh fails (Yahoo rate-limiting or
  blocking the host's IP), the stale row is served rather than nothing — better a
  week-old consensus than a hole in the snapshot. Only a miss with no row to fall
  back on propagates the error.

It implements ``AnalystEstimatesProvider``, so it slots into the wiring exactly where
the bare live provider would, with the use case none the wiser.
"""

import logging
from datetime import datetime, timedelta, timezone

from app.stocks.entities import AnalystEstimates
from app.stocks.exceptions import StockDataUnavailable, StockNotFound
from app.stocks.estimates.ports import AnalystEstimatesProvider
from app.stocks.estimates.repository import AnalystEstimatesRepository

logger = logging.getLogger(__name__)


def _as_utc(moment: datetime) -> datetime:
    """Treat a naive stored timestamp as UTC, so the staleness check never trips on
    a tz-aware/naive comparison (SQLite hands datetimes back without a zone)."""
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=timezone.utc)


class DbCachedAnalystEstimatesProvider(AnalystEstimatesProvider):
    """Wraps an AnalystEstimatesProvider with a persistent, DB-backed cache."""

    # A row older than this is refreshed on access. Set just over a month so the
    # monthly sync normally keeps rows fresh, and a symbol the sync doesn't cover
    # still self-refreshes about monthly whenever it's viewed.
    _DEFAULT_MAX_AGE = timedelta(days=35)

    def __init__(
        self,
        inner: AnalystEstimatesProvider,
        repo: AnalystEstimatesRepository,
        *,
        max_age: timedelta = _DEFAULT_MAX_AGE,
        now=None,
    ) -> None:
        self._inner = inner
        self._repo = repo
        self._max_age = max_age
        self._now = now or (lambda: datetime.now(timezone.utc))

    def get_estimates(self, symbol: str) -> AnalystEstimates:
        cached = self._safe_get(symbol)
        if cached is not None and self._fresh(cached.fetched_at):
            return cached.estimates
        # Stale or missing → refresh from the live source. If that fails but we held
        # a stale row, serve it rather than nothing; only a true miss propagates.
        try:
            estimates = self._inner.get_estimates(symbol)
        except (StockNotFound, StockDataUnavailable):
            if cached is not None:
                logger.warning("serving stale estimates for %s (live refresh failed)", symbol)
                return cached.estimates
            raise
        self._safe_upsert(symbol, estimates)
        return estimates

    def _fresh(self, fetched_at: datetime) -> bool:
        return self._now() - _as_utc(fetched_at) < self._max_age

    def _safe_get(self, symbol: str):
        # A cache read must never break the (best-effort) estimates: on any error,
        # treat it as a miss and let the caller fall through to the live source.
        try:
            return self._repo.get(symbol)
        except Exception:  # noqa: BLE001 — cache resilience, not error handling
            logger.warning("estimates cache read failed for %s", symbol, exc_info=True)
            return None

    def _safe_upsert(self, symbol: str, estimates: AnalystEstimates) -> None:
        # Caching is best-effort; a write failure must not fail the request the
        # caller already has a good answer for. (Name comes from the sync job, not
        # the estimates feed, so it's left untouched here.)
        try:
            self._repo.upsert(symbol, None, estimates)
        except Exception:  # noqa: BLE001 — cache resilience, not error handling
            logger.warning("estimates cache write failed for %s", symbol, exc_info=True)
