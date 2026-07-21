import logging
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from app.stocks.ai.analysis.entities import (
    EarningsAnalysis,
    FundamentalsAnalysis,
    MarketSummary,
    RatingsAnalysis,
    SectorAnalysis,
    SectorContext,
    SectorHeadline,
    SectorMover,
    StockScorecard,
)
from app.stocks.ai.analysis.interfaces import (
    AiAnalysisCacheAdapter,
    EarningsAnalysisAdapter,
    FundamentalsAnalysisAdapter,
    MarketSummaryAdapter,
    RatingsAnalysisAdapter,
    SectorAnalysisAdapter,
    StockScorecardCacheAdapter,
    StockScorecardAdapter,
)
from app.stocks.company.earnings.annual.entities import AnnualEarningsTimeline
from app.stocks.company.earnings.annual.interfaces import AnnualEarningsAdapter
from app.stocks.company.earnings.quarterly.entities import QuarterlyEarningsTimeline
from app.stocks.company.earnings.quarterly.interfaces import QuarterlyEarningsAdapter
from app.stocks.entities import (
    AllTimeHigh,
    AnalystEstimates,
    KeyMetrics,
    Stock,
    StockPerformance,
    normalize_symbol,
)
from app.stocks.exceptions import StockDataUnavailable, StockNotFound
from app.stocks.market.boards.use_cases import GetMarketOverview, GetSectorPerformance
from app.stocks.company.news.entities import NewsArticle
from app.stocks.company.news.interfaces import NewsRepositoryAdapter
from app.stocks.interfaces import (
    AllTimeHighAdapter,
    AnalystEstimatesAdapter,
    BulkQuoteAdapter,
    StockDataAdapter,
    StockPerformanceAdapter,
)
from app.stocks.company.recommendations.entities import (
    AnalystRatingChanges,
    AnalystRecommendations,
)
from app.stocks.company.recommendations.interfaces import (
    RatingChangeAdapter,
    RecommendationAdapter,
)
from app.stocks.company.ticker.entities import PeHistoryStats
from app.stocks.company.ticker.use_cases import GetStockPeHistory
from app.stocks.catalog.universe.entities import (
    AnchorMetrics,
    IndustryValuation,
    SortDirection,
    StockSearchCriteria,
    StockSort,
)
from app.stocks.catalog.universe.interfaces import StockSearchRepositoryAdapter

logger = logging.getLogger(__name__)

# The cache key for the market-wide AI reads (sector, market summary), which take no
# symbol — a fixed sentinel so each gets one row in the shared, (kind, symbol)-keyed
# cache. Not a real ticker (underscored), so it can never collide with one.
_MARKET_CACHE_KEY = "_MARKET_"


def _analysis_is_fresh(generated_at: datetime | None, ttl: timedelta) -> bool:
    if generated_at is None:
        return False
    if generated_at.tzinfo is None:  # a naive stamp (e.g. from SQLite) is UTC
        generated_at = generated_at.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - generated_at <= ttl


def _consensus_pe(price: float | None, ttm_eps: float | None) -> float | None:
    if price is None or ttm_eps is None or price <= 0 or ttm_eps <= 0:
        return None
    return round(price / ttm_eps, 2)


def _price_multiple(price: float | None, per_share: float | None) -> float | None:
    if price is None or per_share is None or price <= 0 or per_share <= 0:
        return None
    return round(price / per_share, 2)


def _dividend_yield(dividend_per_share: float | None, price: float | None) -> float | None:
    if dividend_per_share is None or not price or price <= 0:
        return None
    return round(dividend_per_share / price * 100, 2)


def _ev_ebitda(
    price: float | None,
    ebitda: float | None,
    total_debt: float | None,
    cash: float | None,
    shares_outstanding: float | None,
) -> float | None:
    if (
        price is None
        or price <= 0
        or shares_outstanding is None
        or shares_outstanding <= 0
        or ebitda is None
        or ebitda <= 0
    ):
        return None
    enterprise_value = price * shares_outstanding + (total_debt or 0.0) - (cash or 0.0)
    return round(enterprise_value / ebitda, 2)


def _with_stored_fundamentals(
    stock: Stock, anchor: "AnchorMetrics", ttm_eps: float | None
) -> Stock:
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
        "ocf_per_share": anchor.ocf_per_share,
        "revenue_growth_yoy": anchor.revenue_growth_yoy,
        "eps_growth_yoy": anchor.eps_growth_yoy,
        "fcf_growth_yoy": anchor.fcf_growth_yoy,
        "eps": ttm_eps,
        "pe": _consensus_pe(price, ttm_eps),
        "pb": _price_multiple(price, anchor.book_value_per_share),
        "ps": _price_multiple(price, anchor.sales_per_share),
        "ev_to_ebitda": _ev_ebitda(
            price,
            anchor.ebitda,
            anchor.total_debt,
            anchor.cash_and_equivalents,
            anchor.shares_outstanding,
        ),
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
    return normalize_symbol(symbol)


class GetStockInfo:
    def __init__(
        self,
        provider: StockDataAdapter,
        performance_provider: StockPerformanceAdapter | None = None,
        all_time_high_provider: AllTimeHighAdapter | None = None,
        estimates_provider: AnalystEstimatesAdapter | None = None,
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
    def __init__(
        self,
        stock_info: GetStockInfo,
        analyzer: StockScorecardAdapter,
        quarterly_provider: QuarterlyEarningsAdapter | None = None,
        annual_provider: AnnualEarningsAdapter | None = None,
        recommendations_provider: RecommendationAdapter | None = None,
        industry_repository: StockSearchRepositoryAdapter | None = None,
        cache: StockScorecardCacheAdapter | None = None,
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
    def __init__(
        self,
        analyzer: EarningsAnalysisAdapter,
        quarterly_provider: QuarterlyEarningsAdapter | None = None,
        annual_provider: AnnualEarningsAdapter | None = None,
        cache: AiAnalysisCacheAdapter[EarningsAnalysis] | None = None,
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
    # How many credible covering firms to surface for the model — matches the card's top-firms.
    _TOP_FIRMS = 10

    def __init__(
        self,
        analyzer: RatingsAnalysisAdapter,
        recommendations_provider: RecommendationAdapter | None = None,
        rating_change_provider: RatingChangeAdapter | None = None,
        cache: AiAnalysisCacheAdapter[RatingsAnalysis] | None = None,
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
    def __init__(
        self,
        stock_info: GetStockInfo,
        analyzer: FundamentalsAnalysisAdapter,
        industry_repository: StockSearchRepositoryAdapter | None = None,
        quarterly_provider: QuarterlyEarningsAdapter | None = None,
        pe_history: GetStockPeHistory | None = None,
        cache: AiAnalysisCacheAdapter[FundamentalsAnalysis] | None = None,
        cache_ttl: timedelta = timedelta(minutes=30),
    ) -> None:
        self._stock_info = stock_info
        self._analyzer = analyzer
        self._industry_repository = industry_repository
        self._quarterly_provider = quarterly_provider
        self._pe_history = pe_history
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
        analysis = self._analyzer.analyze(
            stock,
            self._industry_valuation(normalized),
            self._pe_history_stats(normalized),
        )
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

    def _pe_history_stats(self, symbol: str) -> PeHistoryStats | None:
        # Best-effort context: where the current trailing P/E sits in the stock's own history
        # (percentile + cheap/fair/expensive signal), the "cheap for this stock?" anchor that
        # complements the peer benchmark. Unlike the rest of the analysis context this is not
        # DB-only — the P/E walk needs the deep reported-EPS run (Yahoo) and the daily closes
        # (Alpaca) — so it's wrapped best-effort: a blocked/failed read (or a series too short
        # to rank) just omits the signal. The result cache amortizes the live legs to once per
        # TTL, and a `None` here simply shortens the prompt (mirrors the industry benchmark).
        if self._pe_history is None:
            return None
        try:
            return self._pe_history.execute(symbol).stats
        except (StockNotFound, StockDataUnavailable):
            return None

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
    return (
        stock.metrics is not None
        or stock.analyst_estimates is not None
        or stock.dividend_yield is not None
        or stock.dividend_per_share is not None
        or stock.market_cap is not None
    )


# The sector board reads through the SPDR Select Sector ETFs, whose names follow the GICS
# vocabulary ("Health Care", "Financials", "Consumer Discretionary"); the screened universe
# stores Yahoo's own sector taxonomy, slugified ("healthcare", "financial_services",
# "consumer_cyclical"). This bridges the two so a board sector can be joined to its
# constituents. A board name absent here — or a slug with no screened members — simply
# yields no movers, since attribution is best-effort and degrades to the plain board.
_SECTOR_NAME_TO_SLUG: dict[str, str] = {
    "Technology": "technology",
    "Health Care": "healthcare",
    "Financials": "financial_services",
    "Consumer Discretionary": "consumer_cyclical",
    "Consumer Staples": "consumer_defensive",
    "Energy": "energy",
    "Industrials": "industrials",
    "Materials": "basic_materials",
    "Utilities": "utilities",
    "Real Estate": "real_estate",
    "Communication Services": "communication_services",
}


class GetSectorAnalysis:
    # The S&P 500 is the SPDR Select Sector ETFs' own universe (they *are* the GICS sectors
    # of that index), so its members grouped by sector are the faithful constituent set. The
    # ceiling sits above the index's ~500 names so the whole thing lands in one DB read.
    _MAX_CONSTITUENTS = 600
    # Top movers surfaced per sector (cap-weighted) and headlines attached per sector — kept
    # small so the prompt stays a focused "here's what drove it", not a constituent dump.
    _MOVERS_PER_SECTOR = 3
    _HEADLINES_PER_SECTOR = 2

    def __init__(
        self,
        sectors: GetSectorPerformance,
        analyzer: SectorAnalysisAdapter,
        cache: AiAnalysisCacheAdapter[SectorAnalysis] | None = None,
        cache_ttl: timedelta = timedelta(minutes=30),
        *,
        constituents: StockSearchRepositoryAdapter | None = None,
        quotes: BulkQuoteAdapter | None = None,
        news: NewsRepositoryAdapter | None = None,
    ) -> None:
        self._sectors = sectors
        self._analyzer = analyzer
        self._cache = cache
        self._cache_ttl = cache_ttl
        # Best-effort attribution legs. Any of them None (or failing at read time) leaves the
        # contexts with no movers — the analysis still runs on the plain board.
        self._constituents = constituents
        self._quotes = quotes
        self._news = news

    def execute(self) -> SectorAnalysis:
        # A fresh cached read short-circuits the whole board gather + model call — this
        # is market-wide, so one stored read serves every viewer within the TTL. Keyed
        # on the market sentinel, since the read takes no symbol.
        cached = self._fresh_cached()
        if cached is not None:
            return cached
        # Timed in two halves so the logs decompose the endpoint's latency into its
        # only two moving parts: the multi-source board+attribution gather (Alpaca + DB)
        # and the model call (Bedrock). This is the ground truth for "where do the
        # seconds go", rather than guessing which leg dominates.
        gather_start = time.perf_counter()
        board = self._sectors.execute()
        contexts = self._build_contexts(board)
        gather_ms = (time.perf_counter() - gather_start) * 1000

        # Log in a `finally` so a failing/slow model call (e.g. a 502 from an
        # unentitled model) still records the split — a line that only fires on
        # success would go missing in exactly the case we most want to diagnose.
        model_start = time.perf_counter()
        analysis: SectorAnalysis | None = None
        try:
            analysis = self._analyzer.analyze(contexts)
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

    def _build_contexts(self, board: list) -> list[SectorContext]:
        movers_by_slug = self._movers_by_slug()
        contexts = [
            SectorContext.from_constituents(
                sector=s.sector,
                symbol=s.symbol,
                change_percent=s.change_percent,
                performance=s.performance,
                constituents=tuple(movers_by_slug.get(_SECTOR_NAME_TO_SLUG.get(s.sector), ())),
                top_n=self._MOVERS_PER_SECTOR,
            )
            for s in board
        ]
        self._attach_headlines(contexts)
        self._log_attribution(contexts)
        return contexts

    def _log_attribution(self, contexts: list[SectorContext]) -> None:
        total_movers = sum(len(c.movers) for c in contexts)
        sectors_with_headlines = sum(1 for c in contexts if c.headlines)
        total_headlines = sum(len(c.headlines) for c in contexts)
        logger.info(
            "sector analysis attribution: sectors=%d total_movers=%d "
            "sectors_with_headlines=%d total_headlines=%d (quotes=%s news=%s)",
            len(contexts),
            total_movers,
            sectors_with_headlines,
            total_headlines,
            "on" if self._quotes is not None else "off",
            "on" if self._news is not None else "off",
        )

    def _movers_by_slug(self) -> dict[str, list[SectorMover]]:
        if self._constituents is None:
            return {}
        try:
            page = self._constituents.search(self._criteria())
        except Exception:  # best-effort context: a DB hiccup must never sink the analysis
            logger.warning(
                "sector analysis: constituent read failed; no attribution", exc_info=True
            )
            return {}
        rows = [
            r for r in page.results if r.sector and r.market_cap is not None
        ]
        changes = self._changes(tuple(r.ticker for r in rows))
        by_slug: dict[str, list[SectorMover]] = {}
        for r in rows:
            by_slug.setdefault(r.sector, []).append(
                SectorMover(
                    ticker=r.ticker,
                    name=r.name,
                    change_percent=changes.get(r.ticker),
                    market_cap=r.market_cap,
                )
            )
        return by_slug

    def _changes(self, tickers: tuple[str, ...]) -> dict[str, float | None]:
        if self._quotes is None or not tickers:
            return {}
        try:
            quotes = self._quotes.get_quotes(tickers)
        except StockDataUnavailable:
            logger.warning("sector analysis: live quotes unavailable; movers unranked")
            return {}
        return {symbol: quote.change_percent for symbol, quote in quotes.items()}

    def _attach_headlines(self, contexts: list[SectorContext]) -> None:
        if self._news is None:
            return
        tickers = {m.ticker for c in contexts for m in c.movers}
        if not tickers:
            return
        latest_by_ticker: dict[str, NewsArticle] = {}
        for ticker in tickers:
            try:
                stored = self._news.get(ticker)
            except Exception:  # a per-ticker read hiccup drops only that catalyst
                continue
            if stored is not None and not stored.is_empty:
                latest_by_ticker[ticker] = stored.articles[0]  # newest first
        for i, c in enumerate(contexts):
            headlines: list[SectorHeadline] = []
            for m in c.movers:
                art = latest_by_ticker.get(m.ticker)
                if art is not None:
                    headlines.append(
                        SectorHeadline(
                            ticker=m.ticker,
                            title=art.title,
                            published_at=art.published_at,
                            publisher=art.publisher,
                            link=art.link,
                        )
                    )
            if headlines:
                contexts[i] = replace(
                    c, headlines=tuple(headlines[: self._HEADLINES_PER_SECTOR])
                )

    def _criteria(self) -> StockSearchCriteria:
        return StockSearchCriteria(
            query=None,
            sectors=(),
            industries=(),
            in_sp500=True,
            in_nasdaq100=None,
            market_cap_tiers=(),
            sort=StockSort.MARKET_CAP,
            direction=SortDirection.DESC,
            limit=self._MAX_CONSTITUENTS,
            offset=0,
        )

    def _fresh_cached(self) -> SectorAnalysis | None:
        if self._cache is None:
            return None
        stored = self._cache.get(_MARKET_CACHE_KEY)
        if stored is None or not _analysis_is_fresh(stored.generated_at, self._cache_ttl):
            return None
        return stored


class GetMarketSummary:
    def __init__(
        self,
        overview: GetMarketOverview,
        analyzer: MarketSummaryAdapter,
        cache: AiAnalysisCacheAdapter[MarketSummary] | None = None,
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
