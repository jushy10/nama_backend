"""Application use cases for the universe slice.

Pure orchestration over the ports so each runs offline in tests against hand-written fakes
and knows nothing of Yahoo, HTTP, or SQLAlchemy:

- ``SyncUniverse`` — the out-of-band populator. Two passes in one run: (1) screen the US
  market at/above the floor and upsert the result onto the ``stocks`` anchor (additive: it
  never removes a stock); (2) enrich up to ``limit`` stored stocks that still lack a
  ``sector`` or ``industry``, classifying each through a per-ticker call and writing its
  sector/industry slugs. Invoked by the (fire-and-forget) cron endpoint. Guarded so a blocked/truncated
  screen (empty or implausibly small) skips *both* passes rather than churning a partial set
  or hammering the same blocked vendor with per-ticker calls.
- ``SearchStocks`` — the read side (``GET /stocks/ticker``): normalize a search request at the
  edge and hand the read repository a clean ``StockSearchCriteria``, returning the matched
  page. No live feed — the universe is already on the anchor.
- ``ListClassifications`` — the filter-menu read (``GET /stocks/classifications``): the
  distinct sector/industry slugs the FE offers, straight from the repository.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.stocks.exceptions import StockDataUnavailable, StockNotFound
from app.stocks.universe.entities import (
    Classifications,
    SortDirection,
    StockSearchCriteria,
    StockSearchPage,
    StockSort,
    slugify,
)
from app.stocks.universe.ports import CompanyClassificationProvider, StockScreener
from app.stocks.universe.repository import StockSearchRepository, UniverseRepository


@dataclass(frozen=True)
class UniverseSyncReport:
    """The outcome of one sync run.

    ``screened`` is the screen size and ``added`` / ``updated`` the anchors the screen upsert
    inserted / refreshed. ``enriched`` is how many stocks the enrichment pass classified this
    run (wrote a sector/industry for) and ``enrich_failed`` how many per-ticker lookups the
    source couldn't serve (an outage or block) — both zero when the screen was skipped.
    ``skipped`` is ``True`` when the screen came back empty or implausibly small (a truncated
    or blocked fetch) so *nothing* was written; the four counts are then all zero. There is no
    ``removed`` count: the sync is additive (a shared anchor is never deleted).
    """

    screened: int
    added: int
    updated: int
    skipped: bool
    enriched: int
    enrich_failed: int


class SyncUniverse:
    """Populate/refresh the searchable universe from a live market screen, then classify the
    stocks that still lack a sector/industry."""

    # The market-cap floor that defines the universe: US companies worth at least $1B.
    MIN_MARKET_CAP = 1_000_000_000.0

    # Below this many screened names the result is treated as truncated or blocked (a
    # healthy US ≥$1B screen is ~2,800 names), so the upsert is skipped — a bad
    # vendor day shouldn't re-stamp only a partial slice as freshly screened. The screener
    # also raises on a hard failure (which propagates); this guards a *degraded* success.
    MIN_PLAUSIBLE_SCREEN = 100

    # Default stocks the enrichment pass classifies per run; the caller (the cron endpoint)
    # can override per invocation. Kept modest so the sequential per-ticker Yahoo calls stay
    # gentle on its rate limits — a universe larger than this is classified over successive
    # runs, and since ``industry`` is fill-once each run only touches the still-unclassified.
    DEFAULT_LIMIT = 500

    def __init__(
        self,
        screener: StockScreener,
        repository: UniverseRepository,
        classifier: CompanyClassificationProvider,
    ) -> None:
        self._screener = screener
        self._repository = repository
        self._classifier = classifier

    def execute(self, *, limit: int | None = None) -> UniverseSyncReport:
        """Screen the market, upsert the result onto the anchor, then classify up to ``limit``
        (default ``DEFAULT_LIMIT``) still-unclassified stocks.

        A hard screen failure (``StockDataUnavailable``) propagates to the caller (the
        background runner logs it). A *degraded* screen — fewer than ``MIN_PLAUSIBLE_SCREEN``
        names — is skipped so a partial/blocked fetch isn't written, and the enrichment pass
        is skipped too (if the one bulk screen call was blocked, the per-ticker calls would
        be as well). Otherwise the whole screen is upserted (additive) and the enrichment pass
        runs. A single symbol's classification failure never aborts the run — it's counted and
        the sweep continues.
        """
        capped = self.DEFAULT_LIMIT if limit is None else max(1, limit)
        screened = self._screener.screen(min_market_cap=self.MIN_MARKET_CAP)
        if len(screened) < self.MIN_PLAUSIBLE_SCREEN:
            return UniverseSyncReport(
                screened=len(screened),
                added=0,
                updated=0,
                skipped=True,
                enriched=0,
                enrich_failed=0,
            )
        counts = self._repository.upsert_screen(screened)
        enriched, enrich_failed = self._enrich_missing_classifications(capped)
        return UniverseSyncReport(
            screened=len(screened),
            added=counts.added,
            updated=counts.updated,
            skipped=False,
            enriched=enriched,
            enrich_failed=enrich_failed,
        )

    def _enrich_missing_classifications(self, limit: int) -> tuple[int, int]:
        """Classify up to ``limit`` stored stocks still missing a sector or industry, writing
        each one's sector/industry. Returns ``(enriched, failed)``: ``enriched`` wrote a
        classification, ``failed`` couldn't reach the source. A symbol the source reaches but
        can't classify (both sides ``None``) is neither — it's left for a later run rather than
        counted, since nothing was written and nothing went wrong."""
        enriched = 0
        failed = 0
        for ticker in self._repository.tickers_missing_classification(limit):
            try:
                classification = self._classifier.get_classification(ticker)
            except (StockNotFound, StockDataUnavailable):
                # The source couldn't serve this symbol this run (outage/block). Leave it as
                # is and count it; the next run retries it.
                failed += 1
                continue
            if classification.industry is None and classification.sector is None:
                continue  # source has no classification yet — leave it for a later run
            self._repository.set_classification(ticker, classification)
            enriched += 1
        return enriched, failed


class SearchStocks:
    """Search/filter/sort the screened universe for the ``GET /stocks/ticker`` list.

    Pure orchestration over the read repository: normalize the request once at the edge, hand
    the repository a clean ``StockSearchCriteria``, return the page it matches. No live feed,
    no vendor — the universe is already stored on the anchor by the sync.
    """

    # The default page size, and the ceiling a client can ask for. The endpoint enforces the
    # same bounds on its query param; the use case clamps too, so a direct caller (or a test)
    # can't ask for an unbounded or zero page.
    DEFAULT_LIMIT = 25
    MAX_LIMIT = 100

    def __init__(self, repository: StockSearchRepository) -> None:
        self._repository = repository

    def execute(
        self,
        *,
        query: str | None = None,
        sector: str | None = None,
        industry: str | None = None,
        in_sp500: bool | None = None,
        in_nasdaq100: bool | None = None,
        sort: StockSort = StockSort.MARKET_CAP,
        direction: SortDirection = SortDirection.DESC,
        limit: int | None = None,
        offset: int = 0,
    ) -> StockSearchPage:
        """Normalize the inputs once, at the edge, then run the search.

        ``query`` is trimmed (blank → no text filter); ``sector`` / ``industry`` are slugged to
        the stored convention with :func:`slugify` (so both the raw label and the stored slug
        match, and blank → no filter); ``limit`` defaults to ``DEFAULT_LIMIT`` and is clamped to
        ``[1, MAX_LIMIT]``, ``offset`` floored at 0. The index flags pass through as a tri-state
        (``None`` = don't filter). The repository does the rest.
        """
        text = (query or "").strip()
        capped = self.DEFAULT_LIMIT if limit is None else min(max(1, limit), self.MAX_LIMIT)
        criteria = StockSearchCriteria(
            query=text or None,
            sector=slugify(sector),
            industry=slugify(industry),
            in_sp500=in_sp500,
            in_nasdaq100=in_nasdaq100,
            sort=sort,
            direction=direction,
            limit=capped,
            offset=max(0, offset),
        )
        return self._repository.search(criteria)


class ListClassifications:
    """The distinct sector + industry slugs for the FE's filter menus
    (``GET /stocks/classifications``).

    A thin read — the repository owns the distinct query; this is its own use case only to keep
    the one-class-per-action convention (and so the endpoint depends on a use case, not the
    repository directly).
    """

    def __init__(self, repository: StockSearchRepository) -> None:
        self._repository = repository

    def execute(self) -> Classifications:
        return self._repository.classifications()
