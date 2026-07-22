from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass

from app.domains.financials.earnings.quarterly.interfaces import QuarterlyEarningsRepositoryAdapter
from app.domains.shared.entities import is_cboe_canada
from app.domains.shared.exceptions import StockDataUnavailable, StockNotFound
from app.domains.shared.progress import iter_with_progress
from app.domains.listings.universe.entities import (
    Classifications,
    IndustryValuation,
    MarketCapTier,
    PeerComparison,
    ScreenedStock,
    ScreenIntent,
    SortDirection,
    StockSearchCriteria,
    StockSearchPage,
    StockSort,
    normalize_company_name,
    slugify,
)
from app.domains.listings.universe.interfaces import (
    CompanyClassificationAdapter,
    ScreenerQueryAdapter,
    StockScreenerAdapter,
)
from app.domains.listings.universe.interfaces import StockSearchRepositoryAdapter, UniverseRepositoryAdapter

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class UniverseSyncReport:
    screened: int
    added: int
    updated: int
    skipped: bool
    enriched: int
    enrich_failed: int
    valued: int


def _slugged(values: Sequence[str] | None) -> tuple[str, ...]:
    if not values:
        return ()
    return tuple(dict.fromkeys(s for v in values if (s := slugify(v)) is not None))


def _upper_codes(values: Sequence[str] | None) -> tuple[str, ...]:
    if not values:
        return ()
    return tuple(
        dict.fromkeys(
            v.strip().upper() for v in values if isinstance(v, str) and v.strip()
        )
    )


def _pe_ratio(price: float | None, ttm_eps: float | None) -> float | None:
    if price is None or ttm_eps is None or price <= 0 or ttm_eps <= 0:
        return None
    return round(price / ttm_eps, 2)


def _fcf_yield(price: float | None, fcf_per_share: float | None) -> float | None:
    if price is None or fcf_per_share is None or price <= 0:
        return None
    return round(fcf_per_share / price * 100, 2)


def _ev_ebitda(
    market_cap: float | None,
    components: tuple[float, float | None, float | None] | None,
) -> float | None:
    if market_cap is None or components is None:
        return None
    ebitda, total_debt, cash = components
    if ebitda is None or ebitda <= 0:
        return None
    enterprise_value = market_cap + (total_debt or 0.0) - (cash or 0.0)
    return round(enterprise_value / ebitda, 2)


class SyncUniverse:
    # The market-cap floor that defines the universe: companies worth at least $1B **in the
    # screened market's own currency** (Yahoo screens each quote natively, so this is $1B USD
    # for the US pass and $1B CAD for the CA pass — the floor is the same number, applied per
    # market's money).
    MIN_MARKET_CAP = 1_000_000_000.0

    # Below this many screened names the result is treated as truncated or blocked, so the
    # upsert is skipped — a bad vendor day shouldn't re-stamp only a partial slice as freshly
    # screened. The floor is **per market**, since a healthy screen's size differs by market (a
    # US ≥$1B screen is ~2,800 names; a CA ≥$1B screen is only a few hundred). The screener also
    # raises on a hard failure (which propagates); this guards a *degraded* success. Keyed by
    # ISO-2 region; ``MIN_PLAUSIBLE_SCREEN`` is the US default and the fallback for any market
    # not listed.
    MIN_PLAUSIBLE_SCREEN = 100
    _MIN_PLAUSIBLE_BY_REGION: dict[str, int] = {"us": 100, "ca": 40}

    # Default stocks the enrichment pass classifies per run; the caller (the cron endpoint)
    # can override per invocation. Kept modest so the sequential per-ticker Yahoo calls stay
    # gentle on its rate limits — a universe larger than this is classified over successive
    # runs, and since ``industry`` is fill-once each run only touches the still-unclassified.
    DEFAULT_LIMIT = 500

    def __init__(
        self,
        screener: StockScreenerAdapter,
        repository: UniverseRepositoryAdapter,
        classifier: CompanyClassificationAdapter,
        quarterly: QuarterlyEarningsRepositoryAdapter | None = None,
        *,
        region: str = "us",
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
        # The market this instance screens (ISO-2). Drives the screener's region and the
        # per-market plausibility floor; the currency/country ride back on each ScreenedStock.
        self._region = region.lower()
        self._min_plausible = self._MIN_PLAUSIBLE_BY_REGION.get(
            self._region, self.MIN_PLAUSIBLE_SCREEN
        )

    def execute(self, *, limit: int | None = None) -> UniverseSyncReport:
        capped = self.DEFAULT_LIMIT if limit is None else max(1, limit)
        screened = tuple(
            stock
            for stock in self._screener.screen(
                min_market_cap=self.MIN_MARKET_CAP, region=self._region
            )
            if not is_cboe_canada(stock.ticker)
        )
        if len(screened) < self._min_plausible:
            return UniverseSyncReport(
                screened=len(screened),
                added=0,
                updated=0,
                skipped=True,
                enriched=0,
                enrich_failed=0,
                valued=0,
            )
        if self._region != "us":
            # A CA pass runs after the US pass (US universe + its domicile on the anchor), so we
            # can spot a .TO CDR of a US company by name and both drop it from this upsert and
            # purge any copy a prior run stored. Skipped for the US pass (a US row would match
            # itself). Only on a healthy screen (past the plausibility gate above).
            screened = self._drop_and_purge_us_company_cdrs(screened)
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

    def _drop_and_purge_us_company_cdrs(
        self, screened: tuple[ScreenedStock, ...]
    ) -> tuple[ScreenedStock, ...]:
        us_names = frozenset(
            normalized
            for name in self._repository.us_domiciled_company_names()
            if (normalized := normalize_company_name(name)) is not None
        )
        if not us_names:
            return screened
        cdr_tickers = tuple(
            stock.ticker
            for stock in screened
            if (norm := normalize_company_name(stock.name)) is not None
            and norm in us_names
        )
        if cdr_tickers:
            # Purge any copies a prior run stored (the row's own domicile may be stale/absent),
            # then drop them from this upsert so they're never (re-)written.
            self._repository.delete_stocks(cdr_tickers)
            dropped = set(cdr_tickers)
            screened = tuple(s for s in screened if s.ticker not in dropped)
        return screened

    def _enrich_missing_classifications(self, limit: int) -> tuple[int, int]:
        enriched = 0
        failed = 0
        tickers = self._repository.tickers_missing_classification(limit)
        for ticker in iter_with_progress(
            tickers, logger=logger, label="universe sync (classification)"
        ):
            try:
                classification = self._classifier.get_classification(ticker)
            except (StockNotFound, StockDataUnavailable):
                # The source couldn't serve this symbol this run (outage/block). Leave it as
                # is and count it; the next run retries it.
                failed += 1
                continue
            if (
                classification.industry is None
                and classification.sector is None
                and classification.domicile_country is None
            ):
                continue  # source has nothing for it yet — leave it for a later run
            self._repository.set_classification(ticker, classification)
            enriched += 1
        return enriched, failed

    def _value_screened(self, screened: tuple[ScreenedStock, ...]) -> int:
        pe_by_ticker: dict[str, float | None] = {}
        fcf_yield_by_ticker: dict[str, float | None] = {}
        ev_ebitda_by_ticker: dict[str, float | None] = {}
        fcf_per_share = self._repository.fcf_per_share_by_ticker()
        ev_components = self._repository.ev_components_by_ticker()
        for stock in screened:
            if stock.price is None:
                continue  # no price this sweep — leave any prior figures untouched
            if self._quarterly is not None:
                stored = self._quarterly.get(stock.ticker)
                ttm_eps = stored.ttm_eps if stored is not None else None
                pe_by_ticker[stock.ticker] = _pe_ratio(stock.price, ttm_eps)
            fcf_yield_by_ticker[stock.ticker] = _fcf_yield(
                stock.price, fcf_per_share.get(stock.ticker)
            )
            # EV/EBITDA off the screen-time market cap (not the price × shares the card uses
            # live) + the anchor's stored EV components — a sortable snapshot like the P/E.
            ev_ebitda_by_ticker[stock.ticker] = _ev_ebitda(
                stock.market_cap, ev_components.get(stock.ticker)
            )
        self._repository.set_fcf_yields(fcf_yield_by_ticker)
        self._repository.set_ev_ebitda(ev_ebitda_by_ticker)
        return self._repository.set_pe_ratios(pe_by_ticker) if pe_by_ticker else 0


class SearchStocks:
    # The default page size, and the ceiling a client can ask for. The endpoint enforces the
    # same bounds on its query param; the use case clamps too, so a direct caller (or a test)
    # can't ask for an unbounded or zero page.
    DEFAULT_LIMIT = 25
    MAX_LIMIT = 100

    def __init__(self, repository: StockSearchRepositoryAdapter) -> None:
        self._repository = repository

    def execute(
        self,
        *,
        query: str | None = None,
        sectors: Sequence[str] | None = None,
        industries: Sequence[str] | None = None,
        in_sp500: bool | None = None,
        in_nasdaq100: bool | None = None,
        market_cap_tiers: Sequence[MarketCapTier] | None = None,
        sort: StockSort | None = None,
        direction: SortDirection = SortDirection.DESC,
        limit: int | None = None,
        offset: int = 0,
        countries: Sequence[str] | None = None,
        include_interlisted: bool = False,
    ) -> StockSearchPage:
        text = (query or "").strip()
        capped = self.DEFAULT_LIMIT if limit is None else min(max(1, limit), self.MAX_LIMIT)
        criteria = StockSearchCriteria(
            query=text or None,
            sectors=_slugged(sectors),
            industries=_slugged(industries),
            in_sp500=in_sp500,
            in_nasdaq100=in_nasdaq100,
            market_cap_tiers=tuple(
                dict.fromkeys(t for t in (market_cap_tiers or ()) if t is not None)
            ),
            sort=sort,
            direction=direction,
            limit=capped,
            offset=max(0, offset),
            countries=_upper_codes(countries),
            include_interlisted=include_interlisted,
        )
        return self._repository.search(criteria)


class AiScreenStocks:
    def __init__(
        self,
        translator: ScreenerQueryAdapter,
        repository: StockSearchRepositoryAdapter,
    ) -> None:
        self._translator = translator
        # Read only for the allowed-vocabulary the translator is constrained to.
        self._repository = repository

    def execute(self, *, query: str) -> ScreenIntent:
        text = (query or "").strip()
        if not text:
            raise ValueError("A search request is required.")
        allowed = self._repository.classifications()
        return self._translator.translate(
            text, sectors=allowed.sectors, industries=allowed.industries
        )


class ListClassifications:
    def __init__(self, repository: StockSearchRepositoryAdapter) -> None:
        self._repository = repository

    def execute(self) -> Classifications:
        return self._repository.classifications()


class GetIndustryValuation:
    def __init__(self, repository: StockSearchRepositoryAdapter) -> None:
        self._repository = repository

    def execute(self, industry: str) -> IndustryValuation:
        slug = slugify(industry)
        if slug is None:
            raise ValueError("An industry is required.")
        pe_ratios = self._repository.pe_ratios_for_industry(slug)
        return IndustryValuation.from_pe_ratios(slug, pe_ratios)


def _normalize_ticker(ticker: str) -> str:
    normalized = (ticker or "").strip().upper()
    if not normalized:
        raise ValueError("A ticker is required.")
    return normalized


class GetPeerComparison:
    def __init__(self, repository: StockSearchRepositoryAdapter) -> None:
        self._repository = repository

    def execute(self, ticker: str) -> PeerComparison:
        normalized = _normalize_ticker(ticker)
        industry = self._repository.industry_for_ticker(normalized)
        if industry is None:
            return PeerComparison.build(normalized, None, ())
        candidates = self._repository.peers_for_industry(industry)
        return PeerComparison.build(normalized, industry, candidates)
