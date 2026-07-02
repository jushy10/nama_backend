"""Application Business Rules: the stock use cases.

Orchestrate the flow: validate/normalize the symbol, then ask the injected
provider for the data. Depend only on the entity and the port — never on a
framework or a concrete provider.
"""

from dataclasses import replace
from datetime import datetime

from app.stocks.entities import (
    AllTimeHigh,
    AnalystEstimates,
    CandleSeries,
    CompanyProfile,
    Constituent,
    GrowthScreenBoard,
    GrowthScreenedStock,
    GrowthSortKey,
    InvestmentAnalysis,
    Logo,
    MoversBoard,
    Quote,
    ScreenedStock,
    SectorPerformance,
    Stock,
    StockFundamentals,
    StockIndex,
    StockPerformance,
    Timeframe,
)
from app.stocks.earnings.quarterly.entities import QuarterlyEarningsTimeline
from app.stocks.earnings.quarterly.ports import QuarterlyEarningsProvider
from app.stocks.exceptions import StockDataUnavailable, StockNotFound
from app.stocks.indicators import RsiSeries, rsi_series
from app.stocks.ports import (
    AllTimeHighProvider,
    AnalystEstimatesProvider,
    CandleProvider,
    CompanyProfileProvider,
    ConstituentRepository,
    ForwardGrowthProvider,
    InvestmentAnalysisProvider,
    LogoProvider,
    QuoteBatchProvider,
    SectorPerformanceProvider,
    StockDataProvider,
    StockFundamentalsProvider,
    StockPerformanceProvider,
    StockQuoteProvider,
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
        fundamentals = self._fundamentals(normalized)
        profile = self._profile(normalized)
        return replace(
            stock,
            # Prefer the profile vendor's clean display name ("Apple Inc.") over
            # the price feed's full legal title ("Apple Inc. Common Stock"); fall
            # back to the feed's name when the profile is missing or unconfigured.
            name=profile.name if profile and profile.name else stock.name,
            performance=self._performance(normalized),
            market_cap=fundamentals.market_cap if fundamentals else None,
            dividend_per_share=(
                fundamentals.dividend_per_share if fundamentals else None
            ),
            dividend_yield=fundamentals.dividend_yield if fundamentals else None,
            metrics=fundamentals.metrics if fundamentals else None,
            analyst_estimates=self._estimates(normalized),
            all_time_high=self._all_time_high(normalized, stock),
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


class GetStockAnalysis:
    """Use case: an AI-generated buy/hold/sell read on a single stock.

    Reuses ``GetStockInfo`` to assemble the enriched snapshot (price plus the
    best-effort performance/fundamentals/valuation enrichment), best-effort adds
    the recent quarterly earnings timeline as extra context, then asks the
    injected analyzer to weigh it all. The snapshot and the analysis are the
    primary data — a bad/unknown symbol or a model failure propagates — while the
    earnings context is best-effort, so a miss there leaves the analysis intact
    rather than failing it. The analyzer reasons only over what it's handed; it
    fetches nothing itself.
    """

    def __init__(
        self,
        stock_info: GetStockInfo,
        analyzer: InvestmentAnalysisProvider,
        earnings_provider: QuarterlyEarningsProvider | None = None,
    ) -> None:
        self._stock_info = stock_info
        self._analyzer = analyzer
        self._earnings_provider = earnings_provider

    def execute(self, symbol: str) -> InvestmentAnalysis:
        normalized = _normalize_symbol(symbol)
        # The enriched snapshot is primary: a bad symbol (ValueError), an unknown
        # one (StockNotFound), or an upstream failure (StockDataUnavailable) all
        # propagate rather than yielding an analysis of nothing.
        stock = self._stock_info.execute(normalized)
        earnings = self._earnings(normalized)
        return self._analyzer.analyze(stock, earnings)

    def _earnings(self, symbol: str) -> QuarterlyEarningsTimeline | None:
        # Best-effort context: the beat history sharpens the analysis but isn't
        # required, so a missing provider, an upstream miss, or an uncovered
        # symbol (empty timeline) simply omits it.
        if self._earnings_provider is None:
            return None
        try:
            timeline = self._earnings_provider.get_quarterly_earnings(symbol)
        except (StockNotFound, StockDataUnavailable):
            return None
        return None if timeline.is_empty else timeline


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


class ScreenStocks:
    """Use case: rank a universe of stocks by their move on the day.

    Builds a "movers" board — the biggest gainers and the biggest losers — over
    the constituents of an index (or the whole known universe), optionally
    narrowed to one GICS sector. The day's move comes from a best-effort batch
    of live quotes; constituents without a usable quote are simply left out of
    the ranking, and a symbol is never both a gainer and a loser.
    """

    def __init__(
        self, repository: ConstituentRepository, quotes: QuoteBatchProvider
    ) -> None:
        self._repository = repository
        self._quotes = quotes

    def execute(
        self,
        *,
        index: StockIndex | None = None,
        sector: str | None = None,
        limit: int = 10,
    ) -> MoversBoard:
        if limit < 1:
            raise ValueError("'limit' must be at least 1.")

        universe = _filter_universe(self._repository.all(), index, sector)
        symbols = [c.symbol for c in universe]
        quotes = self._quotes.get_quotes(symbols) if symbols else {}
        # A non-empty universe that returns no quotes at all means the upstream
        # feed is down — surface that rather than serving an empty board that
        # would read as a flat market. (An empty *universe* is a valid "nothing
        # matched the filter" and returns an empty board below.)
        if symbols and not quotes:
            raise StockDataUnavailable(
                "screener", "no quotes for the screened universe"
            )

        screened = [
            ScreenedStock(name=c.name, sector=c.sector, quote=quotes[c.symbol])
            for c in universe
            if c.symbol in quotes and quotes[c.symbol].change_percent is not None
        ]
        # None percents are already filtered out, so the key is always a float.
        ranked = sorted(screened, key=lambda s: s.change_percent, reverse=True)
        # Best-first gainers and worst-first losers, each capped at `limit` and
        # never overlapping. When the universe is too small to fill both sides
        # it's split down the middle, so a name shows once — as a gainer or a
        # loser, not both. For a real index (hundreds of names) this is just the
        # top `limit` and bottom `limit`.
        count = len(ranked)
        gain_count = min(limit, (count + 1) // 2)
        lose_count = min(limit, count - gain_count)
        gainers = tuple(ranked[:gain_count])
        losers = tuple(reversed(ranked[-lose_count:])) if lose_count else ()

        as_of = max(
            (s.quote.as_of for s in screened if s.quote.as_of is not None),
            default=None,
        )
        return MoversBoard(
            index=index,
            sector=sector,
            limit=limit,
            universe_count=len(universe),
            quoted_count=len(screened),
            as_of=as_of,
            gainers=gainers,
            losers=losers,
        )


def _filter_universe(
    constituents: tuple[Constituent, ...],
    index: StockIndex | None,
    sector: str | None,
) -> list[Constituent]:
    """Narrow a screener universe by index membership and/or GICS sector.

    Shared by both screeners so "the universe" means the same thing whichever
    line a caller ranks on. Sector matching is case-insensitive; ``None`` for
    either filter means "don't narrow on it".
    """
    sector_key = sector.strip().casefold() if sector else None
    return [
        c
        for c in constituents
        if (index is None or c.in_index(index))
        and (sector_key is None or (c.sector or "").casefold() == sector_key)
    ]


class ScreenGrowthStocks:
    """Use case: rank a universe of stocks by their expected next-fiscal-year growth.

    The forward-looking screener — where ``ScreenStocks`` ranks the day's price
    move, this ranks the analyst consensus for the upcoming fiscal year (FY1
    estimate versus the latest reported actual, as stored by the annual-earnings
    slice) over the constituents of an index, optionally narrowed to one GICS
    sector. Callers pick the line to rank on (EPS or revenue) and may set minimum
    growth thresholds on either; a stock missing a thresholded figure — or the
    sort figure — can't demonstrate it qualifies, so it's simply left out.
    Coverage is whatever the annual-earnings cache holds; the board reports it so
    an unseeded universe reads as "no data yet", not "no growth anywhere".
    """

    def __init__(
        self, repository: ConstituentRepository, growth: ForwardGrowthProvider
    ) -> None:
        self._repository = repository
        self._growth = growth

    def execute(
        self,
        *,
        index: StockIndex | None = None,
        sector: str | None = None,
        sort: GrowthSortKey = GrowthSortKey.EPS,
        min_revenue_growth: float | None = None,
        min_eps_growth: float | None = None,
        limit: int = 20,
    ) -> GrowthScreenBoard:
        if limit < 1:
            raise ValueError("'limit' must be at least 1.")

        universe = _filter_universe(self._repository.all(), index, sector)
        symbols = [c.symbol for c in universe]
        growth = self._growth.get_forward_growth(symbols) if symbols else {}

        screened = [
            GrowthScreenedStock(name=c.name, sector=c.sector, growth=growth[c.symbol])
            for c in universe
            if c.symbol in growth
        ]
        qualified = [
            s
            for s in screened
            if self._sort_key(s, sort) is not None
            and _passes(s.expected_revenue_growth, min_revenue_growth)
            and _passes(s.expected_eps_growth, min_eps_growth)
        ]
        # The sort figure is never None past the filter above.
        ranked = sorted(qualified, key=lambda s: self._sort_key(s, sort), reverse=True)

        return GrowthScreenBoard(
            index=index,
            sector=sector,
            sort=sort,
            min_revenue_growth=min_revenue_growth,
            min_eps_growth=min_eps_growth,
            limit=limit,
            universe_count=len(universe),
            covered_count=len(screened),
            stocks=tuple(ranked[:limit]),
        )

    @staticmethod
    def _sort_key(stock: GrowthScreenedStock, sort: GrowthSortKey) -> float | None:
        if sort is GrowthSortKey.REVENUE:
            return stock.expected_revenue_growth
        return stock.expected_eps_growth


def _passes(value: float | None, minimum: float | None) -> bool:
    """Whether a growth figure clears a minimum threshold. No threshold passes
    everything; a thresholded-but-missing figure fails — the stock can't
    demonstrate it qualifies."""
    if minimum is None:
        return True
    return value is not None and value >= minimum
