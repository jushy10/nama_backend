"""Application Business Rules: the stock use cases.

Orchestrate the flow: validate/normalize the symbol, then ask the injected
provider for the data. Depend only on the entity and the port — never on a
framework or a concrete provider.
"""

from dataclasses import replace
from datetime import datetime

from app.stocks.entities import (
    CandleSeries,
    Constituent,
    EarningsHistory,
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
from app.stocks.exceptions import StockDataUnavailable, StockNotFound
from app.stocks.indicators import RsiSeries, rsi_series
from app.stocks.ports import (
    CandleProvider,
    CompanyProfileProvider,
    ConstituentRepository,
    EarningsHistoryProvider,
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

    The price snapshot is required; performance, fundamentals and the company
    description are optional, best-effort enrichment. If those sources fail or
    aren't configured, the stock is still returned with those fields left unset.
    """

    def __init__(
        self,
        provider: StockDataProvider,
        performance_provider: StockPerformanceProvider | None = None,
        fundamentals_provider: StockFundamentalsProvider | None = None,
        profile_provider: CompanyProfileProvider | None = None,
    ) -> None:
        self._provider = provider
        self._performance_provider = performance_provider
        self._fundamentals_provider = fundamentals_provider
        self._profile_provider = profile_provider

    def execute(self, symbol: str) -> Stock:
        normalized = _normalize_symbol(symbol)
        stock = self._provider.get_stock(normalized)  # required; errors propagate
        fundamentals = self._fundamentals(normalized)
        return replace(
            stock,
            performance=self._performance(normalized),
            description=self._description(normalized),
            market_cap=fundamentals.market_cap if fundamentals else None,
            dividend_per_share=(
                fundamentals.dividend_per_share if fundamentals else None
            ),
            dividend_yield=fundamentals.dividend_yield if fundamentals else None,
            metrics=fundamentals.metrics if fundamentals else None,
        )

    def _performance(self, symbol: str) -> StockPerformance | None:
        if self._performance_provider is None:
            return None
        try:
            return self._performance_provider.get_performance(symbol)
        except (StockNotFound, StockDataUnavailable):
            return None  # best-effort: never sink the price response

    def _fundamentals(self, symbol: str) -> StockFundamentals | None:
        if self._fundamentals_provider is None:
            return None
        try:
            return self._fundamentals_provider.get_fundamentals(symbol)
        except (StockNotFound, StockDataUnavailable):
            return None  # best-effort

    def _description(self, symbol: str) -> str | None:
        if self._profile_provider is None:
            return None
        try:
            return self._profile_provider.get_profile(symbol).description
        except (StockNotFound, StockDataUnavailable):
            return None  # best-effort: never sink the price response


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


class GetStockEarnings:
    """Use case: retrieve a stock's recent quarterly earnings surprises.

    A dedicated dataset (actual vs estimate per quarter), not snapshot
    enrichment — so errors propagate to the caller rather than being swallowed,
    mirroring the candles and RSI endpoints.
    """

    def __init__(self, provider: EarningsHistoryProvider) -> None:
        self._provider = provider

    def execute(self, symbol: str, *, limit: int = 4) -> EarningsHistory:
        if limit < 1:
            raise ValueError("'limit' must be at least 1.")
        return self._provider.get_earnings_history(
            _normalize_symbol(symbol), limit=limit
        )


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
