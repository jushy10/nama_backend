"""Application Business Rules: the stock use cases.

Orchestrate the flow: validate/normalize the symbol, then ask the injected
provider for the data. Depend only on the entity and the port — never on a
framework or a concrete provider.
"""

from dataclasses import replace
from datetime import date, datetime
from typing import TypeVar

from app.stocks.entities import (
    AllTimeHigh,
    AnalystEstimates,
    AnalystRecommendations,
    CandleSeries,
    CompanyProfile,
    Constituent,
    EarningsHistory,
    EarningsMetrics,
    EarningsSurprise,
    InvestmentAnalysis,
    Logo,
    MoversBoard,
    NextEarnings,
    Quote,
    ScreenedStock,
    SectorPerformance,
    Stock,
    StockFundamentals,
    StockIndex,
    StockPerformance,
    Timeframe,
)
from app.stocks.exceptions import StockDataUnavailable, StockNotFound
from app.stocks.indicators import RsiSeries, rsi_series
from app.stocks.ports import (
    AllTimeHighProvider,
    AnalystEstimatesProvider,
    CandleProvider,
    CompanyProfileProvider,
    ConstituentRepository,
    EarningsCalendarProvider,
    EarningsHistoryProvider,
    InvestmentAnalysisProvider,
    LogoProvider,
    QuoteBatchProvider,
    RecommendationProvider,
    RevenueHistoryProvider,
    SectorPerformanceProvider,
    SegmentRevenueProvider,
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

    The price snapshot is required; performance, fundamentals, the company
    description and the forward analyst estimates are optional, best-effort
    enrichment. If those sources fail or aren't configured, the stock is still
    returned with those fields left unset.
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
            description=profile.description if profile else None,
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
        # One call feeds two fields — the clean name and the description.
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


# The EPS feed (Finnhub) and SEC EDGAR date the same quarter differently: EDGAR
# records the true fiscal period end, while Finnhub snaps the quarter to a nearby
# calendar quarter-end. For an off-calendar filer those two dates can sit up to
# ~two months apart (NVDA's quarter ends ~Apr 26, Ciena's ~May 2, each labelled a
# calendar quarter away) — far enough that the snapped label can land closer to an
# *adjacent* fiscal quarter than to its own, so pairing by closest date misaligns.
#
# Instead we align the two series by order: both list the same consecutive quarters
# newest-first, so we walk them in lockstep and pair them off, using the dates only
# to notice when one side runs a quarter ahead of the other (e.g. the EPS is
# announced before the 10-Q is filed, so EDGAR hasn't got that quarter yet). Two
# dates this close are taken to be the same quarter — comfortably above the worst
# label gap (~65 days) yet well below the ~91-day spacing between quarters, so
# neighbouring quarters never collide.
_SAME_QUARTER_DAYS = 75

_T = TypeVar("_T")


def _align_to_quarters(
    quarters: tuple[EarningsSurprise, ...], by_period_end: dict[date, _T]
) -> dict[int, _T]:
    """Map each EPS quarter (by its index) to the EDGAR value for that quarter.

    Walks the EPS quarters and the EDGAR period ends together, newest-first, and
    pairs them up by fiscal period end. Used for both overlays that key off the
    same filings — the per-quarter revenue actuals and the segment/product
    breakdowns — so they align identically. A quarter newer than anything EDGAR
    has filed (or one with no period) is left unmatched, as is an EDGAR period
    belonging to a quarter newer than any in the EPS history.
    """
    dated = sorted(
        ((i, q.period) for i, q in enumerate(quarters) if q.period is not None),
        key=lambda pair: pair[1],
        reverse=True,
    )
    ends = sorted(by_period_end, reverse=True)
    matched: dict[int, _T] = {}
    i = j = 0
    while i < len(dated) and j < len(ends):
        index, period = dated[i]
        end = ends[j]
        gap = (period - end).days
        if gap > _SAME_QUARTER_DAYS:
            i += 1  # this EPS quarter is newer than any EDGAR period — skip it
        elif gap < -_SAME_QUARTER_DAYS:
            j += 1  # this EDGAR period is newer than the EPS quarter — skip it
        else:
            matched[index] = by_period_end[end]
            i += 1
            j += 1
    return matched


# Finnhub's free `/stock/earnings` returns only the last four quarters and
# ignores a larger `limit`, so the count is fixed here rather than a caller knob.
_EARNINGS_QUARTERS = 4


class GetStockEarnings:
    """Use case: retrieve a stock's recent quarterly earnings surprises.

    A dedicated dataset (actual vs estimate per quarter), not snapshot
    enrichment — so errors propagate to the caller rather than being swallowed,
    mirroring the candles and RSI endpoints. Several best-effort layers ride on
    top of the (primary) beat history, none of which can sink it: the trailing
    earnings ``metrics`` and the ``valuation`` ratios (both from the same
    fundamentals call), the ``next_report`` forecast (from the earnings
    calendar), per-quarter ``revenue_actual`` (reported revenue from SEC EDGAR),
    and per-quarter ``revenue_breakdown`` (that revenue split by segment and
    product/service, parsed from the filing itself). The two revenue layers are
    aligned onto each quarter by fiscal period end.
    """

    def __init__(
        self,
        provider: EarningsHistoryProvider,
        fundamentals_provider: StockFundamentalsProvider | None = None,
        calendar_provider: EarningsCalendarProvider | None = None,
        revenue_provider: RevenueHistoryProvider | None = None,
        segment_revenue_provider: SegmentRevenueProvider | None = None,
    ) -> None:
        self._provider = provider
        self._fundamentals_provider = fundamentals_provider
        self._calendar_provider = calendar_provider
        self._revenue_provider = revenue_provider
        self._segment_revenue_provider = segment_revenue_provider

    def execute(self, symbol: str) -> EarningsHistory:
        normalized = _normalize_symbol(symbol)
        history = self._provider.get_earnings_history(
            normalized, limit=_EARNINGS_QUARTERS
        )
        quarters = self._with_revenue(normalized, history.quarters)
        quarters = self._with_breakdown(normalized, quarters)
        fundamentals = self._fundamentals(normalized)
        # One fundamentals call feeds two blocks: the trailing earnings metrics
        # and the valuation/health/market ratios (the same KeyMetrics the stock
        # snapshot carries).
        metrics = EarningsMetrics.from_key_metrics(
            fundamentals.metrics if fundamentals else None
        )
        valuation = fundamentals.metrics if fundamentals else None
        next_report = self._next_report(normalized)
        if (
            quarters is history.quarters
            and metrics is None
            and valuation is None
            and next_report is None
        ):
            return history
        return replace(
            history,
            quarters=quarters,
            metrics=metrics,
            valuation=valuation,
            next_report=next_report,
        )

    def _with_revenue(
        self, symbol: str, quarters: tuple[EarningsSurprise, ...]
    ) -> tuple[EarningsSurprise, ...]:
        # Overlay reported revenue actuals (SEC EDGAR) onto the EPS quarters,
        # aligned quarter-by-quarter; best-effort. Returning the same tuple
        # identity signals "nothing merged" so execute can short-circuit.
        if self._revenue_provider is None:
            return quarters
        try:
            revenue = self._revenue_provider.get_quarterly_revenue(symbol)
        except (StockNotFound, StockDataUnavailable):
            return quarters
        if not revenue:
            return quarters
        matched = _align_to_quarters(quarters, revenue)
        if not matched:
            return quarters
        return tuple(
            replace(q, revenue_actual=matched[i]) if i in matched else q
            for i, q in enumerate(quarters)
        )

    def _with_breakdown(
        self, symbol: str, quarters: tuple[EarningsSurprise, ...]
    ) -> tuple[EarningsSurprise, ...]:
        # Overlay each quarter's segment/product revenue breakdown (parsed from
        # the SEC EDGAR filings), aligned by period end like the revenue actuals;
        # best-effort, and same identity-preserving short-circuit as _with_revenue.
        if self._segment_revenue_provider is None:
            return quarters
        try:
            breakdowns = self._segment_revenue_provider.get_quarterly_segment_revenue(
                symbol
            )
        except (StockNotFound, StockDataUnavailable):
            return quarters
        if not breakdowns:
            return quarters
        matched = _align_to_quarters(quarters, breakdowns)
        if not matched:
            return quarters
        return tuple(
            replace(q, revenue_breakdown=matched[i]) if i in matched else q
            for i, q in enumerate(quarters)
        )

    def _fundamentals(self, symbol: str) -> StockFundamentals | None:
        # The trailing earnings metrics and the valuation ratios both ride on the
        # same Finnhub fundamentals the stock snapshot uses; fetched once here and
        # best-effort, so a miss leaves the beat history intact rather than
        # failing the request.
        if self._fundamentals_provider is None:
            return None
        try:
            return self._fundamentals_provider.get_fundamentals(symbol)
        except (StockNotFound, StockDataUnavailable):
            return None

    def _next_report(self, symbol: str) -> NextEarnings | None:
        # The next scheduled report + consensus, from the earnings calendar;
        # best-effort, like the metrics block above.
        if self._calendar_provider is None:
            return None
        try:
            return self._calendar_provider.get_next_earnings(symbol)
        except (StockNotFound, StockDataUnavailable):
            return None


class GetStockAnalysis:
    """Use case: an AI-generated buy/hold/sell read on a single stock.

    Reuses ``GetStockInfo`` to assemble the enriched snapshot (price plus the
    best-effort performance/fundamentals/valuation enrichment), best-effort adds
    the recent earnings beat history as extra context, then asks the injected
    analyzer to weigh it all. The snapshot and the analysis are the primary data
    — a bad/unknown symbol or a model failure propagates — while the earnings
    context is best-effort, so a miss there leaves the analysis intact rather
    than failing it. The analyzer reasons only over what it's handed; it fetches
    nothing itself.
    """

    def __init__(
        self,
        stock_info: GetStockInfo,
        analyzer: InvestmentAnalysisProvider,
        earnings_provider: EarningsHistoryProvider | None = None,
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

    def _earnings(self, symbol: str) -> EarningsHistory | None:
        # Best-effort context: the beat history sharpens the analysis but isn't
        # required, so a missing provider or an upstream miss simply omits it.
        if self._earnings_provider is None:
            return None
        try:
            return self._earnings_provider.get_earnings_history(
                symbol, limit=_EARNINGS_QUARTERS
            )
        except (StockNotFound, StockDataUnavailable):
            return None


class GetStockRecommendations:
    """Use case: retrieve a stock's analyst recommendation trends.

    A dedicated dataset (the analyst buy/hold/sell split over recent months), not
    snapshot enrichment — so errors propagate to the caller rather than being
    swallowed, mirroring the earnings and RSI endpoints. The consensus read and
    its month-over-month trend are intrinsic to the entity; this use case just
    normalizes the symbol and delegates to the provider.
    """

    def __init__(self, provider: RecommendationProvider) -> None:
        self._provider = provider

    def execute(self, symbol: str) -> AnalystRecommendations:
        return self._provider.get_recommendations(_normalize_symbol(symbol))


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

        universe = self._filter(self._repository.all(), index, sector)
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

    @staticmethod
    def _filter(
        constituents: tuple[Constituent, ...],
        index: StockIndex | None,
        sector: str | None,
    ) -> list[Constituent]:
        """Narrow the universe by index membership and/or GICS sector.

        Sector matching is case-insensitive; ``None`` for either filter means
        "don't narrow on it".
        """
        sector_key = sector.strip().casefold() if sector else None
        return [
            c
            for c in constituents
            if (index is None or c.in_index(index))
            and (sector_key is None or (c.sector or "").casefold() == sector_key)
        ]
