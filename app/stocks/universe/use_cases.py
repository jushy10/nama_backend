"""Application use cases for the universe slice.

Pure orchestration over the ports so each runs offline in tests against hand-written fakes
and knows nothing of Yahoo, HTTP, or SQLAlchemy:

- ``SyncUniverse`` — the out-of-band populator. Three passes in one run: (1) screen the US
  market at/above the floor and upsert the result onto the ``stocks`` anchor (additive: it
  never removes a stock); (2) enrich up to ``limit`` stored stocks that still lack a
  ``sector`` or ``industry``, classifying each through a per-ticker call and writing its
  sector/industry slugs; (3) value every screened stock — its trailing P/E from the
  screen-time price over the quarterly slice's stored TTM consensus EPS — overwriting the
  anchor's ``pe_ratio``. Invoked by the (fire-and-forget) cron endpoint. Guarded so a
  blocked/truncated screen (empty or implausibly small) skips *all* passes rather than
  churning a partial set or hammering the same blocked vendor with per-ticker calls.
- ``SearchStocks`` — the read side (``GET /stocks/ticker``): normalize a search request at the
  edge and hand the read repository a clean ``StockSearchCriteria``, returning the matched
  page. No live feed — the universe is already on the anchor.
- ``ListClassifications`` — the filter-menu read (``GET /stocks/classifications``): the
  distinct sector/industry slugs the FE offers, straight from the repository.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.stocks.earnings.quarterly.repository import QuarterlyEarningsRepository
from app.stocks.exceptions import StockDataUnavailable, StockNotFound
from app.stocks.universe.entities import (
    Classifications,
    MarketCapTier,
    ScreenedStock,
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
    source couldn't serve (an outage or block). ``valued`` is how many screened stocks the
    valuation pass wrote a non-null trailing P/E for (a stock with no cached TTM or a trailing
    loss is recomputed to ``None`` and not counted) — all three zero when the screen was
    skipped. ``skipped`` is ``True`` when the screen came back empty or implausibly small (a
    truncated or blocked fetch) so *nothing* was written; the counts are then all zero. There
    is no ``removed`` count: the sync is additive (a shared anchor is never deleted).
    """

    screened: int
    added: int
    updated: int
    skipped: bool
    enriched: int
    enrich_failed: int
    valued: int


def _pe_ratio(price: float | None, ttm_eps: float | None) -> float | None:
    """The ticker card's trailing P/E, materialized for the sortable anchor column.

    The exact figure ``TickerValuation.trailing_pe`` serves — a market price over the quarterly
    slice's consensus-basis TTM EPS — with the same positive-legs guard: ``None`` off a loss
    (``ttm_eps <= 0``), a missing/degenerate price, or fewer than four cached quarters (``ttm_eps``
    is then ``None``). Kept in lockstep with the card by definition, so the sort column and the
    card read the same P/E on the same basis."""
    if price is None or ttm_eps is None or price <= 0 or ttm_eps <= 0:
        return None
    return round(price / ttm_eps, 2)


class SyncUniverse:
    """Populate/refresh the searchable universe from a live market screen, classify the stocks
    that still lack a sector/industry, and value each screened stock with a trailing P/E."""

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
        quarterly: QuarterlyEarningsRepository | None = None,
    ) -> None:
        self._screener = screener
        self._repository = repository
        self._classifier = classifier
        # The DB-only stored-TTM read (no Yahoo call) the valuation pass pairs with the
        # screen-time price — so valuing the whole universe stays a cheap sweep of DB reads.
        # Optional because the P/E is best-effort enrichment (like sector/growth), not the
        # sync's reason to exist: wired without it, the sync still screens and classifies and
        # simply writes no P/E.
        self._quarterly = quarterly

    def execute(self, *, limit: int | None = None) -> UniverseSyncReport:
        """Screen the market, upsert the result onto the anchor, classify up to ``limit``
        (default ``DEFAULT_LIMIT``) still-unclassified stocks, then value every screened stock.

        A hard screen failure (``StockDataUnavailable``) propagates to the caller (the
        background runner logs it). A *degraded* screen — fewer than ``MIN_PLAUSIBLE_SCREEN``
        names — is skipped so a partial/blocked fetch isn't written, and the enrichment and
        valuation passes are skipped too (if the one bulk screen call was blocked, the
        per-ticker calls would be as well). Otherwise the whole screen is upserted (additive),
        the enrichment pass runs, and the valuation pass recomputes each screened stock's
        trailing P/E from the screen-time price over the quarterly slice's stored TTM EPS. A
        single symbol's classification failure never aborts the run — it's counted and the
        sweep continues.
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
                valued=0,
            )
        counts = self._repository.upsert_screen(screened)
        enriched, enrich_failed = self._enrich_missing_classifications(capped)
        valued = self._value_screened(screened)
        return UniverseSyncReport(
            screened=len(screened),
            added=counts.added,
            updated=counts.updated,
            skipped=False,
            enriched=enriched,
            enrich_failed=enrich_failed,
            valued=valued,
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

    def _value_screened(self, screened: tuple[ScreenedStock, ...]) -> int:
        """Recompute and persist every screened stock's trailing P/E, returning how many got a
        non-null figure.

        Values the *whole* screened set every run — it's cheap: the price already rode in on
        the screen, and the TTM read is DB-only (no Yahoo call). For each stock it pairs the
        screen-time price with the quarterly slice's stored TTM consensus EPS and applies the
        card's rule (:func:`_pe_ratio`), overwriting the anchor's ``pe_ratio`` in one commit. A
        stock with no price this sweep is skipped, so a rare missing price never nulls a good
        prior figure; a stock with a price but no cached TTM (or a trailing loss) is written
        ``None`` — genuinely no P/E, the same way the growth pair drops to null. A no-op (0)
        when no quarterly cache was wired — the P/E is best-effort enrichment."""
        if self._quarterly is None:
            return 0
        pe_by_ticker: dict[str, float | None] = {}
        for stock in screened:
            if stock.price is None:
                continue  # no price this sweep — leave any prior P/E untouched
            stored = self._quarterly.get(stock.ticker)
            ttm_eps = stored.ttm_eps if stored is not None else None
            pe_by_ticker[stock.ticker] = _pe_ratio(stock.price, ttm_eps)
        return self._repository.set_pe_ratios(pe_by_ticker)


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
        market_cap_tier: MarketCapTier | None = None,
        sort: StockSort | None = None,
        direction: SortDirection = SortDirection.DESC,
        limit: int | None = None,
        offset: int = 0,
    ) -> StockSearchPage:
        """Normalize the inputs once, at the edge, then run the search.

        ``query`` is trimmed (blank → no text filter); ``sector`` / ``industry`` are slugged to
        the stored convention with :func:`slugify` (so both the raw label and the stored slug
        match, and blank → no filter); ``limit`` defaults to ``DEFAULT_LIMIT`` and is clamped to
        ``[1, MAX_LIMIT]``, ``offset`` floored at 0. The index flags and ``market_cap_tier`` pass
        through as-is (already validated enums / tri-state booleans, ``None`` = don't filter).
        ``sort`` defaults to ``None`` — an unsorted browse the repository orders by ticker (A→Z);
        a ``StockSort`` value sorts by that column, ``direction`` (default descending) then
        applying. The repository does the rest.
        """
        text = (query or "").strip()
        capped = self.DEFAULT_LIMIT if limit is None else min(max(1, limit), self.MAX_LIMIT)
        criteria = StockSearchCriteria(
            query=text or None,
            sector=slugify(sector),
            industry=slugify(industry),
            in_sp500=in_sp500,
            in_nasdaq100=in_nasdaq100,
            market_cap_tier=market_cap_tier,
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
