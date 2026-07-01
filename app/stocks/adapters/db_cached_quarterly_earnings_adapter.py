"""Interface Adapter: a database cache in front of any QuarterlyEarningsProvider.

Backed by a ``QuarterlyEarningsRepository``, this cache is shared across every app
instance and survives restarts, so the endpoint calls Yahoo only when a symbol is missing
or its stored rows have aged out. Out of band, the quarterly-earnings cron endpoint
refreshes the stored rows. It implements ``QuarterlyEarningsProvider``, so it slots into
the wiring exactly where the bare yfinance provider would, with the use case none the
wiser.

Resilience is the point of going through the DB:

- A cache *read* failure (DB hiccup) is swallowed and treated as a miss, so a database
  problem never sinks the (best-effort) earnings — the request falls through to the live
  source.
- A cache *write* failure is swallowed too: the caller still gets the fresh timeline.
- When the rows are stale *and* the live refresh fails (Yahoo outage / block), the stored
  rows are served rather than nothing. Only a miss with no rows to fall back on propagates.
- A live result that comes back *empty* never overwrites stored history — a transient
  empty from Yahoo would otherwise wipe good quarters — so we keep serving what we hold.
"""

import logging
from datetime import datetime, timedelta, timezone

from app.stocks.earnings.quarterly.entities import QuarterlyEarningsTimeline
from app.stocks.earnings.quarterly.ports import QuarterlyEarningsProvider
from app.stocks.earnings.quarterly.repository import QuarterlyEarningsRepository
from app.stocks.exceptions import StockDataUnavailable, StockNotFound

logger = logging.getLogger(__name__)


def _as_utc(moment: datetime) -> datetime:
    """Treat a naive stored timestamp as UTC, so the staleness check never trips on a
    tz-aware/naive comparison (SQLite hands datetimes back without a zone)."""
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=timezone.utc)


class DbCachedQuarterlyEarningsProvider(QuarterlyEarningsProvider):
    """Wraps a QuarterlyEarningsProvider with a persistent, DB-backed cache."""

    # Rows older than this are refreshed on access. A week keeps upcoming report dates
    # and freshly-reported quarters current without hammering Yahoo; the cron sync
    # normally keeps rows fresher than this, and a symbol the sync doesn't cover still
    # self-refreshes about weekly whenever it's viewed.
    _DEFAULT_MAX_AGE = timedelta(days=7)

    def __init__(
        self,
        inner: QuarterlyEarningsProvider,
        repo: QuarterlyEarningsRepository,
        *,
        max_age: timedelta = _DEFAULT_MAX_AGE,
        now=None,
    ) -> None:
        self._inner = inner
        self._repo = repo
        self._max_age = max_age
        self._now = now or (lambda: datetime.now(timezone.utc))

    def get_quarterly_earnings(self, symbol: str) -> QuarterlyEarningsTimeline:
        cached = self._safe_get(symbol)
        if cached is not None and self._fresh(cached.fetched_at):
            return cached.timeline
        # Stale or missing → refresh from the live source. If that fails but we held
        # stored rows, serve them rather than nothing; only a true miss propagates.
        try:
            timeline = self._inner.get_quarterly_earnings(symbol)
        except (StockNotFound, StockDataUnavailable):
            if cached is not None:
                logger.warning(
                    "serving stale quarterly earnings for %s (live refresh failed)", symbol
                )
                return cached.timeline
            raise
        # Never let a transient empty result wipe good history: keep the stored rows if
        # we have them; only pass through the empty when there's nothing cached.
        if timeline.is_empty:
            if cached is not None:
                logger.warning(
                    "live quarterly earnings empty for %s; serving cached rows", symbol
                )
                return cached.timeline
            return timeline
        self._safe_upsert(symbol, timeline)
        return timeline

    def _fresh(self, fetched_at: datetime) -> bool:
        return self._now() - _as_utc(fetched_at) < self._max_age

    def _safe_get(self, symbol: str):
        # A cache read must never break the (best-effort) earnings: on any error, treat
        # it as a miss and let the caller fall through to the live source.
        try:
            return self._repo.get(symbol)
        except Exception:  # noqa: BLE001 — cache resilience, not error handling
            logger.warning(
                "quarterly earnings cache read failed for %s", symbol, exc_info=True
            )
            return None

    def _safe_upsert(self, symbol: str, timeline: QuarterlyEarningsTimeline) -> None:
        # Caching is best-effort; a write failure must not fail the request the caller
        # already has a good answer for. (Name comes from the sync job, not this feed,
        # so it's left untouched here.)
        try:
            self._repo.upsert(symbol, None, timeline)
        except Exception:  # noqa: BLE001 — cache resilience, not error handling
            logger.warning(
                "quarterly earnings cache write failed for %s", symbol, exc_info=True
            )
