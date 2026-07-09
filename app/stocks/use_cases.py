"""Application Business Rules: the stock use cases.

Orchestrate the flow: validate/normalize the symbol, then ask the injected
provider for the data. Depend only on the entity and the port — never on a
framework or a concrete provider.
"""

import logging
import time
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
    InvestmentAnalysis,
    Logo,
    Quote,
    SectorAnalysis,
    SectorPerformance,
    Stock,
    StockFundamentals,
    StockPerformance,
    Timeframe,
)
from app.stocks.exceptions import StockDataUnavailable, StockNotFound
from app.stocks.indicators import (
    RsiSeries,
    SupportLevelSeries,
    rsi_series,
    support_levels,
)
from app.stocks.ports import (
    AllTimeHighProvider,
    AnalystEstimatesProvider,
    CandleProvider,
    CompanyProfileProvider,
    InvestmentAnalysisCache,
    InvestmentAnalysisProvider,
    LogoProvider,
    SectorAnalysisProvider,
    SectorPerformanceProvider,
    StockDataProvider,
    StockFundamentalsProvider,
    StockPerformanceProvider,
    StockQuoteProvider,
)
from app.stocks.recommendations.entities import AnalystRecommendations
from app.stocks.recommendations.ports import RecommendationProvider
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


class GetStockQuote:
    """Use case: retrieve a stock's minimal live quote by its symbol.

    Backs the high-frequency polling endpoint — only the snapshot-derived price
    and day change, no best-effort enrichment. The quote is the primary data, so
    a not-found / upstream failure propagates rather than being swallowed,
    mirroring the candles and earnings endpoints.
    """

    def __init__(self, provider: StockQuoteProvider) -> None:
        self._provider = provider

    def execute(self, symbol: str) -> Quote:
        return self._provider.get_quote(_normalize_symbol(symbol))


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


class GetStockRsi:
    """Use case: compute the RSI indicator for a symbol from its price history.

    Reuses the CandleProvider port — RSI is derived from the same OHLC bars the
    chart endpoint uses, so no extra data source is needed. The indicator math
    is pure domain logic (``rsi_series``); this use case only fetches the window
    and delegates. Too little history in the window yields an empty series
    rather than an error: the symbol exists, the indicator just can't warm up.
    """

    def __init__(self, provider: CandleProvider) -> None:
        self._provider = provider

    def execute(
        self,
        symbol: str,
        timeframe: Timeframe,
        *,
        period: int = 14,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> RsiSeries:
        if start is not None and end is not None and start >= end:
            raise ValueError("'start' must be earlier than 'end'.")
        series = self._provider.get_candles(
            _normalize_symbol(symbol), timeframe, start=start, end=end
        )
        return rsi_series(series, period)


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
        # Store for the next viewer. Best-effort by contract (a write failure is
        # swallowed in the adapter), so it never sinks the freshly-made analysis.
        if self._cache is not None:
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
        # Two DB reads on the shared anchor — resolve the ticker's industry, then
        # summarize its screened peers' P/Es into the benchmark entity. An
        # unconfigured repository, an unscreened/unclassified symbol (no industry),
        # or a benchmark too thin to stand for its industry (fewer than
        # MIN_REPRESENTATIVE_PEERS valued peers — a "median" of one or two stocks
        # is noise, not an anchor) all omit it rather than handing the model a
        # figure it would over-trust.
        if self._industry_repository is None:
            return None
        try:
            industry = self._industry_repository.industry_for_ticker(symbol)
            if not industry:
                return None
            pe_ratios = self._industry_repository.pe_ratios_for_industry(industry)
        except (StockNotFound, StockDataUnavailable):
            return None
        valuation = IndustryValuation.from_pe_ratios(industry, pe_ratios)
        return valuation if valuation.is_representative else None


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
