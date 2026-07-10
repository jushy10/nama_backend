"""Application use cases for the recommendations slice.

Two actions, both pure orchestration over the ports so they run offline in tests against
hand-written fakes and know nothing of yfinance, HTTP, or SQLAlchemy:

- ``GetStockAnalystInfo`` — the read path behind ``GET /stocks/ticker/{ticker}/analyst-info``.
  Normalizes the symbol once and composes the slice's two ports into one ``AnalystInfo`` read:
  the recommendation trends (+ price targets), primary, plus the rating-change events as
  best-effort enrichment. Both are wired in production as the DB cache over yfinance, so each
  hits Yahoo only on its own cold miss.
- ``SyncRecommendations`` — the out-of-band refresh. Walks the already-stored rows
  least-recently-refreshed first and renews them from the live provider, so users see the
  current month's split without a request ever waiting on a vendor round-trip. Invoked by
  the cron endpoint.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from app.stocks.exceptions import StockDataUnavailable, StockNotFound
from app.stocks.progress import iter_with_progress
from app.stocks.recommendations.entities import (
    AnalystRatingChanges,
    AnalystRecommendations,
    FirmRating,
)
from app.stocks.recommendations.ports import (
    RatingChangeProvider,
    RecommendationProvider,
)
from app.stocks.recommendations.repository import (
    RatingChangesRepository,
    RecommendationsRepository,
    RefreshTarget,
)

logger = logging.getLogger(__name__)


def _normalize_symbol(symbol: str) -> str:
    """Trim/upper-case the ticker and reject obvious junk, once, at the edge of the use
    case — so every layer below sees a clean symbol. Mirrors the stocks slice's guard."""
    normalized = (symbol or "").strip().upper()
    if not normalized:
        raise ValueError("A stock symbol is required.")
    if not normalized.isalpha() or len(normalized) > 5:
        # Simple guard; real tickers are 1-5 letters (ignoring class suffixes).
        raise ValueError(f"'{symbol}' is not a valid stock symbol.")
    return normalized


@dataclass(frozen=True)
class AnalystInfo:
    """A stock's full analyst coverage, bundled for the analyst-info card.

    Groups the slice's two reads behind one result — the recommendation trends (+ the
    consensus price target) and the discrete upgrade/downgrade events. Like the ticker card's
    ``TickerCard``, it's a composite of the slice's shared entities, not a stored entity of its
    own. ``recommendations`` is the primary content; ``rating_changes`` is best-effort, so it
    may be an empty run even when the trends are present. ``top_firms`` is the card's headline
    read of the events — the most credible firms covering the stock and their current stance,
    derived from ``rating_changes`` (empty when none of the covering firms are ranked).
    """

    symbol: str
    recommendations: AnalystRecommendations
    rating_changes: AnalystRatingChanges
    top_firms: tuple[FirmRating, ...] = ()


class GetStockAnalystInfo:
    """Use case: retrieve a stock's full analyst coverage — recommendation trends (+ price
    targets) and rating-change events — in one read, for ``GET
    /stocks/ticker/{ticker}/analyst-info``.

    Composes the slice's two ports the way the ticker card composes several. The recommendation
    trends are **primary** — their failure propagates (an unknown symbol → 404, an upstream
    outage → 502), exactly as the old standalone read did. The rating-change events are
    **best-effort enrichment**: a leg the source can't serve (``StockNotFound`` /
    ``StockDataUnavailable``) degrades to an empty run rather than sinking the card, so the
    events can never take down the trends the user came for. "No coverage" is not an error for
    either — an uncovered symbol is an empty run, presented as a 200. In production both
    providers are the read-through DB cache over yfinance, so each hits Yahoo only on its own
    cold miss.
    """

    def __init__(
        self,
        recommendations: RecommendationProvider,
        rating_changes: RatingChangeProvider,
    ) -> None:
        self._recommendations = recommendations
        self._rating_changes = rating_changes

    def execute(self, symbol: str) -> AnalystInfo:
        symbol = _normalize_symbol(symbol)
        # Trends are primary: their exceptions propagate to the endpoint's error mapping.
        recommendations = self._recommendations.get_recommendations(symbol)
        rating_changes = self._read_rating_changes(symbol)
        return AnalystInfo(
            symbol=symbol,
            recommendations=recommendations,
            rating_changes=rating_changes,
            # The most credible covering firms, derived from the (best-effort) events — an
            # empty tuple when none is ranked, so it never adds a failure mode of its own.
            top_firms=rating_changes.top_credible_firms(),
        )

    def _read_rating_changes(self, symbol: str) -> AnalystRatingChanges:
        """Best-effort: the rating-change events, or an empty run when the source can't serve
        them — enrichment must never sink the primary trends."""
        try:
            return self._rating_changes.get_rating_changes(symbol)
        except (StockNotFound, StockDataUnavailable):
            return AnalystRatingChanges(symbol)


@dataclass(frozen=True)
class RecommendationsSyncReport:
    """The outcome of one refresh run: how many stocks had their trends renewed, how many
    also had rating changes stored (a best-effort subset of the renewed ones), how many the
    provider couldn't serve this run (or returned empty for), and the per-run cap (``None``
    when the run was uncapped)."""

    refreshed: int
    failed: int
    limit: int | None
    rating_changes_refreshed: int = 0


class SyncRecommendations:
    """Renew stored analyst coverage from the live source, most-stale stocks first — and
    **seed** stocks not yet cached (never-fetched anchor stocks come first).

    Primarily the recommendation trends (+ price target). When a rating-change provider and
    repository are also wired, the same one-pass walk stores each renewed stock's
    upgrade/downgrade events too — folded into this sweep rather than a second pass over the
    whole anchor, since that would double the (rate-limited) Yahoo round-trips. The
    rating-change leg is **best-effort enrichment**: it runs only after a stock's trends
    refresh succeeds, and its own failure is swallowed so it can never sink the sweep.
    """

    def __init__(
        self,
        provider: RecommendationProvider,
        repository: RecommendationsRepository,
        *,
        rating_change_provider: RatingChangeProvider | None = None,
        rating_change_repository: RatingChangesRepository | None = None,
    ) -> None:
        self._provider = provider
        self._repository = repository
        self._rating_change_provider = rating_change_provider
        self._rating_change_repository = rating_change_repository

    def execute(self, *, limit: int | None = None) -> RecommendationsSyncReport:
        """Refresh up to ``limit`` stocks most in need of it (un-cached first, then stalest);
        ``limit=None`` (the default) processes every stock in the anchor. Returns a summary.
        Never raises for a single symbol's failure — the run continues and the failure is
        counted, so one bad symbol doesn't abort the whole sweep."""
        effective = None if limit is None else max(1, limit)
        refreshed = 0
        failed = 0
        rating_changes_refreshed = 0
        targets = self._repository.refresh_targets(effective)
        for target in iter_with_progress(
            targets, logger=logger, label="recommendations sync"
        ):
            try:
                recommendations = self._provider.get_recommendations(target.symbol)
            except (StockNotFound, StockDataUnavailable):
                # A symbol the vendor can't serve this run (outage, block, or dropped
                # coverage) is left as-is and counted; the next run retries it.
                failed += 1
                continue
            # An empty live result has nothing to merge (the upsert would write no rows,
            # so the stock's refresh stamp would never advance and it would jam the front
            # of the stale queue). Skip it and count a failure so the next run retries;
            # the stored months keep serving in the meantime.
            if recommendations.is_empty:
                failed += 1
                continue
            # Carry the stored name so a nameless refresh doesn't drop a known one.
            self._repository.upsert(target.symbol, target.name, recommendations)
            refreshed += 1
            rating_changes_refreshed += self._sync_rating_changes(target)
        return RecommendationsSyncReport(
            refreshed=refreshed,
            failed=failed,
            limit=effective,
            rating_changes_refreshed=rating_changes_refreshed,
        )

    def _sync_rating_changes(self, target: RefreshTarget) -> int:
        """Best-effort: store the stock's upgrade/downgrade events, returning 1 when any were
        stored (0 otherwise). No-op when the rating-change ports aren't wired. A live failure
        or an empty feed is swallowed — this leg must never fail the recommendations sweep."""
        provider = self._rating_change_provider
        repository = self._rating_change_repository
        if provider is None or repository is None:
            return 0
        try:
            changes = provider.get_rating_changes(target.symbol)
        except (StockNotFound, StockDataUnavailable):
            return 0
        if changes.is_empty:
            return 0
        try:
            repository.upsert(target.symbol, target.name, changes)
        except Exception:  # noqa: BLE001 — best-effort enrichment, never sink the sweep
            logger.warning(
                "rating-changes upsert failed for %s", target.symbol, exc_info=True
            )
            return 0
        return 1
