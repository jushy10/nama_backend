"""Application use cases for the ETF slice.

Pure orchestration over the ports so each runs offline in tests against hand-written fakes and
knows nothing of Yahoo, HTTP, or SQLAlchemy:

- ``SyncEtfs`` — the out-of-band populator. Two passes in one run: (1) screen the top US ETFs
  and upsert the result into the ``etfs`` table (additive: it never removes a fund); (2) enrich
  up to ``limit`` stored funds that still lack a ``category``, classifying each through a
  per-ticker call and writing its slug. Invoked by the (fire-and-forget) cron endpoint. Guarded
  so a blocked/truncated screen (empty or implausibly small) skips *both* passes rather than
  churning a partial set or hammering the same blocked vendor with per-ticker calls.
- ``SearchEtfs`` — the read side (``GET /stocks/etfs``): normalize a search request at the edge
  and hand the read repository a clean ``EtfSearchCriteria``, returning the matched page. No live
  feed — the set is already in the table.
- ``ListEtfCategories`` — the filter-menu read (``GET /stocks/etfs/categories``): the distinct
  category slugs the FE offers, straight from the repository.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.stocks.etfs.entities import (
    EtfCategories,
    EtfSearchCriteria,
    EtfSearchPage,
    EtfSort,
    SortDirection,
    slugify,
)
from app.stocks.etfs.ports import EtfCategoryProvider, EtfScreener
from app.stocks.etfs.repository import EtfRepository, EtfSearchRepository
from app.stocks.exceptions import StockDataUnavailable, StockNotFound


@dataclass(frozen=True)
class EtfSyncReport:
    """The outcome of one sync run.

    ``screened`` is the screen size and ``added`` / ``updated`` the rows the screen upsert
    inserted / refreshed. ``enriched`` is how many funds the enrichment pass categorised this run
    and ``enrich_failed`` how many per-ticker lookups the source couldn't serve (an outage or
    block) — both zero when the screen was skipped. ``skipped`` is ``True`` when the screen came
    back empty or implausibly small (a truncated or blocked fetch) so *nothing* was written; the
    other counts are then all zero. There is no ``removed`` count — the sync is additive.
    """

    screened: int
    added: int
    updated: int
    skipped: bool
    enriched: int
    enrich_failed: int


class SyncEtfs:
    """Populate/refresh the searchable ETF set from a live top-ETFs screen, then categorise the
    funds that still lack one."""

    # Below this many screened funds the result is treated as truncated or blocked (a healthy
    # top-ETFs screen is ~540), so the upsert is skipped — a bad Yahoo day shouldn't re-stamp
    # only a partial slice as freshly screened. The screener also raises on a hard failure (which
    # propagates); this guards a *degraded* success.
    MIN_PLAUSIBLE_SCREEN = 50

    # Default funds the enrichment pass categorises per run; the caller (the cron endpoint) can
    # override. The top-ETF set is ~540, so this default covers the whole set in one run while
    # staying bounded (the sequential per-ticker Yahoo calls are rate-limited); since ``category``
    # is fill-once, each run only touches the still-uncategorised.
    DEFAULT_LIMIT = 600

    def __init__(
        self,
        screener: EtfScreener,
        repository: EtfRepository,
        classifier: EtfCategoryProvider,
    ) -> None:
        self._screener = screener
        self._repository = repository
        self._classifier = classifier

    def execute(self, *, limit: int | None = None) -> EtfSyncReport:
        """Screen the top ETFs, upsert the result, then categorise up to ``limit`` (default
        ``DEFAULT_LIMIT``) still-uncategorised funds.

        A hard screen failure (``StockDataUnavailable``) propagates to the caller (the background
        runner logs it). A *degraded* screen — fewer than ``MIN_PLAUSIBLE_SCREEN`` funds — is
        skipped so a partial/blocked fetch isn't written, and the enrichment pass is skipped too
        (if the one bulk screen call was blocked, the per-ticker calls would be as well).
        Otherwise the whole screen is upserted (additive) and the enrichment pass runs. A single
        fund's classification failure never aborts the run — it's counted and the sweep continues.
        """
        capped = self.DEFAULT_LIMIT if limit is None else max(1, limit)
        screened = self._screener.screen()
        if len(screened) < self.MIN_PLAUSIBLE_SCREEN:
            return EtfSyncReport(
                screened=len(screened),
                added=0,
                updated=0,
                skipped=True,
                enriched=0,
                enrich_failed=0,
            )
        counts = self._repository.upsert_screen(screened)
        enriched, enrich_failed = self._enrich_missing_categories(capped)
        return EtfSyncReport(
            screened=len(screened),
            added=counts.added,
            updated=counts.updated,
            skipped=False,
            enriched=enriched,
            enrich_failed=enrich_failed,
        )

    def _enrich_missing_categories(self, limit: int) -> tuple[int, int]:
        """Categorise up to ``limit`` stored funds still missing a category, writing each one's
        slug. Returns ``(enriched, failed)``: ``enriched`` wrote a category, ``failed`` couldn't
        reach the source. A fund the source reaches but doesn't categorise (``category`` None) is
        neither — it's left for a later run rather than counted, since nothing was written and
        nothing went wrong."""
        enriched = 0
        failed = 0
        for ticker in self._repository.tickers_missing_category(limit):
            try:
                classification = self._classifier.get_category(ticker)
            except (StockNotFound, StockDataUnavailable):
                # The source couldn't serve this fund this run (outage/block). Leave it and count
                # it; the next run retries it.
                failed += 1
                continue
            if classification.category is None:
                continue  # source has no category yet — leave it for a later run
            self._repository.set_category(ticker, classification)
            enriched += 1
        return enriched, failed


class SearchEtfs:
    """Search/filter/sort the stored ETF set for the ``GET /stocks/etfs`` list.

    Pure orchestration over the read repository: normalize the request once at the edge, hand the
    repository a clean ``EtfSearchCriteria``, return the page it matches. No live feed, no vendor
    — the set is already stored by the sync.
    """

    # The default page size, and the ceiling a client can ask for. The endpoint enforces the same
    # bounds on its query param; the use case clamps too, so a direct caller (or a test) can't ask
    # for an unbounded or zero page.
    DEFAULT_LIMIT = 25
    MAX_LIMIT = 100

    def __init__(self, repository: EtfSearchRepository) -> None:
        self._repository = repository

    def execute(
        self,
        *,
        query: str | None = None,
        category: str | None = None,
        sort: EtfSort = EtfSort.NET_ASSETS,
        direction: SortDirection = SortDirection.DESC,
        limit: int | None = None,
        offset: int = 0,
    ) -> EtfSearchPage:
        """Normalize the inputs once, at the edge, then run the search.

        ``query`` is trimmed (blank → no text filter); ``category`` is slugged to the stored
        convention with :func:`slugify` (so both the raw label and the stored slug match, and
        blank → no filter); ``limit`` defaults to ``DEFAULT_LIMIT`` and is clamped to
        ``[1, MAX_LIMIT]``, ``offset`` floored at 0. The sort/direction pass through as-is
        (already validated enums). The repository does the rest.
        """
        text = (query or "").strip()
        capped = self.DEFAULT_LIMIT if limit is None else min(max(1, limit), self.MAX_LIMIT)
        criteria = EtfSearchCriteria(
            query=text or None,
            category=slugify(category),
            sort=sort,
            direction=direction,
            limit=capped,
            offset=max(0, offset),
        )
        return self._repository.search(criteria)


class ListEtfCategories:
    """The distinct category slugs for the FE's filter menu (``GET /stocks/etfs/categories``).

    A thin read — the repository owns the distinct query; this is its own use case only to keep
    the one-class-per-action convention (and so the endpoint depends on a use case, not the
    repository directly).
    """

    def __init__(self, repository: EtfSearchRepository) -> None:
        self._repository = repository

    def execute(self) -> EtfCategories:
        return self._repository.categories()
