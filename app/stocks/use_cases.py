"""Application Business Rules: the stock use cases.

Orchestrate the flow: validate/normalize the symbol, then ask the injected
provider for the data. Depend only on the entity and the port — never on a
framework or a concrete provider.
"""

import logging
import time
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from app.stocks.earnings.annual.entities import AnnualEarningsTimeline
from app.stocks.earnings.annual.ports import AnnualEarningsProvider
from app.stocks.earnings.quarterly.entities import QuarterlyEarningsTimeline
from app.stocks.earnings.quarterly.ports import QuarterlyEarningsProvider
from app.stocks.entities import (
    AllTimeHigh,
    AnalystEstimates,
    CandleSeries,
    CompanyProfile,
    EarningsAnalysis,
    InvestmentAnalysis,
    Logo,
    MarketIndexPerformance,
    MarketSummary,
    RatingsAnalysis,
    SectorAnalysis,
    SectorPerformance,
    Stock,
    StockFundamentals,
    StockPerformance,
    Timeframe,
)
from app.stocks.exceptions import StockDataUnavailable, StockNotFound
from app.stocks.indicators import (
    EmaSeries,
    SupportLevelSeries,
    ema_series,
    support_levels,
)
from app.stocks.ports import (
    AllTimeHighProvider,
    AnalystEstimatesProvider,
    CandleProvider,
    CompanyProfileProvider,
    EarningsAnalysisProvider,
    InvestmentAnalysisCache,
    InvestmentAnalysisProvider,
    LogoProvider,
    MarketOverviewProvider,
    MarketSummaryProvider,
    RatingsAnalysisProvider,
    SectorAnalysisProvider,
    SectorPerformanceProvider,
    StockDataProvider,
    StockFundamentalsProvider,
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
from app.stocks.universe.entities import IndustryValuation
from app.stocks.universe.repository import StockSearchRepository

logger = logging.getLogger(__name__)


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

    The price snapshot is required; performance, fundamentals, the clean company
    name and the forward analyst estimates are optional, best-effort enrichment.
    If those sources fail or aren't configured, the stock is still returned with
    those fields left unset.
    """

    def __init__(
        self,
        provider: StockDataProvider,
        performance_provider: StockPerformanceProvider | None = None,
        fundamentals_provider: StockFundamentalsProvider | None = None,
        profile_provider: CompanyProfileProvider | None = None,
        all_time_high_provider: AllTimeHighProvider | None = None,
        estimates_provider: AnalystEstimatesProvider | None = None,
    ) -> None:
        self._provider = provider
        self._performance_provider = performance_provider
        self._fundamentals_provider = fundamentals_provider
        self._profile_provider = profile_provider
        self._all_time_high_provider = all_time_high_provider
        self._estimates_provider = estimates_provider

    def execute(self, symbol: str) -> Stock:
        normalized = _normalize_symbol(symbol)
        stock = self._provider.get_stock(normalized)  # required; errors propagate
        # The four enrichment sources below are independent network reads (Alpaca /
        # Finnhub), with no ordering between them, so they run concurrently rather
        # than in series: the gather latency the AI-analysis path pays before the
        # model call collapses from their sum to their slowest. Each is already
        # best-effort (returns None on its own failure), and the vendor SDKs' HTTP
        # clients are safe to call concurrently for independent reads. Estimates is
        # deliberately kept off the pool — it reads the shared request DB session,
        # which must not be touched from a worker thread, and it's a fast local read
        # rather than a network round-trip.
        with ThreadPoolExecutor(max_workers=4) as pool:
            fundamentals_future = pool.submit(self._fundamentals, normalized)
            profile_future = pool.submit(self._profile, normalized)
            performance_future = pool.submit(self._performance, normalized)
            all_time_high_future = pool.submit(self._all_time_high, normalized, stock)
            fundamentals = fundamentals_future.result()
            profile = profile_future.result()
            performance = performance_future.result()
            all_time_high = all_time_high_future.result()
        return replace(
            stock,
            # Prefer the profile vendor's clean display name ("Apple Inc.") over
            # the price feed's full legal title ("Apple Inc. Common Stock"); fall
            # back to the feed's name when the profile is missing or unconfigured.
            name=profile.name if profile and profile.name else stock.name,
            performance=performance,
            market_cap=fundamentals.market_cap if fundamentals else None,
            dividend_per_share=(
                fundamentals.dividend_per_share if fundamentals else None
            ),
            dividend_yield=fundamentals.dividend_yield if fundamentals else None,
            metrics=fundamentals.metrics if fundamentals else None,
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

    def _fundamentals(self, symbol: str) -> StockFundamentals | None:
        if self._fundamentals_provider is None:
            return None
        try:
            return self._fundamentals_provider.get_fundamentals(symbol)
        except (StockNotFound, StockDataUnavailable):
            return None  # best-effort

    def _profile(self, symbol: str) -> CompanyProfile | None:
        # Supplies the clean display name that overrides the price feed's title.
        if self._profile_provider is None:
            return None
        try:
            return self._profile_provider.get_profile(symbol)
        except (StockNotFound, StockDataUnavailable):
            return None  # best-effort: never sink the price response

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


class GetStockLogo:
    """Use case: retrieve the company logo image for a stock symbol."""

    def __init__(self, provider: LogoProvider) -> None:
        self._provider = provider

    def execute(self, symbol: str) -> Logo:
        return self._provider.get_logo(_normalize_symbol(symbol))


class GetStockCandles:
    """Use case: retrieve historical OHLC candles for charting."""

    def __init__(self, provider: CandleProvider) -> None:
        self._provider = provider

    def execute(
        self,
        symbol: str,
        timeframe: Timeframe,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> CandleSeries:
        if start is not None and end is not None and start >= end:
            raise ValueError("'start' must be earlier than 'end'.")
        return self._provider.get_candles(
            _normalize_symbol(symbol), timeframe, start=start, end=end
        )


# Approximate wall-clock span of one bar at each granularity. Used only to reach
# far enough *before* the visible window to warm an EMA up (see GetStockEma). A
# daily bar spans more than a calendar day once weekends/holidays are counted, so
# the warmup applies a generous multiple rather than these raw spans.
_BAR_SPAN: dict[Timeframe, timedelta] = {
    Timeframe.MIN_1: timedelta(minutes=1),
    Timeframe.MIN_5: timedelta(minutes=5),
    Timeframe.MIN_15: timedelta(minutes=15),
    Timeframe.MIN_30: timedelta(minutes=30),
    Timeframe.HOUR_1: timedelta(hours=1),
    Timeframe.HOUR_4: timedelta(hours=4),
    Timeframe.DAY_1: timedelta(days=1),
    Timeframe.WEEK_1: timedelta(weeks=1),
    Timeframe.MONTH_1: timedelta(days=31),
}

# Reach back this many bar-spans per period of warmup. 3× comfortably covers the
# weekend/holiday gaps that stretch a daily bar past one calendar day, so a
# `period`-bar EMA is fully warm by the visible window's start.
_EMA_WARMUP_FACTOR = 3


def _ema_warmup_span(timeframe: Timeframe, max_period: int) -> timedelta:
    """How far before the visible window to start fetching so an EMA of
    ``max_period`` is already warm by that window's first bar."""
    return _BAR_SPAN.get(timeframe, timedelta(days=1)) * max_period * _EMA_WARMUP_FACTOR


class GetStockEma:
    """Use case: compute EMA overlay line(s) for a symbol from its price history.

    Reuses the CandleProvider port — EMA is derived from the same OHLC bars the
    chart endpoint uses, so no extra data source is needed. The indicator math is
    pure domain logic (``ema_series``); this use case only fetches the window and
    delegates. One or more periods can be requested in a single call (e.g. the
    9/21/50 overlay), each returned as its own line.

    **Warmup.** An EMA's first value only lands ``period - 1`` bars in, so fetching
    exactly the visible ``[start, end]`` would leave the chart's left edge bare
    (and a deep period blank). So the fetch reaches an extra ``max(period)`` bars
    *before* ``start``, computes over the longer series, then trims the result back
    to the visible window — every on-screen candle then carries a value. A ``start``
    of ``None`` (MAX) already pulls all available history, so there's nothing
    earlier to warm from and nothing to trim.
    """

    def __init__(self, provider: CandleProvider) -> None:
        self._provider = provider

    def execute(
        self,
        symbol: str,
        timeframe: Timeframe,
        *,
        periods: Sequence[int],
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> EmaSeries:
        if start is not None and end is not None and start >= end:
            raise ValueError("'start' must be earlier than 'end'.")
        # Extend the fetch back by a warmup so the EMA is already warm at `start`.
        fetch_start = start
        if start is not None and periods:
            fetch_start = start - _ema_warmup_span(timeframe, max(periods))
        series = self._provider.get_candles(
            _normalize_symbol(symbol), timeframe, start=fetch_start, end=end
        )
        ema = ema_series(series, periods)
        if start is None:
            return ema
        # Trim the warmup bars back off, leaving only the visible window.
        return replace(
            ema,
            lines=tuple(
                replace(
                    line,
                    points=tuple(p for p in line.points if p.timestamp >= start),
                )
                for line in ema.lines
            ),
        )


class GetStockSupportLevels:
    """Use case: detect horizontal support levels for a symbol from its price
    history.

    Reuses the CandleProvider port — support is read from the same OHLC bars the
    chart endpoint uses, so no extra data source is needed. The detection math is
    pure domain logic (``support_levels``); this use case only fetches the window
    and delegates. Too little history (or no swing low below the current price)
    yields an empty series rather than an error: the symbol exists, there just
    isn't a level to draw.
    """

    def __init__(self, provider: CandleProvider) -> None:
        self._provider = provider

    def execute(
        self,
        symbol: str,
        timeframe: Timeframe,
        *,
        window: int = 5,
        tolerance: float = 0.02,
        max_levels: int = 5,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> SupportLevelSeries:
        if start is not None and end is not None and start >= end:
            raise ValueError("'start' must be earlier than 'end'.")
        series = self._provider.get_candles(
            _normalize_symbol(symbol), timeframe, start=start, end=end
        )
        return support_levels(
            series, window=window, tolerance=tolerance, max_levels=max_levels
        )


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
        analyzer: InvestmentAnalysisProvider,
        quarterly_provider: QuarterlyEarningsProvider | None = None,
        annual_provider: AnnualEarningsProvider | None = None,
        recommendations_provider: RecommendationProvider | None = None,
        industry_repository: StockSearchRepository | None = None,
        cache: InvestmentAnalysisCache | None = None,
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

    def execute(self, symbol: str) -> InvestmentAnalysis:
        normalized = _normalize_symbol(symbol)
        # A fresh cached read short-circuits the whole gather + model call — the
        # analysis only drifts as the figures do, so a repeat view within the TTL
        # (and any burst of viewers) is served straight from the store.
        cached = self._fresh_cached(normalized)
        if cached is not None:
            return cached
        # The enriched snapshot is primary: a bad symbol (ValueError), an unknown
        # one (StockNotFound), or an upstream failure (StockDataUnavailable) all
        # propagate rather than yielding an analysis of nothing. Everything else is
        # best-effort context assembled below.
        stock = self._stock_info.execute(normalized)
        analysis = self._analyzer.analyze(
            stock,
            self._quarterly(normalized),
            self._annual(normalized),
            self._recommendations(normalized),
            self._industry_valuation(normalized),
        )
        # Store for the next viewer — but only a *complete* read (both strengths
        # and risks present). Refusing to cache an incomplete analysis means a rare
        # empty-list model result is never frozen for the TTL: the next view
        # regenerates instead of serving empty bullets. Best-effort by contract (a
        # write failure is swallowed in the adapter), so it never sinks the analysis.
        if self._cache is not None and analysis.is_complete:
            self._cache.put(analysis)
        return analysis

    def _fresh_cached(self, symbol: str) -> InvestmentAnalysis | None:
        # A stored read is a hit only while it's within the TTL; past that it's
        # stale and we regenerate (overwriting it). A cache-read failure degrades to
        # a miss in the adapter, so this simply returns None and we regenerate.
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
    itself. Unlike the per-stock buy/hold/sell analysis this has no DB result cache
    — the endpoint leans on a short HTTP ``Cache-Control`` instead, matching the
    market read.
    """

    def __init__(
        self,
        analyzer: EarningsAnalysisProvider,
        quarterly_provider: QuarterlyEarningsProvider | None = None,
        annual_provider: AnnualEarningsProvider | None = None,
    ) -> None:
        self._analyzer = analyzer
        self._quarterly_provider = quarterly_provider
        self._annual_provider = annual_provider

    def execute(self, symbol: str) -> EarningsAnalysis:
        normalized = _normalize_symbol(symbol)
        quarterly = self._quarterly(normalized)
        annual = self._annual(normalized)
        # Nothing on file for either timeline — an uncovered/unknown symbol. Fail
        # rather than ask the model to reason over an empty slate.
        if quarterly is None and annual is None:
            raise StockDataUnavailable(normalized, "no earnings data to analyse")
        return self._analyzer.analyze(normalized, quarterly, annual)

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
    reasons only over what it's handed; it fetches nothing itself. Like the earnings read, no
    DB result cache — the endpoint leans on a short HTTP ``Cache-Control`` instead.
    """

    # How many credible covering firms to surface for the model — matches the card's top-firms.
    _TOP_FIRMS = 10

    def __init__(
        self,
        analyzer: RatingsAnalysisProvider,
        recommendations_provider: RecommendationProvider | None = None,
        rating_change_provider: RatingChangeProvider | None = None,
        *,
        now: datetime | None = None,
    ) -> None:
        self._analyzer = analyzer
        self._recommendations_provider = recommendations_provider
        self._rating_change_provider = rating_change_provider
        self._now = now  # injectable clock for tests; None → real now per call

    def execute(self, symbol: str) -> RatingsAnalysis:
        normalized = _normalize_symbol(symbol)
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
        return self._analyzer.analyze(normalized, recommendations, top_firms)

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


class GetSectorPerformance:
    """Use case: rank the market's sectors by their move on the day.

    Takes no input — it reports on the whole market. Sectors come back best
    performer first; any sector missing a quote (so no percent move) sorts last.
    """

    def __init__(self, provider: SectorPerformanceProvider) -> None:
        self._provider = provider

    def execute(self) -> list[SectorPerformance]:
        sectors = self._provider.get_sector_performance()
        # Best performer first; a None percent (no quote) sorts to the end.
        return sorted(
            sectors,
            key=lambda s: (s.change_percent is None, -(s.change_percent or 0.0)),
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
        self, sectors: GetSectorPerformance, analyzer: SectorAnalysisProvider
    ) -> None:
        self._sectors = sectors
        self._analyzer = analyzer

    def execute(self) -> SectorAnalysis:
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


class GetMarketOverview:
    """Use case: the headline US indices' performance (the S&P 500 and Nasdaq).

    Takes no input — it reports on the whole market. Returns the indices in the
    provider's stable order (broad market first), each carrying its day move and
    trailing-window returns.
    """

    def __init__(self, provider: MarketOverviewProvider) -> None:
        self._provider = provider

    def execute(self) -> list[MarketIndexPerformance]:
        return self._provider.get_market_overview()


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
        self, overview: GetMarketOverview, analyzer: MarketSummaryProvider
    ) -> None:
        self._overview = overview
        self._analyzer = analyzer

    def execute(self) -> MarketSummary:
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
