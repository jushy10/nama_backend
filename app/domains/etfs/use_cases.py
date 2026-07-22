from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Sequence

from app.domains.research.analysis.entities import InvestmentAnalysis
from app.domains.shared.entities import StockPerformance, normalize_symbol
from app.domains.etfs.entities import (
    EtfCategories,
    EtfDetail,
    EtfProfile,
    EtfScreenIntent,
    EtfSearchCriteria,
    EtfSearchPage,
    EtfSort,
    SortDirection,
    slugify,
)
from app.domains.etfs.interfaces import (
    EtfAnalysisAdapter,
    EtfProfileAdapter,
    EtfScreenerAdapter,
    EtfScreenerQueryAdapter,
)
from app.domains.etfs.interfaces import (
    EtfLookupRepositoryAdapter,
    EtfRepositoryAdapter,
    EtfSearchRepositoryAdapter,
)
from app.domains.shared.exceptions import StockDataUnavailable, StockNotFound
from app.domains.research.analysis.interfaces import InvestmentAnalysisCacheAdapter
from app.domains.shared.interfaces import (
    StockPerformanceAdapter,
    StockQuoteAdapter,
)
from app.domains.shared.progress import iter_with_progress

logger = logging.getLogger(__name__)


def _slugged(values: Sequence[str] | None) -> tuple[str, ...]:
    if not values:
        return ()
    return tuple(dict.fromkeys(s for v in values if (s := slugify(v)) is not None))


# The blocks a caller may opt into on the ETF detail card (``?include=``). Everything else — the
# quote + day move, the stored identity facts (name/exchange/category), and the always-on Yahoo
# enrichment (fund family, description, holdings, sector weightings) — is served regardless.
INCLUDABLE = frozenset({"metrics", "dividends", "performance"})


@dataclass(frozen=True)
class EtfSyncReport:
    screened: int
    added: int
    updated: int
    skipped: bool
    enriched: int
    enrich_failed: int
    enriched_without_holdings: int


class SyncEtfs:
    # The AUM floor that defines the searchable ETF set: US funds with at least $1M in net
    # assets — effectively the full US ETF universe, excluding only near-empty/pre-launch
    # shells. The screener filters this server-side and ranks by AUM.
    MIN_NET_ASSETS = 1_000_000.0

    # Below this many screened funds the result is treated as truncated or blocked (a healthy
    # US ≥$1M ETF screen is several thousand funds), so the upsert is skipped — a bad Yahoo day shouldn't
    # re-stamp only a partial slice as freshly screened. The screener also raises on a hard
    # failure (which propagates); this guards a *degraded* success.
    MIN_PLAUSIBLE_SCREEN = 100

    def __init__(
        self,
        screener: EtfScreenerAdapter,
        repository: EtfRepositoryAdapter,
        profile_provider: EtfProfileAdapter,
    ) -> None:
        self._screener = screener
        self._repository = repository
        self._profile_provider = profile_provider

    def execute(self, *, limit: int | None = None) -> EtfSyncReport:
        capped = None if limit is None else max(1, limit)
        screened = self._screener.screen(min_net_assets=self.MIN_NET_ASSETS)
        if len(screened) < self.MIN_PLAUSIBLE_SCREEN:
            return EtfSyncReport(
                screened=len(screened),
                added=0,
                updated=0,
                skipped=True,
                enriched=0,
                enrich_failed=0,
                enriched_without_holdings=0,
            )
        counts = self._repository.upsert_screen(screened)
        enriched, enrich_failed, enriched_without_holdings = self._enrich_profiles(
            capped
        )
        return EtfSyncReport(
            screened=len(screened),
            added=counts.added,
            updated=counts.updated,
            skipped=False,
            enriched=enriched,
            enrich_failed=enrich_failed,
            enriched_without_holdings=enriched_without_holdings,
        )

    def _enrich_profiles(self, limit: int | None) -> tuple[int, int, int]:
        enriched = 0
        failed = 0
        without_holdings = 0
        tickers = self._repository.profile_refresh_targets(limit)
        for ticker in iter_with_progress(
            tickers, logger=logger, label="etf sync (profile enrichment)"
        ):
            try:
                profile = self._profile_provider.get_profile(ticker)
            except (StockNotFound, StockDataUnavailable):
                # The source couldn't serve this fund this run (outage/block). Leave its stored
                # profile intact and count it; the next run retries it.
                failed += 1
                continue
            self._repository.upsert_profile(ticker, profile)
            enriched += 1
            # Both holdings and sector weightings ride Yahoo's one topHoldings response, so both
            # empty means funds_data was blocked/absent even though .info served — count it as a
            # health signal (nearly every real ETF carries both).
            if not profile.top_holdings and not profile.sector_weightings:
                without_holdings += 1
        return enriched, failed, without_holdings


class SearchEtfs:
    # The default page size, and the ceiling a client can ask for. The endpoint enforces the same
    # bounds on its query param; the use case clamps too, so a direct caller (or a test) can't ask
    # for an unbounded or zero page.
    DEFAULT_LIMIT = 25
    MAX_LIMIT = 100

    def __init__(self, repository: EtfSearchRepositoryAdapter) -> None:
        self._repository = repository

    def execute(
        self,
        *,
        query: str | None = None,
        categories: Sequence[str] | None = None,
        sort: EtfSort = EtfSort.NET_ASSETS,
        direction: SortDirection = SortDirection.DESC,
        limit: int | None = None,
        offset: int = 0,
    ) -> EtfSearchPage:
        text = (query or "").strip()
        capped = (
            self.DEFAULT_LIMIT if limit is None else min(max(1, limit), self.MAX_LIMIT)
        )
        criteria = EtfSearchCriteria(
            query=text or None,
            categories=_slugged(categories),
            sort=sort,
            direction=direction,
            limit=capped,
            offset=max(0, offset),
        )
        return self._repository.search(criteria)


class ListEtfCategories:
    def __init__(self, repository: EtfSearchRepositoryAdapter) -> None:
        self._repository = repository

    def execute(self) -> EtfCategories:
        return self._repository.categories()


class AiScreenEtfs:
    def __init__(
        self,
        translator: EtfScreenerQueryAdapter,
        repository: EtfSearchRepositoryAdapter,
    ) -> None:
        self._translator = translator
        # Read only for the allowed vocabulary the translator is constrained to.
        self._repository = repository

    def execute(self, *, query: str) -> EtfScreenIntent:
        text = (query or "").strip()
        if not text:
            raise ValueError("A search request is required.")
        allowed = self._repository.categories()
        return self._translator.translate(text, categories=allowed.categories)


def _normalize_symbol(symbol: str) -> str:
    return normalize_symbol(symbol, kind="ETF", article="An")


def _normalize_includes(include: Sequence[str] | None) -> frozenset[str]:
    if not include:
        return frozenset()
    parts = {
        part.strip().lower()
        for raw in include
        for part in raw.split(",")
        if part.strip()
    }
    unknown = parts - INCLUDABLE
    if unknown:
        raise ValueError(
            f"Unknown include(s): {', '.join(sorted(unknown))}. "
            f"Valid includes: {', '.join(sorted(INCLUDABLE))}."
        )
    return frozenset(parts)


class GetEtfDetail:
    def __init__(
        self,
        lookup: EtfLookupRepositoryAdapter,
        quotes: StockQuoteAdapter,
        performance: StockPerformanceAdapter | None = None,
        profile_provider: EtfProfileAdapter | None = None,
    ) -> None:
        self._lookup = lookup
        self._quotes = quotes
        self._performance = performance
        # Backs the performance block's live 3y/5y returns (no longer stored). Optional/best-effort
        # like the performance provider — an unwired one just leaves the returns null.
        self._profile_provider = profile_provider

    def execute(self, symbol: str, include: Sequence[str] | None = None) -> EtfDetail:
        normalized = _normalize_symbol(symbol)
        wanted = _normalize_includes(include)
        # Membership gate first: not an ETF -> 404, before any upstream call.
        facts = self._lookup.get(normalized)
        if facts is None:
            raise StockNotFound(normalized)
        # Primary source: a quote failure propagates (mapped to 502/503 at the edge).
        quote = self._quotes.get_quote(normalized)
        # Enrichment, read from the DB (populated out-of-band by the sync — no live Yahoo on the
        # read path). A fund not yet enriched yields an empty profile — never an error — so the
        # card still serves. It backs the always-on enrichment as well as the metrics/dividends
        # blocks' figures (nav/yield), which are serialization-gated, not extra calls.
        profile = self._lookup.get_stored_profile(normalized)
        performance = None
        if "performance" in wanted:
            performance = self._get_performance(normalized)
            # The 3y/5y returns this block surfaces are no longer stored — overlay them from a live
            # Yahoo read (best-effort, like the Alpaca windows beside them). Done only here, so no
            # other request path pays a live Yahoo call.
            profile = self._with_live_returns(normalized, profile)
        return EtfDetail.assemble(
            normalized, quote, facts, profile, include=wanted, performance=performance
        )

    def _get_performance(self, symbol: str) -> StockPerformance | None:
        # The Alpaca trailing windows — fetched only when the performance block is requested, and
        # best-effort: a failure leaves the gains null rather than sinking the card whose primary
        # data (the quote) is already in hand.
        if self._performance is None:
            return None
        try:
            return self._performance.get_performance(symbol)
        except (StockNotFound, StockDataUnavailable):
            return None

    def _with_live_returns(self, symbol: str, profile: EtfProfile) -> EtfProfile:
        if self._profile_provider is None:
            return profile
        try:
            live = self._profile_provider.get_profile(symbol)
        except (StockNotFound, StockDataUnavailable):
            return profile
        return replace(
            profile,
            ytd_return=live.ytd_return,
            three_year_return=live.three_year_return,
            five_year_return=live.five_year_return,
        )


class GetEtfAnalysis:
    # The performance block is the only include that enriches the *entity* handed to the model — it
    # adds the Alpaca trailing windows (1w…1y) and the live 3y/5y return ladder. The metrics /
    # dividends includes only gate serialization on the detail *card*; the figures they'd surface
    # (expense ratio, NAV, net assets, yield) are already on every ``EtfDetail`` regardless, so the
    # analysis sees them without asking. So the fullest snapshot is "performance" alone.
    _SNAPSHOT_INCLUDES = ("performance",)

    def __init__(
        self,
        detail: GetEtfDetail,
        analyzer: EtfAnalysisAdapter,
        cache: InvestmentAnalysisCacheAdapter | None = None,
        cache_ttl: timedelta = timedelta(minutes=30),
    ) -> None:
        self._detail = detail
        self._analyzer = analyzer
        self._cache = cache
        self._cache_ttl = cache_ttl

    def execute(self, symbol: str) -> InvestmentAnalysis:
        # Normalize up front so the cache key matches what the analyzer stamps on the
        # result (``EtfDetail.ticker``, the normalized ticker) — a hit here skips both
        # the snapshot build (quote + live 3y/5y returns) and the model call.
        normalized = _normalize_symbol(symbol)
        cached = self._fresh_cached(normalized)
        if cached is not None:
            return cached
        detail = self._detail.execute(normalized, include=self._SNAPSHOT_INCLUDES)
        analysis = self._analyzer.analyze(detail)
        # Cache only a *complete* read (both strengths and risks present), so a rare
        # empty-list model result never freezes for the TTL — the next view regenerates.
        if self._cache is not None and analysis.is_complete:
            self._cache.put(analysis)
        return analysis

    def _fresh_cached(self, symbol: str) -> InvestmentAnalysis | None:
        if self._cache is None:
            return None
        stored = self._cache.get(symbol)
        if stored is None or not self._is_fresh(stored):
            return None
        return stored

    def _is_fresh(self, analysis: InvestmentAnalysis) -> bool:
        generated = analysis.generated_at
        if generated is None:
            return False
        if generated.tzinfo is None:  # a naive stamp (e.g. from SQLite) is UTC
            generated = generated.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) - generated <= self._cache_ttl
