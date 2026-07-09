"""Application use cases for the ETF slice.

Pure orchestration over the ports so each runs offline in tests against hand-written fakes and
knows nothing of Yahoo, HTTP, or SQLAlchemy:

- ``SyncEtfs`` — the out-of-band populator. Two passes in one run: (1) screen the top US ETFs
  and upsert the result into the ``etfs`` table (additive: it never removes a fund); (2) enrich
  the stored funds with their full profile — **all** of them by default, or up to ``limit`` when
  the caller throttles — fetching each fund's profile (category, fund family, dividend yield, NAV,
  description, top holdings, sector weightings) through a single per-ticker call and persisting it
  (scalars onto the row, the two lists into their child tables — the trailing returns ride the same
  fetch but are not stored; the detail card reads those live). The write is
  merge-preserving, so a fund whose fetch hard-fails is simply skipped and retried next run — its
  stored profile is left intact. Invoked by the (fire-and-forget) cron endpoint. Guarded so a
  blocked/truncated screen (empty or implausibly small) skips *both* passes rather than churning a
  partial set or hammering the same blocked vendor with per-ticker calls.
- ``SearchEtfs`` — the read side (``GET /stocks/etfs``): normalize a search request at the edge
  and hand the read repository a clean ``EtfSearchCriteria``, returning the matched page. No live
  feed — the set is already in the table.
- ``ListEtfCategories`` — the filter-menu read (``GET /stocks/etfs/categories``): the distinct
  category slugs the FE offers, straight from the repository.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Sequence

from app.stocks.entities import InvestmentAnalysis, StockPerformance
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
from app.stocks.etfs.ports import EtfAnalysisProvider, EtfProfileProvider, EtfScreener
from app.stocks.etfs.repository import (
    EtfLookupRepository,
    EtfRepository,
    EtfSearchRepository,
)
from app.stocks.exceptions import StockDataUnavailable, StockNotFound
from app.stocks.ports import (
    InvestmentAnalysisCache,
    StockPerformanceProvider,
    StockQuoteProvider,
)
from app.stocks.progress import iter_with_progress

logger = logging.getLogger(__name__)


def _slugged(values: Sequence[str] | None) -> tuple[str, ...]:
    """Slug each label to the stored convention, dropping blanks/non-strings and de-duplicating
    while preserving order — the multi-select edge for the ``category`` filter. Each value may be
    the slug or the raw label (``slugify`` normalizes both), and the param repeats to OR several at
    once (``?category=large_growth&category=large_blend``)."""
    if not values:
        return ()
    return tuple(dict.fromkeys(s for v in values if (s := slugify(v)) is not None))


# The blocks a caller may opt into on the ETF detail card (``?include=``). Everything else — the
# quote + day move, the stored identity facts (name/exchange/category), and the always-on Yahoo
# enrichment (fund family, description, holdings, sector weightings) — is served regardless.
INCLUDABLE = frozenset({"metrics", "dividends", "performance"})


@dataclass(frozen=True)
class EtfSyncReport:
    """The outcome of one sync run.

    ``screened`` is the screen size and ``added`` / ``updated`` the rows the screen upsert
    inserted / refreshed. ``enriched`` is how many funds the enrichment pass fetched and persisted
    a profile for this run and ``enrich_failed`` how many per-ticker lookups the source couldn't
    serve (an outage or block) — both zero when the screen was skipped. ``enriched_without_holdings``
    is the subset of ``enriched`` whose profile came back with **neither** holdings nor sector
    weightings — the ``funds_data``-blocked signature (both ride Yahoo's one ``topHoldings``
    response, and nearly every real ETF has them), so a non-trivial value flags a run where Yahoo
    gated the holdings surface even though ``.info`` served; it's a health signal, not a hard
    failure (those funds still got their scalar profile). ``skipped`` is ``True`` when the screen
    came back empty or implausibly small (a truncated or blocked fetch) so *nothing* was written;
    the other counts are then all zero. There is no ``removed`` count — the sync is additive.
    """

    screened: int
    added: int
    updated: int
    skipped: bool
    enriched: int
    enrich_failed: int
    enriched_without_holdings: int


class SyncEtfs:
    """Populate/refresh the searchable ETF set from a live top-ETFs screen, then enrich each stored
    fund with its full profile."""

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
        screener: EtfScreener,
        repository: EtfRepository,
        profile_provider: EtfProfileProvider,
    ) -> None:
        self._screener = screener
        self._repository = repository
        self._profile_provider = profile_provider

    def execute(self, *, limit: int | None = None) -> EtfSyncReport:
        """Screen the top ETFs, upsert the result, then refresh each stored fund's profile.

        ``limit`` caps how many funds the enrichment pass refreshes this run; ``None`` (the
        default) refreshes **every** stored fund in the one run. A caller (the cron endpoint)
        passes a value only to throttle a run — e.g. if Yahoo starts rate-limiting the per-ticker
        calls.

        A hard screen failure (``StockDataUnavailable``) propagates to the caller (the background
        runner logs it). A *degraded* screen — fewer than ``MIN_PLAUSIBLE_SCREEN`` funds — is
        skipped so a partial/blocked fetch isn't written, and the enrichment pass is skipped too
        (if the one bulk screen call was blocked, the per-ticker calls would be as well).
        Otherwise the whole screen is upserted (additive) and the enrichment pass runs. A single
        fund's profile-fetch failure never aborts the run — it's counted and the sweep continues.
        """
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
        """Fetch and persist each stored fund's profile — up to ``limit`` of them, stalest first,
        or **all** of them when ``limit`` is ``None``. Returns
        ``(enriched, failed, without_holdings)``: ``enriched`` persisted a profile, ``failed``
        couldn't reach the source, and ``without_holdings`` is the subset of ``enriched`` whose
        profile carried neither holdings nor sector weightings (the ``funds_data``-blocked
        signature — surfaced so a degraded run is visible in the logs). A hard per-ticker failure
        leaves the fund's stored profile untouched (the write is merge-preserving and simply isn't
        called), so a bad Yahoo day delays a fund's refresh but never erases it; the next run
        retries it."""
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
        categories: Sequence[str] | None = None,
        sort: EtfSort = EtfSort.NET_ASSETS,
        direction: SortDirection = SortDirection.DESC,
        limit: int | None = None,
        offset: int = 0,
    ) -> EtfSearchPage:
        """Normalize the inputs once, at the edge, then run the search.

        ``query`` is trimmed (blank → no text filter); ``categories`` is each slugged to the
        stored convention with :func:`slugify` (so both the raw label and the stored slug match),
        blanks dropped and duplicates collapsed — empty = don't filter, otherwise match *any* of
        the slugs (an OR set, so several categories can be screened at once); ``limit`` defaults to
        ``DEFAULT_LIMIT`` and is clamped to ``[1, MAX_LIMIT]``, ``offset`` floored at 0. The
        sort/direction pass through as-is (already validated enums). The repository does the rest.
        """
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


def _normalize_includes(include: Sequence[str] | None) -> frozenset[str]:
    """Flatten/lower-case the requested includes and reject unknown ones, once, at the edge — the
    same stance (and client idioms) as the ticker card's ``_normalize_includes``. Accepts both
    repeated params and comma-separated values (``?include=metrics&include=dividends`` or
    ``?include=metrics,dividends``)."""
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
    """Use case: one fund's detail card — the live quote, the stored ``etfs`` facts, and the stored
    profile (``GET /stocks/etf/{ticker}``).

    "not an ETF"), *before* any quote call, so a stock or a bogus ticker costs nothing upstream.
    Then the live quote is fetched and is **primary** — a quote failure propagates (the endpoint
    maps it to the same 502/503 the quote endpoints use), because a detail card with no price isn't
    worth serving. The profile is read from the DB (the sync's enrichment pass populates it): a fund
    the pass hasn't reached yet just yields an empty profile, and the card still returns 200 with the
    quote + stored facts. The stored net_assets/expense figures (screen facts) win over the profile's
    where both exist (the detail page must agree with the screener list); the profile only fills the
    gaps. The one exception to the DB-read profile is the trailing-return ladder (3y/5y), no longer
    stored and overlaid from a live Yahoo read — see ``performance`` below.

    The opt-in blocks (``?include=``) shape what the card carries: ``metrics`` (expense ratio, NAV,
    net assets) and ``dividends`` (yield) are drawn from the DB-read profile + stored facts, so
    requesting them costs no *extra* call — only their serialization is gated. ``performance``
    (the trailing price-return windows) is fetched only when asked for and carries **two** upstream
    calls, both best-effort (a blocked read leaves the affected figures null without sinking the
    card): the Alpaca trailing windows, and — since the 3y/5y annualized returns it surfaces are no
    longer stored — a live Yahoo profile read whose return ladder is overlaid onto the profile. This
    is the only block with a live Yahoo call on the read path, and only when it's requested.
    """

    def __init__(
        self,
        lookup: EtfLookupRepository,
        quotes: StockQuoteProvider,
        performance: StockPerformanceProvider | None = None,
        profile_provider: EtfProfileProvider | None = None,
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
        """Overlay the fund's trailing-return ladder (ytd/3y/5y) onto the DB-read ``profile`` from a
        live Yahoo read — the sole live Yahoo call on the read path, made only for the performance
        block. Best-effort like the Alpaca windows: a blocked/failed read (Yahoo IP-gates
        data-centre IPs intermittently) leaves the returns null without sinking the card. An unwired
        provider leaves them null too."""
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
    """Use case: an AI-generated buy/hold/sell read on one fund (``GET /stocks/etf/{ticker}/analysis``).

    The ETF analogue of the stock slice's ``GetStockAnalysis``: it reuses ``GetEtfDetail`` to
    assemble the fund's snapshot (the live quote, the stored ``etfs`` facts, and the best-effort
    Yahoo profile), then hands that whole ``EtfDetail`` to the analyzer for a plain-language read.
    Composing ``GetEtfDetail`` (rather than re-wiring the lookup/quote/profile ports) keeps the two
    endpoints' primary data identical — the analysis reasons over exactly what the detail card
    shows.

    The detail is **primary**: its normalization (a bad ticker → ``ValueError`` → 400), its
    membership gate (not an ETF → ``StockNotFound`` → 404), and its quote-primary failure
    (``StockDataUnavailable`` → 502) all propagate unchanged — an analysis with no snapshot to
    reason over isn't worth serving. The profile enrichment (holdings, sectors, fund family, NAV,
    yield) is best-effort inside ``GetEtfDetail`` as ever, so a fund the sync hasn't enriched yet
    still gets analysed off its quote + stored facts, just with thinner context (and the model
    lowers its confidence accordingly).

    A read-through result cache fronts the whole thing, exactly as on the stock analysis: a fresh
    stored read (within ``cache_ttl`` of its ``generated_at``) skips the snapshot build and the
    model call, and a freshly-generated one is stored on the way out. Optional and best-effort, so
    it only makes the endpoint faster, never wrong or unavailable.
    """

    # The performance block is the only include that enriches the *entity* handed to the model — it
    # adds the Alpaca trailing windows (1w…1y) and the live 3y/5y return ladder. The metrics /
    # dividends includes only gate serialization on the detail *card*; the figures they'd surface
    # (expense ratio, NAV, net assets, yield) are already on every ``EtfDetail`` regardless, so the
    # analysis sees them without asking. So the fullest snapshot is "performance" alone.
    _SNAPSHOT_INCLUDES = ("performance",)

    def __init__(
        self,
        detail: GetEtfDetail,
        analyzer: EtfAnalysisProvider,
        cache: InvestmentAnalysisCache | None = None,
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
        if self._cache is not None:
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
