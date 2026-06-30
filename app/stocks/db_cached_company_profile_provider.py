"""Interface Adapter: a database cache in front of any CompanyProfileProvider.

The persistent sibling of ``CachingCompanyProfileProvider`` (which is per-process,
in-memory). Backed by a ``CompanyProfileRepository``, this cache is shared across
every app instance and survives restarts, so the endpoint calls the profile vendors
(Finnhub for the name, FMP for the description) only when a symbol is missing or its
stored row has aged out — keeping well under FMP's ~250-calls/day free quota. A
company's name and description barely change, so the freshness window is long.

Resilience mirrors the estimates cache:

- A cache *read* failure (DB hiccup) is swallowed and treated as a miss, so a database
  problem never sinks the best-effort profile — the request falls through to live.
- A cache *write* failure is swallowed too: the caller still gets the fresh profile.
- The wrapped composite returns an all-``None`` profile (rather than raising) when both
  vendors miss or fail. So an empty refresh is *not* cached, and when a stored row
  exists the stale-but-real profile is served over the empty one — a transient vendor
  blip doesn't blank a known name/description. Only a hard miss with nothing stored
  returns the empty profile (or propagates an outright error).

It implements ``CompanyProfileProvider``, so it slots into the wiring exactly where
the composite used to, with the use case none the wiser.
"""

import logging
from datetime import datetime, timedelta, timezone

from app.stocks.entities import CompanyProfile
from app.stocks.exceptions import StockDataUnavailable, StockNotFound
from app.stocks.ports import CompanyProfileProvider, CompanyProfileRepository

logger = logging.getLogger(__name__)


def _as_utc(moment: datetime) -> datetime:
    """Treat a naive stored timestamp as UTC (SQLite drops the zone) so the staleness
    check never trips on a tz-aware/naive comparison."""
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=timezone.utc)


def _has_content(profile: CompanyProfile) -> bool:
    """Whether a profile carries anything worth caching — an all-``None`` result is a
    miss/outage, not data, so it isn't stored."""
    return profile.name is not None or profile.description is not None


class DbCachedCompanyProfileProvider(CompanyProfileProvider):
    """Wraps a CompanyProfileProvider with a persistent, DB-backed cache."""

    # Company profiles are near-immutable, so a stored row is refreshed only every few
    # months on access; the out-of-band sync keeps them fresher than that.
    _DEFAULT_MAX_AGE = timedelta(days=180)

    def __init__(
        self,
        inner: CompanyProfileProvider,
        repo: CompanyProfileRepository,
        *,
        max_age: timedelta = _DEFAULT_MAX_AGE,
        now=None,
    ) -> None:
        self._inner = inner
        self._repo = repo
        self._max_age = max_age
        self._now = now or (lambda: datetime.now(timezone.utc))

    def get_profile(self, symbol: str) -> CompanyProfile:
        cached = self._safe_get(symbol)
        if cached is not None and self._fresh(cached.fetched_at):
            return cached.profile
        # Stale or missing → refresh from the live source.
        try:
            profile = self._inner.get_profile(symbol)
        except (StockNotFound, StockDataUnavailable):
            if cached is not None:
                logger.warning("serving stale profile for %s (live refresh failed)", symbol)
                return cached.profile
            raise
        if _has_content(profile):
            self._safe_upsert(symbol, profile)
            return profile
        # Empty refresh: keep the last real profile if we have one, rather than
        # caching or returning a blank.
        return cached.profile if cached is not None else profile

    def _fresh(self, fetched_at: datetime) -> bool:
        return self._now() - _as_utc(fetched_at) < self._max_age

    def _safe_get(self, symbol: str):
        # A cache read must never break the (best-effort) profile: on any error, treat
        # it as a miss and let the caller fall through to the live source.
        try:
            return self._repo.get(symbol)
        except Exception:  # noqa: BLE001 — cache resilience, not error handling
            logger.warning("profile cache read failed for %s", symbol, exc_info=True)
            return None

    def _safe_upsert(self, symbol: str, profile: CompanyProfile) -> None:
        # Caching is best-effort; a write failure must not fail the request the caller
        # already has a good answer for.
        try:
            self._repo.upsert(symbol, profile)
        except Exception:  # noqa: BLE001 — cache resilience, not error handling
            logger.warning("profile cache write failed for %s", symbol, exc_info=True)
