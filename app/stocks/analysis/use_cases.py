"""Application Business Rules: the AI-analysis use cases.

Every AI-generated read the API serves — the enriched stock snapshot that backs
the analyses (``GetStockInfo``), the sectioned stock scorecard, the earnings /
ratings / fundamentals reads, and the market-wide sector + summary. Orchestrate
the flow: validate/normalize the symbol, gather the context through ports, and
hand it to the analyser. Depend only on the entities and the ports — never on a
framework or a concrete provider.
"""

import logging
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from app.stocks.analysis.entities import (
    EarningsAnalysis,
    FundamentalsAnalysis,
    MarketSummary,
    RatingsAnalysis,
    SectorAnalysis,
    StockScorecard,
)
from app.stocks.analysis.ports import (
    AiAnalysisCache,
    EarningsAnalysisProvider,
    FundamentalsAnalysisProvider,
    MarketSummaryProvider,
    RatingsAnalysisProvider,
    SectorAnalysisProvider,
    StockScorecardCache,
    StockScorecardProvider,
)
from app.stocks.earnings.annual.entities import AnnualEarningsTimeline
from app.stocks.earnings.annual.ports import AnnualEarningsProvider
from app.stocks.earnings.quarterly.entities import QuarterlyEarningsTimeline
from app.stocks.earnings.quarterly.ports import QuarterlyEarningsProvider
from app.stocks.entities import (
    AllTimeHigh,
    AnalystEstimates,
    KeyMetrics,
    Stock,
    StockPerformance,
)
from app.stocks.exceptions import StockDataUnavailable, StockNotFound
from app.stocks.market.use_cases import GetMarketOverview, GetSectorPerformance
from app.stocks.ports import (
    AllTimeHighProvider,
    AnalystEstimatesProvider,
    StockDataProvider,
    StockPerformanceProvider,
)
from app.stocks.recommendations.entities import (
    AnalystRatingChanges,
    AnalystRecommendations,
)
from app.stocks.recommendations.ports import (
    RatingChangeProvider,
    RecommendationProvider,
)
from app.stocks.universe.entities import AnchorMetrics, IndustryValuation
from app.stocks.universe.repository import StockSearchRepository

logger = logging.getLogger(__name__)

# The cache key for the market-wide AI reads (sector, market summary), which take no
# symbol — a fixed sentinel so each gets one row in the shared, (kind, symbol)-keyed
# cache. Not a real ticker (underscored), so it can never collide with one.
_MARKET_CACHE_KEY = "_MARKET_"


def _analysis_is_fresh(generated_at: datetime | None, ttl: timedelta) -> bool:
    """Whether a stored AI read is still within its TTL, so a cache hit can be served
    without regenerating. Shared by every cached analysis use case."""
    if generated_at is None:
        return False
    if generated_at.tzinfo is None:  # a naive stamp (e.g. from SQLite) is UTC
        generated_at = generated_at.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - generated_at <= ttl


def _consensus_pe(price: float | None, ttm_eps: float | None) -> float | None:
    """Trailing P/E on the analyst-consensus (adjusted) basis — the live price over the
    quarterly slice's TTM consensus EPS, the exact figure ``TickerValuation.trailing_pe``
    and the universe sync's valuation pass serve.

    ``None`` on a non-positive/absent price or EPS (a trailing loss, or fewer than four
    cached quarters), the same guard those use — so the scorecard's P/E is the canonical
    consensus one or absent, never Finnhub's GAAP ``peTTM``, keeping it on the same basis
    as the industry-median P/E it's weighed against.
    """
    if price is None or ttm_eps is None or price <= 0 or ttm_eps <= 0:
        return None
    return round(price / ttm_eps, 2)


def _price_multiple(price: float | None, per_share: float | None) -> float | None:
    """A price-derived multiple — the live price over a stored per-share input (book value →
    P/B, sales → P/S), the same "store the input, price it live" split the P/E and FCF yield
    use. ``None`` on a non-positive/absent price or per-share figure (P/B off a negative book
    value is meaningless, the same guard the consensus P/E uses on a loss)."""
    if price is None or per_share is None or price <= 0 or per_share <= 0:
        return None
    return round(price / per_share, 2)


def _dividend_yield(dividend_per_share: float | None, price: float | None) -> float | None:
    """Dividend yield (percent) — the stored annual dividend per share over the live price.
    ``None`` without both, or a non-positive price."""
    if dividend_per_share is None or not price or price <= 0:
        return None
    return round(dividend_per_share / price * 100, 2)


def _with_stored_fundamentals(
    stock: Stock, anchor: "AnchorMetrics", ttm_eps: float | None
) -> Stock:
    """Overlay the anchor-materialized fundamentals onto the live snapshot, DB-only, so the
    analysis reads the same canonical figures the ticker card and universe search show — never
    a divergent live-vendor number. Replaces the retired live Finnhub fundamentals + profile
    calls.

    The trailing ratios (margins, ROE, current ratio, debt/equity, beta) and the annual slice's
    cash/growth come straight off the anchor; the price-derived multiples are computed here on
    the live quote — the consensus P/E from the quarterly TTM EPS (``ttm_eps``, ``None`` when no
    quarterly context was gathered), and P/B / P/S from the stored per-share book value / sales.
    ``eps`` is set to the same consensus TTM so the prompt's EPS sits on the P/E's basis. The
    market cap, dividend (per share + a live-priced yield) and clean display name are filled off
    the anchor too, falling back to the price feed's name when the anchor hasn't got one yet.

    Overwrites each field (including to ``None``): an unsynced stock simply carries no
    fundamentals — the thinner coverage reads as lower confidence — rather than a stale or
    divergent figure. Leaves ``metrics`` ``None`` (not an empty block) when nothing resolved, so
    the fundamentals-analysis no-data guard still fires."""
    price = stock.price
    overlay = {
        "gross_margin": anchor.gross_margin,
        "operating_margin": anchor.operating_margin,
        "net_margin": anchor.net_margin,
        "roe": anchor.return_on_equity,
        "current_ratio": anchor.current_ratio,
        "debt_to_equity": anchor.debt_to_equity,
        "beta": anchor.beta,
        "fcf_per_share": anchor.fcf_per_share,
        "revenue_growth_yoy": anchor.revenue_growth_yoy,
        "eps_growth_yoy": anchor.eps_growth_yoy,
        "eps": ttm_eps,
        "pe": _consensus_pe(price, ttm_eps),
        "pb": _price_multiple(price, anchor.book_value_per_share),
        "ps": _price_multiple(price, anchor.sales_per_share),
    }
    if stock.metrics is not None:
        metrics = replace(stock.metrics, **overlay)
    elif any(value is not None for value in overlay.values()):
        metrics = KeyMetrics(**overlay)
    else:
        metrics = None  # nothing resolved — keep it a bare price, not an empty metrics block
    return replace(
        stock,
        metrics=metrics,
        market_cap=anchor.market_cap,
        dividend_per_share=anchor.dividend_per_share,
        dividend_yield=_dividend_yield(anchor.dividend_per_share, price),
        name=anchor.name or stock.name,
    )


def _normalize_symbol(symbol: str) -> str:
    normalized = (symbol or "").strip().upper()
    if not normalized:
        raise ValueError("A stock symbol is required.")
    if not normalized.isalpha() or len(normalized) > 5:
        # Simple guard; real tickers are 1-5 letters (ignoring class suffixes).
        raise ValueError(f"'{symbol}' is not a valid stock symbol.")
    return normalized


class GetStockInfo:
    """Use case: retrieve information about a single stock by its symbol.

    The price snapshot is required; performance, the forward analyst estimates and
    the all-time high are optional, best-effort enrichment. If those sources fail or
    aren't configured, the stock is still returned with those fields left unset.

    The trailing fundamentals (margins, valuation, dividend, market cap) and the
    clean display name are **not** read here any more — they're materialized on the
    ``stocks`` anchor by the fundamentals/universe syncs, and the callers (the AI
    analyses, the only consumers of this use case) overlay them from that one anchor
    read (:func:`_with_stored_fundamentals`). So the snapshot this returns carries
    the live price + performance + forward estimates, and its fundamentals are filled
    downstream from the DB rather than a live vendor.
    """

    def __init__(
        self,
        provider: StockDataProvider,
        performance_provider: StockPerformanceProvider | None = None,
        all_time_high_provider: AllTimeHighProvider | None = None,
        estimates_provider: AnalystEstimatesProvider | None = None,
    ) -> None:
        self._provider = provider
        self._performance_provider = performance_provider
        self._all_time_high_provider = all_time_high_provider
        self._estimates_provider = estimates_provider

    def execute(self, symbol: str) -> Stock:
        normalized = _normalize_symbol(symbol)
        stock = self._provider.get_stock(normalized)  # required; errors propagate
        # The two enrichment reads below are independent Alpaca calls with no ordering
        # between them, so they run concurrently rather than in series. Each is already
        # best-effort (returns None on its own failure), and the SDK's HTTP client is
        # safe to call concurrently for independent reads. Estimates is deliberately kept
        # off the pool — it reads the shared request DB session, which must not be touched
        # from a worker thread, and it's a fast local read rather than a network round-trip.
        with ThreadPoolExecutor(max_workers=2) as pool:
            performance_future = pool.submit(self._performance, normalized)
            all_time_high_future = pool.submit(self._all_time_high, normalized, stock)
            performance = performance_future.result()
            all_time_high = all_time_high_future.result()
        # Name stays the price feed's (the anchor's clean name is overlaid downstream);
        # fundamentals/market-cap/dividend are left unset here and filled from the anchor.
        return replace(
            stock,
            performance=performance,
            analyst_estimates=self._estimates(normalized),
            all_time_high=all_time_high,
        )

    def _performance(self, symbol: str) -> StockPerformance | None:
        if self._performance_provider is None:
            return None
        try:
            return self._performance_provider.get_performance(symbol)
        except (StockNotFound, StockDataUnavailable):
            return None  # best-effort: never sink the price response

    def _all_time_high(self, symbol: str, stock: Stock) -> AllTimeHigh | None:
        if self._all_time_high_provider is None:
            return None
        try:
            high = self._all_time_high_provider.get_all_time_high(symbol)
        except (StockNotFound, StockDataUnavailable):
            return None  # best-effort: never sink the price response
        # "All-time" must include right now. The history feed lags the live trade
        # (it ends a few minutes back), so when the current price has pushed past
        # the recorded peak the stock is setting a new high — the high *is* the
        # live price as of now. Folding it in here keeps all_time_high.price >=
        # price, so the entity's drawdown_from_high never reads positive.
        if stock.price > high.price:
            as_of = stock.as_of.date() if stock.as_of else None
            return replace(high, price=stock.price, reached_on=as_of)
        return high

    def _estimates(self, symbol: str) -> AnalystEstimates | None:
        # Forward analyst estimates back the snapshot's forward P/E; best-effort, so
        # a miss (or an uncovered symbol's empty block) just omits the forward
        # metrics rather than failing the price response.
        if self._estimates_provider is None:
            return None
        try:
            estimates = self._estimates_provider.get_estimates(symbol)
        except (StockNotFound, StockDataUnavailable):
            return None  # best-effort: never sink the price response
        return None if estimates.is_empty else estimates


class GetStockAnalysis:
    """Use case: an AI-generated buy/hold/sell read on a single stock.

    Reuses ``GetStockInfo`` to assemble the enriched snapshot (price plus the
    best-effort performance/fundamentals/trailing+forward valuation enrichment),
    then best-effort layers on the same context the app's own views expose — the
    quarterly and annual earnings timelines, the analyst recommendation trends, and
    the stock's industry P/E benchmark (how its valuation sits against its peers) —
    before asking the injected analyzer to weigh it all. The snapshot and the
    analysis are the primary data — a bad/unknown symbol or a model failure
    propagates — while every context source is best-effort, so a miss on any of
    them leaves the analysis intact rather than failing it. The analyzer reasons
    only over what it's handed; it fetches nothing itself.

    A read-through result cache fronts the whole thing: a fresh stored analysis
    (within ``cache_ttl`` of its ``generated_at``) is returned without gathering or
    calling the model at all, and a freshly-generated one is stored on the way out.
    The cache is optional (``None`` disables it) and best-effort — a read failure
    is a miss and a write failure is swallowed — so it only ever makes the endpoint
    faster, never wrong or unavailable.
    """

    def __init__(
        self,
        stock_info: GetStockInfo,
        analyzer: StockScorecardProvider,
        quarterly_provider: QuarterlyEarningsProvider | None = None,
        annual_provider: AnnualEarningsProvider | None = None,
        recommendations_provider: RecommendationProvider | None = None,
        industry_repository: StockSearchRepository | None = None,
        cache: StockScorecardCache | None = None,
        cache_ttl: timedelta = timedelta(minutes=30),
    ) -> None:
        self._stock_info = stock_info
        self._analyzer = analyzer
        self._quarterly_provider = quarterly_provider
        self._annual_provider = annual_provider
        self._recommendations_provider = recommendations_provider
        self._industry_repository = industry_repository
        self._cache = cache
        self._cache_ttl = cache_ttl

    def execute(self, symbol: str) -> StockScorecard:
        normalized = _normalize_symbol(symbol)
        # A fresh cached read short-circuits the whole gather + model call — the
        # scorecard only drifts as the figures do, so a repeat view within the TTL
        # (and any burst of viewers) is served straight from the store.
        cached = self._fresh_cached(normalized)
        if cached is not None:
            return cached
        # The enriched snapshot is primary: a bad symbol (ValueError), an unknown
        # one (StockNotFound), or an upstream failure (StockDataUnavailable) all
        # propagate rather than yielding a scorecard of nothing. Everything else is
        # best-effort context assembled below.
        stock = self._stock_info.execute(normalized)
        # Gathered before the overlay so its TTM EPS can price the snapshot's trailing
        # P/E on the consensus basis (the analyzer needs it either way as the beat-history
        # context).
        quarterly = self._quarterly(normalized)
        stock = self._with_stored_metrics(stock, normalized, quarterly)
        scorecard = self._analyzer.analyze(
            stock,
            quarterly,
            self._annual(normalized),
            self._recommendations(normalized),
            self._industry_valuation(normalized),
        )
        # Store for the next viewer — but only a *complete* read (every section
        # present with a non-empty summary). Refusing to cache an incomplete
        # scorecard means a rare model miss is never frozen for the TTL: the next
        # view regenerates instead of serving a blank section. Best-effort by
        # contract (a write failure is swallowed in the adapter), so it never sinks
        # the scorecard.
        if self._cache is not None and scorecard.is_complete:
            self._cache.put(scorecard)
        return scorecard

    def _fresh_cached(self, symbol: str) -> StockScorecard | None:
        # A stored read is a hit only while it's within the TTL; past that it's
        # stale and we regenerate (overwriting it). A cache-read failure degrades to
        # a miss in the adapter, so this simply returns None and we regenerate.
        if self._cache is None:
            return None
        stored = self._cache.get(symbol)
        if stored is None or not self._is_fresh(stored):
            return None
        return stored

    def _is_fresh(self, scorecard: StockScorecard) -> bool:
        return _analysis_is_fresh(scorecard.generated_at, self._cache_ttl)

    def _with_stored_metrics(
        self, stock: Stock, symbol: str, quarterly: QuarterlyEarningsTimeline | None
    ) -> Stock:
        # Overlay the whole trailing-fundamentals block onto the live snapshot from the one
        # anchor read — the DB-canonical figures the ticker card and universe search show,
        # never a divergent (or now retired) live-vendor number. See
        # ``_with_stored_fundamentals`` for the field-by-field rules. Best-effort — an
        # unconfigured repository or a failed read leaves the snapshot untouched.
        if self._industry_repository is None:
            return stock
        try:
            anchor = self._industry_repository.anchor_metrics_for_ticker(symbol)
        except (StockNotFound, StockDataUnavailable):
            return stock
        ttm_eps = quarterly.ttm_eps if quarterly is not None else None
        return _with_stored_fundamentals(stock, anchor, ttm_eps)

    def _quarterly(self, symbol: str) -> QuarterlyEarningsTimeline | None:
        # Best-effort context: the beat history sharpens the analysis but isn't
        # required, so a missing provider, an upstream miss, or an uncovered
        # symbol (empty timeline) simply omits it.
        if self._quarterly_provider is None:
            return None
        try:
            timeline = self._quarterly_provider.get_quarterly_earnings(symbol)
        except (StockNotFound, StockDataUnavailable):
            return None
        return None if timeline.is_empty else timeline

    def _annual(self, symbol: str) -> AnnualEarningsTimeline | None:
        # Best-effort context, same stance as the quarterly timeline: an
        # unconfigured provider, an upstream miss, or an uncovered symbol (empty
        # timeline) just omits the annual history.
        if self._annual_provider is None:
            return None
        try:
            timeline = self._annual_provider.get_annual_earnings(symbol)
        except (StockNotFound, StockDataUnavailable):
            return None
        return None if timeline.is_empty else timeline

    def _recommendations(self, symbol: str) -> AnalystRecommendations | None:
        # Best-effort context: the sell-side's own buy/hold/sell consensus, the
        # same DB-cached read the recommendations endpoint serves. A missing
        # provider, an upstream miss, or an uncovered symbol (empty run) omits it.
        if self._recommendations_provider is None:
            return None
        try:
            recs = self._recommendations_provider.get_recommendations(symbol)
        except (StockNotFound, StockDataUnavailable):
            return None
        return None if recs.is_empty else recs

    def _industry_valuation(self, symbol: str) -> IndustryValuation | None:
        # Best-effort context: the peer-valuation anchor that makes the stock's own
        # trailing P/E meaningful ("28 is high for an industry that trades near 21").
        # Reads on the shared anchor — resolve the ticker's industry and its own size
        # tier, then summarize its peers' P/Es into a benchmark scoped to that tier
        # (a mega-cap judged against mega-caps), widening to neighbouring tiers when
        # the same-tier sample is thin (see IndustryValuation.for_stock_peers). An
        # unconfigured repository, an unscreened/unclassified symbol (no industry),
        # or a cohort still too thin to stand for its peers (fewer than
        # MIN_REPRESENTATIVE_PEERS — a "median" of one or two stocks is noise, not an
        # anchor) all omit it rather than handing the model a figure it would
        # over-trust.
        if self._industry_repository is None:
            return None
        try:
            industry = self._industry_repository.industry_for_ticker(symbol)
            if not industry:
                return None
            anchor_tier = self._industry_repository.tier_for_ticker(symbol)
            peers = self._industry_repository.industry_peers(industry)
        except (StockNotFound, StockDataUnavailable):
            return None
        valuation = IndustryValuation.for_stock_peers(industry, anchor_tier, peers)
        return valuation if valuation.is_representative else None


class GetEarningsAnalysis:
    """Use case: an AI-generated, plain-language read of a stock's earnings story.

    The earnings-focused sibling of ``GetStockAnalysis``. Gathers the quarterly and
    annual earnings timelines — read **DB-only** (via the slices' repositories, not
    their read-through providers), so a cache miss never triggers a synchronous,
    rate-limited Yahoo fetch mid-request — and hands them to the injected analyzer.
    The analysis is the primary data, so a model failure propagates; a symbol with
    no earnings on file surfaces as ``StockDataUnavailable`` rather than an analysis
    of nothing. The analyzer reasons only over what it's handed; it fetches nothing
    itself.

    A read-through result cache fronts the whole thing (like the per-stock analysis): a
    fresh stored read within ``cache_ttl`` is served without gathering or calling the
    model at all, and a freshly-generated one is stored on the way out. The cache is
    optional (``None`` disables it) and best-effort — a read failure is a miss and a
    write failure is swallowed — so it only ever makes the endpoint faster.
    """

    def __init__(
        self,
        analyzer: EarningsAnalysisProvider,
        quarterly_provider: QuarterlyEarningsProvider | None = None,
        annual_provider: AnnualEarningsProvider | None = None,
        cache: AiAnalysisCache[EarningsAnalysis] | None = None,
        cache_ttl: timedelta = timedelta(minutes=30),
    ) -> None:
        self._analyzer = analyzer
        self._quarterly_provider = quarterly_provider
        self._annual_provider = annual_provider
        self._cache = cache
        self._cache_ttl = cache_ttl

    def execute(self, symbol: str) -> EarningsAnalysis:
        normalized = _normalize_symbol(symbol)
        # A fresh cached read short-circuits the whole DB gather + model call — the
        # read only drifts as the earnings figures do, so a repeat view within the TTL
        # (and any burst of viewers) is served straight from the store.
        cached = self._fresh_cached(normalized)
        if cached is not None:
            return cached
        quarterly = self._quarterly(normalized)
        annual = self._annual(normalized)
        # Nothing on file for either timeline — an uncovered/unknown symbol. Fail
        # rather than ask the model to reason over an empty slate.
        if quarterly is None and annual is None:
            raise StockDataUnavailable(normalized, "no earnings data to analyse")
        analysis = self._analyzer.analyze(normalized, quarterly, annual)
        # Store for the next viewer — but only a complete read, so a rare empty model
        # result is never frozen for the TTL. Best-effort (a write failure is swallowed
        # in the adapter), so it never sinks the analysis.
        if self._cache is not None and analysis.is_complete:
            self._cache.put(normalized, analysis)
        return analysis

    def _fresh_cached(self, symbol: str) -> EarningsAnalysis | None:
        if self._cache is None:
            return None
        stored = self._cache.get(symbol)
        if stored is None or not _analysis_is_fresh(stored.generated_at, self._cache_ttl):
            return None
        return stored

    def _quarterly(self, symbol: str) -> QuarterlyEarningsTimeline | None:
        if self._quarterly_provider is None:
            return None
        try:
            timeline = self._quarterly_provider.get_quarterly_earnings(symbol)
        except (StockNotFound, StockDataUnavailable):
            return None
        return None if timeline.is_empty else timeline

    def _annual(self, symbol: str) -> AnnualEarningsTimeline | None:
        if self._annual_provider is None:
            return None
        try:
            timeline = self._annual_provider.get_annual_earnings(symbol)
        except (StockNotFound, StockDataUnavailable):
            return None
        return None if timeline.is_empty else timeline


class GetRatingsFindings:
    """Use case: an AI-generated, plain-language read of a stock's analyst coverage.

    The analyst-ratings sibling of ``GetEarningsAnalysis``. Gathers the recommendation
    consensus (trends + price targets) and the discrete rating-change events — both read
    **DB-only** (via the recommendations slice's repositories, not their read-through
    providers), so a cache miss never triggers a synchronous, rate-limited Yahoo fetch
    mid-request — derives the most credible covering firms from the events, and hands the lot
    to the injected analyzer. The analysis is the primary data, so a model failure propagates;
    a symbol with no coverage to render (no consensus trends and no credible covering firm)
    surfaces as ``StockDataUnavailable`` rather than an analysis of nothing. The analyzer
    reasons only over what it's handed; it fetches nothing itself. Like the earnings read, a
    best-effort read-through result cache fronts it — a fresh stored read within ``cache_ttl``
    skips the whole gather + model call.
    """

    # How many credible covering firms to surface for the model — matches the card's top-firms.
    _TOP_FIRMS = 10

    def __init__(
        self,
        analyzer: RatingsAnalysisProvider,
        recommendations_provider: RecommendationProvider | None = None,
        rating_change_provider: RatingChangeProvider | None = None,
        cache: AiAnalysisCache[RatingsAnalysis] | None = None,
        cache_ttl: timedelta = timedelta(minutes=30),
        *,
        now: datetime | None = None,
    ) -> None:
        self._analyzer = analyzer
        self._recommendations_provider = recommendations_provider
        self._rating_change_provider = rating_change_provider
        self._cache = cache
        self._cache_ttl = cache_ttl
        self._now = now  # injectable clock for tests; None → real now per call

    def execute(self, symbol: str) -> RatingsAnalysis:
        normalized = _normalize_symbol(symbol)
        # A fresh cached read short-circuits the whole DB gather + model call.
        cached = self._fresh_cached(normalized)
        if cached is not None:
            return cached
        recommendations = self._recommendations(normalized)
        rating_changes = self._rating_changes(normalized)
        # Only surface firms whose latest target is within the last year, matching the card.
        today = (self._now or datetime.now(timezone.utc)).date()
        top_firms = rating_changes.top_credible_firms(self._TOP_FIRMS, as_of=today)
        # Nothing the prompt can render — no consensus trends and no credible covering firm.
        # Fail rather than ask the model to analyse an empty slate. (Top firms derive from the
        # events, so this also covers a symbol with only uncredited firms' actions.)
        if (recommendations is None or recommendations.is_empty) and not top_firms:
            raise StockDataUnavailable(normalized, "no analyst coverage to analyse")
        analysis = self._analyzer.analyze(normalized, recommendations, top_firms)
        # Store for the next viewer — complete reads only, best-effort (see GetEarningsAnalysis).
        if self._cache is not None and analysis.is_complete:
            self._cache.put(normalized, analysis)
        return analysis

    def _fresh_cached(self, symbol: str) -> RatingsAnalysis | None:
        if self._cache is None:
            return None
        stored = self._cache.get(symbol)
        if stored is None or not _analysis_is_fresh(stored.generated_at, self._cache_ttl):
            return None
        return stored

    def _recommendations(self, symbol: str) -> AnalystRecommendations | None:
        # Best-effort context, DB-only: the sell-side consensus, or None on a miss/empty run.
        if self._recommendations_provider is None:
            return None
        try:
            recs = self._recommendations_provider.get_recommendations(symbol)
        except (StockNotFound, StockDataUnavailable):
            return None
        return None if recs.is_empty else recs

    def _rating_changes(self, symbol: str) -> AnalystRatingChanges:
        # Best-effort context, DB-only: the rating-change events the top firms derive from, or
        # an empty run on a miss so the top-firms read is simply empty.
        if self._rating_change_provider is None:
            return AnalystRatingChanges(symbol)
        try:
            return self._rating_change_provider.get_rating_changes(symbol)
        except (StockNotFound, StockDataUnavailable):
            return AnalystRatingChanges(symbol)


class GetFundamentalsAnalysis:
    """Use case: an AI-generated, plain-language read of a stock's fundamentals.

    The fundamentals-focused sibling of ``GetEarningsAnalysis`` and ``GetRatingsFindings``.
    Reuses ``GetStockInfo`` to assemble the enriched snapshot — the trailing valuation/health
    metrics, the forward analyst estimates, the dividend and market cap — then best-effort layers
    on the stock's industry-P/E benchmark (the same peer anchor ``GetStockAnalysis`` uses, so a
    valuation multiple reads against its peers rather than in a vacuum) before handing the lot to
    the injected analyzer.

    The snapshot is primary — a bad/unknown symbol or an upstream price failure propagates — but a
    snapshot carrying *no* fundamentals at all (no metrics, no estimates, no dividend, no market
    cap: an uncovered symbol or an unconfigured fundamentals vendor) surfaces as
    ``StockDataUnavailable`` rather than asking the model to reason over a bare price. The industry
    benchmark is best-effort, so a miss just omits it. Like the per-stock analysis, a best-effort
    read-through result cache fronts it — a fresh stored read within ``cache_ttl`` skips the whole
    snapshot gather + model call, matching the earnings and ratings reads. The analyzer reasons only
    over what it's handed; it fetches nothing itself.
    """

    def __init__(
        self,
        stock_info: GetStockInfo,
        analyzer: FundamentalsAnalysisProvider,
        industry_repository: StockSearchRepository | None = None,
        quarterly_provider: QuarterlyEarningsProvider | None = None,
        cache: AiAnalysisCache[FundamentalsAnalysis] | None = None,
        cache_ttl: timedelta = timedelta(minutes=30),
    ) -> None:
        self._stock_info = stock_info
        self._analyzer = analyzer
        self._industry_repository = industry_repository
        self._quarterly_provider = quarterly_provider
        self._cache = cache
        self._cache_ttl = cache_ttl

    def execute(self, symbol: str) -> FundamentalsAnalysis:
        normalized = _normalize_symbol(symbol)
        # A fresh cached read short-circuits the whole snapshot gather + model call.
        cached = self._fresh_cached(normalized)
        if cached is not None:
            return cached
        # The enriched snapshot is primary: a bad symbol (ValueError), an unknown one
        # (StockNotFound), or an upstream price failure (StockDataUnavailable) all propagate
        # rather than yielding an analysis of nothing. The trailing fundamentals it carries are
        # overlaid from the anchor (the DB-canonical figures, replacing the retired live vendor).
        stock = self._with_stored_metrics(
            self._stock_info.execute(normalized), normalized
        )
        if not _has_fundamentals(stock):
            # Only a price came back — no valuation/health metrics, no forward estimates, no
            # dividend or market cap. Nothing fundamental to read, so fail rather than ask the
            # model to reason over a bare quote (mirrors the earnings/ratings no-data guards).
            raise StockDataUnavailable(normalized, "no fundamentals data to analyse")
        analysis = self._analyzer.analyze(stock, self._industry_valuation(normalized))
        # Store for the next viewer — complete reads only, best-effort (see GetEarningsAnalysis).
        if self._cache is not None and analysis.is_complete:
            self._cache.put(normalized, analysis)
        return analysis

    def _fresh_cached(self, symbol: str) -> FundamentalsAnalysis | None:
        if self._cache is None:
            return None
        stored = self._cache.get(symbol)
        if stored is None or not _analysis_is_fresh(stored.generated_at, self._cache_ttl):
            return None
        return stored

    def _industry_valuation(self, symbol: str) -> IndustryValuation | None:
        # Best-effort context: the peer-valuation anchor that makes the stock's own P/E
        # meaningful ("28 is high for an industry that trades near 21"). Identical to
        # ``GetStockAnalysis._industry_valuation`` — resolve the ticker's industry and size
        # tier, summarize its peers' P/Es into a tier-scoped benchmark, and only surface it when
        # the cohort is representative (a "median" of one or two stocks is noise, not an anchor).
        if self._industry_repository is None:
            return None
        try:
            industry = self._industry_repository.industry_for_ticker(symbol)
            if not industry:
                return None
            anchor_tier = self._industry_repository.tier_for_ticker(symbol)
            peers = self._industry_repository.industry_peers(industry)
        except (StockNotFound, StockDataUnavailable):
            return None
        valuation = IndustryValuation.for_stock_peers(industry, anchor_tier, peers)
        return valuation if valuation.is_representative else None

    def _with_stored_metrics(self, stock: Stock, symbol: str) -> Stock:
        # Same anchor overlay the per-stock scorecard uses (``GetStockAnalysis``): fill the
        # trailing fundamentals from the one anchor read, DB-only. The consensus P/E needs the
        # quarterly TTM EPS, so it's read here too (DB-only context, best-effort). Best-effort —
        # an unconfigured repository or a failed read leaves the snapshot untouched.
        if self._industry_repository is None:
            return stock
        try:
            anchor = self._industry_repository.anchor_metrics_for_ticker(symbol)
        except (StockNotFound, StockDataUnavailable):
            return stock
        quarterly = self._quarterly(symbol)
        ttm_eps = quarterly.ttm_eps if quarterly is not None else None
        return _with_stored_fundamentals(stock, anchor, ttm_eps)

    def _quarterly(self, symbol: str) -> QuarterlyEarningsTimeline | None:
        # DB-only context for the consensus P/E's TTM EPS; a missing provider or an uncovered
        # symbol just leaves the P/E null.
        if self._quarterly_provider is None:
            return None
        try:
            timeline = self._quarterly_provider.get_quarterly_earnings(symbol)
        except (StockNotFound, StockDataUnavailable):
            return None
        return None if timeline.is_empty else timeline


def _has_fundamentals(stock: Stock) -> bool:
    """Whether an enriched snapshot carries anything fundamental to analyse.

    True when at least one fundamentals source contributed — the trailing metrics block, the
    forward estimates, a dividend, or a market cap. A snapshot with none of these is a bare
    price (an uncovered symbol or an unconfigured fundamentals vendor), which the use case
    refuses to hand the model."""
    return (
        stock.metrics is not None
        or stock.analyst_estimates is not None
        or stock.dividend_yield is not None
        or stock.dividend_per_share is not None
        or stock.market_cap is not None
    )


class GetSectorAnalysis:
    """Use case: an AI-generated read of which market sectors are leading today.

    The market-wide sibling of ``GetStockAnalysis``. Reuses
    ``GetSectorPerformance`` to assemble the day's ranked board, then hands it to
    the injected analyzer. Both the board and the analysis are primary data — an
    upstream board failure (``StockNotFound``/``StockDataUnavailable``) or a model
    failure propagates rather than yielding an analysis of nothing. The analyzer
    reasons only over the board it's handed; it fetches nothing itself. Takes no
    input — it reports on the whole market.
    """

    def __init__(
        self,
        sectors: GetSectorPerformance,
        analyzer: SectorAnalysisProvider,
        cache: AiAnalysisCache[SectorAnalysis] | None = None,
        cache_ttl: timedelta = timedelta(minutes=30),
    ) -> None:
        self._sectors = sectors
        self._analyzer = analyzer
        self._cache = cache
        self._cache_ttl = cache_ttl

    def execute(self) -> SectorAnalysis:
        # A fresh cached read short-circuits the whole board gather + model call — this
        # is market-wide, so one stored read serves every viewer within the TTL. Keyed
        # on the market sentinel, since the read takes no symbol.
        cached = self._fresh_cached()
        if cached is not None:
            return cached
        # Timed in two halves so the logs decompose the endpoint's latency into its
        # only two moving parts: the multi-source board gather (Alpaca) and the
        # model call (Bedrock). This is the ground truth for "where do the seconds
        # go", rather than guessing which leg dominates.
        gather_start = time.perf_counter()
        board = self._sectors.execute()
        gather_ms = (time.perf_counter() - gather_start) * 1000

        # Log in a `finally` so a failing/slow model call (e.g. a 502 from an
        # unentitled model) still records the split — a line that only fires on
        # success would go missing in exactly the case we most want to diagnose.
        model_start = time.perf_counter()
        analysis: SectorAnalysis | None = None
        try:
            analysis = self._analyzer.analyze(board)
            # Store for the next viewer — complete reads only, best-effort (a write
            # failure is swallowed in the adapter), so it never sinks the analysis.
            if self._cache is not None and analysis.is_complete:
                self._cache.put(_MARKET_CACHE_KEY, analysis)
            return analysis
        finally:
            model_ms = (time.perf_counter() - model_start) * 1000
            if analysis is not None:
                logger.info(
                    "sector analysis timing: board_gather=%.0fms model_call=%.0fms "
                    "total=%.0fms (model=%s)",
                    gather_ms,
                    model_ms,
                    gather_ms + model_ms,
                    analysis.model,
                )
            else:
                logger.info(
                    "sector analysis timing: board_gather=%.0fms model_call=%.0fms "
                    "-> model call failed",
                    gather_ms,
                    model_ms,
                )

    def _fresh_cached(self) -> SectorAnalysis | None:
        if self._cache is None:
            return None
        stored = self._cache.get(_MARKET_CACHE_KEY)
        if stored is None or not _analysis_is_fresh(stored.generated_at, self._cache_ttl):
            return None
        return stored


class GetMarketSummary:
    """Use case: an AI-generated overview of how the US market has moved lately.

    The market-wide sibling of ``GetSectorAnalysis``. Reuses ``GetMarketOverview``
    to assemble the day's index board, then hands it to the injected analyzer.
    Both the board and the summary are primary data — an upstream board failure
    (``StockNotFound``/``StockDataUnavailable``) or a model failure propagates
    rather than yielding a summary of nothing. The analyzer reasons only over the
    board it's handed; it fetches nothing itself. Takes no input — it reports on
    the whole market.
    """

    def __init__(
        self,
        overview: GetMarketOverview,
        analyzer: MarketSummaryProvider,
        cache: AiAnalysisCache[MarketSummary] | None = None,
        cache_ttl: timedelta = timedelta(minutes=30),
    ) -> None:
        self._overview = overview
        self._analyzer = analyzer
        self._cache = cache
        self._cache_ttl = cache_ttl

    def execute(self) -> MarketSummary:
        # A fresh cached read short-circuits the whole board gather + model call — this
        # is market-wide, so one stored read serves every viewer within the TTL. Keyed
        # on the market sentinel, since the read takes no symbol.
        cached = self._fresh_cached()
        if cached is not None:
            return cached
        # Timed in two halves so the logs decompose the endpoint's latency into its
        # only two moving parts: the index-board gather (Alpaca) and the model call
        # (Bedrock) — the same split the sector-analysis use case records.
        gather_start = time.perf_counter()
        board = self._overview.execute()
        gather_ms = (time.perf_counter() - gather_start) * 1000

        # Log in a `finally` so a failing/slow model call still records the split.
        model_start = time.perf_counter()
        summary: MarketSummary | None = None
        try:
            summary = self._analyzer.analyze(board)
            # Store for the next viewer — complete reads only, best-effort (see above).
            if self._cache is not None and summary.is_complete:
                self._cache.put(_MARKET_CACHE_KEY, summary)
            return summary
        finally:
            model_ms = (time.perf_counter() - model_start) * 1000
            if summary is not None:
                logger.info(
                    "market summary timing: board_gather=%.0fms model_call=%.0fms "
                    "total=%.0fms (model=%s)",
                    gather_ms,
                    model_ms,
                    gather_ms + model_ms,
                    summary.model,
                )
            else:
                logger.info(
                    "market summary timing: board_gather=%.0fms model_call=%.0fms "
                    "-> model call failed",
                    gather_ms,
                    model_ms,
                )

    def _fresh_cached(self) -> MarketSummary | None:
        if self._cache is None:
            return None
        stored = self._cache.get(_MARKET_CACHE_KEY)
        if stored is None or not _analysis_is_fresh(stored.generated_at, self._cache_ttl):
            return None
        return stored
