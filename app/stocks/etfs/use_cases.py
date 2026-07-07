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

import logging
from dataclasses import dataclass

from app.stocks.etfs.entities import (
    EtfCategories,
    EtfDetail,
    EtfProfile,
    EtfSearchCriteria,
    EtfSearchPage,
    EtfSort,
    SortDirection,
    slugify,
)
from app.stocks.etfs.ports import EtfCategoryProvider, EtfProfileProvider, EtfScreener
from app.stocks.etfs.repository import (
    EtfLookupRepository,
    EtfRepository,
    EtfSearchRepository,
)
from app.stocks.exceptions import StockDataUnavailable, StockNotFound
from app.stocks.ports import StockQuoteProvider
from app.stocks.progress import iter_with_progress

logger = logging.getLogger(__name__)


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

    # The AUM floor that defines the searchable ETF set: US funds with at least $1M in net
    # assets — effectively the full US ETF universe, excluding only near-empty/pre-launch
    # shells. The screener filters this server-side and ranks by AUM.
    MIN_NET_ASSETS = 1_000_000.0

    # Below this many screened funds the result is treated as truncated or blocked (a healthy
    # US ≥$1M ETF screen is several thousand funds), so the upsert is skipped — a bad Yahoo day shouldn't
    # re-stamp only a partial slice as freshly screened. The screener also raises on a hard
    # failure (which propagates); this guards a *degraded* success.
    MIN_PLAUSIBLE_SCREEN = 100

    # Default funds the enrichment pass categorises per run; the caller (the cron endpoint) can
    # override. Kept modest so the sequential per-ticker Yahoo calls stay gentle on rate limits —
    # the ≥$1M set (several thousand) is classified over successive runs, and since ``category`` is
    # fill-once each run only touches the still-uncategorised.
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
        screened = self._screener.screen(min_net_assets=self.MIN_NET_ASSETS)
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
        tickers = self._repository.tickers_missing_category(limit)
        for ticker in iter_with_progress(
            tickers, logger=logger, label="etf sync (categorization)"
        ):
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


def _normalize_symbol(symbol: str) -> str:
    """Trim/upper-case the ticker and reject obvious junk, once, at the edge of the use case —
    the same guard the ticker/stocks slices apply, so ``GET /stocks/etf/{ticker}`` 400s on the
    same bad input as its siblings."""
    normalized = (symbol or "").strip().upper()
    if not normalized:
        raise ValueError("An ETF symbol is required.")
    if not normalized.isalpha() or len(normalized) > 5:
        # Simple guard; ETF tickers are 1-5 letters, like the stock guard.
        raise ValueError(f"'{symbol}' is not a valid ETF symbol.")
    return normalized


class GetEtfDetail:
    """Use case: one fund's detail card — the live quote, the stored ``etfs`` facts, and the
    best-effort Yahoo profile (``GET /stocks/etf/{ticker}``).

    Membership-gated and quote-primary. First the symbol is looked up in the stored ETF universe:
    a symbol that isn't a screened fund raises ``StockNotFound`` (the endpoint maps it to 404 —
    "not an ETF"), *before* any quote or Yahoo call, so a stock or a bogus ticker costs nothing
    upstream. Then the live quote is fetched and is **primary** — a quote failure propagates
    (the endpoint maps it to the same 502/503 the quote endpoints use), because a detail card with
    no price isn't worth serving. The Yahoo profile is best-effort enrichment layered last: its
    provider is total (never raises), so a blocked or uncovered read just leaves the profile empty
    and the card still returns 200 with the quote + stored facts. The stored net_assets/expense
    figures win over the profile's where both exist (the detail page must agree with the screener
    list); the profile only fills the gaps.
    """

    def __init__(
        self,
        lookup: EtfLookupRepository,
        quotes: StockQuoteProvider,
        profile: EtfProfileProvider,
    ) -> None:
        self._lookup = lookup
        self._quotes = quotes
        self._profile = profile

    def execute(self, symbol: str) -> EtfDetail:
        normalized = _normalize_symbol(symbol)
        # Membership gate first: not an ETF -> 404, before any upstream call.
        facts = self._lookup.get(normalized)
        if facts is None:
            raise StockNotFound(normalized)
        # Primary source: a quote failure propagates (mapped to 502/503 at the edge).
        quote = self._quotes.get_quote(normalized)
        # Best-effort enrichment: the provider is total, but guard anyway so a contract slip
        # can never sink a card whose primary data (the quote) is already in hand.
        try:
            profile = self._profile.get_profile(normalized)
        except (StockNotFound, StockDataUnavailable):
            profile = EtfProfile.empty()
        return EtfDetail.assemble(normalized, quote, facts, profile)
