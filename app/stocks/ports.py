"""Application port: the abstraction the use case depends on.

This is the Dependency Inversion that makes the slice clean: the use case
depends on this interface, and the adapter layer provides the Alpaca-backed
implementation. The core never imports Alpaca; Alpaca imports the core.
"""

from abc import ABC, abstractmethod
from datetime import date, datetime

from app.stocks.earnings.annual.entities import AnnualEarningsTimeline
from app.stocks.earnings.quarterly.entities import QuarterlyEarningsTimeline
from app.stocks.entities import (
    AllTimeHigh,
    AnalystEstimates,
    CandleSeries,
    CompanyProfile,
    InvestmentAnalysis,
    Logo,
    Quote,
    SectorPerformance,
    Stock,
    StockFundamentals,
    StockPerformance,
    Timeframe,
)
from app.stocks.ticker.entities import TickerOptionsMetrics


class StockDataProvider(ABC):
    """A gateway for retrieving stock data from some external source."""

    @abstractmethod
    def get_stock(self, symbol: str) -> Stock:
        """Return a Stock for the given (already-normalized) symbol.

        Raises:
            StockNotFound: the symbol does not exist / has no data.
            StockDataUnavailable: the upstream source failed.
        """
        raise NotImplementedError


class StockQuoteProvider(ABC):
    """A gateway for a stock's minimal live quote (price + day change).

    Separate from StockDataProvider because this backs a high-frequency polling
    endpoint: it returns only the snapshot-derived quote and skips the company
    metadata lookup, so a client refreshing every few seconds stays cheap.
    """

    @abstractmethod
    def get_quote(self, symbol: str) -> Quote:
        """Return the latest quote for the (already-normalized) symbol.

        Raises:
            StockNotFound: the symbol does not exist / has no data.
            StockDataUnavailable: the upstream source failed.
        """
        raise NotImplementedError


class StockPerformanceProvider(ABC):
    """A gateway for a stock's trailing price-return over standard windows.

    Separate from StockDataProvider: performance is derived from price history
    rather than the live snapshot, and the endpoint treats it as best-effort
    enrichment, so a failure here must not sink the price response.
    """

    @abstractmethod
    def get_performance(self, symbol: str) -> StockPerformance:
        """Return trailing-window performance for the (normalized) symbol.

        Raises:
            StockNotFound: the symbol has no price history.
            StockDataUnavailable: the upstream source failed.
        """
        raise NotImplementedError


class AllTimeHighProvider(ABC):
    """A gateway for a stock's all-time high over its available price history.

    Derived from the full span of daily bars rather than the live snapshot, like
    trailing performance — and likewise best-effort enrichment on the stock view,
    so a failure here must not sink the price response. "All-time" is bounded by
    how far back the source's history reaches (surfaced on the returned entity).
    """

    @abstractmethod
    def get_all_time_high(self, symbol: str) -> AllTimeHigh:
        """Return the all-time high for the (already-normalized) symbol.

        Raises:
            StockNotFound: the symbol has no price history.
            StockDataUnavailable: the upstream source failed.
        """
        raise NotImplementedError


class StockFundamentalsProvider(ABC):
    """A gateway for company fundamentals (market cap, dividend).

    These come from a fundamentals vendor, not the price feed — market data
    APIs don't expose shares outstanding or dividends. Best-effort enrichment.
    """

    @abstractmethod
    def get_fundamentals(self, symbol: str) -> StockFundamentals:
        """Return fundamentals for the (already-normalized) symbol.

        Raises:
            StockNotFound: the symbol is not covered by the source.
            StockDataUnavailable: the upstream source failed.
        """
        raise NotImplementedError


class CompanyProfileProvider(ABC):
    """A gateway for a company's clean display name.

    Comes from a company-profile vendor, not the price feed — market data APIs
    expose a ticker's full legal title but not the tidy display name. Best-effort
    enrichment, like fundamentals: a failure here must not sink the price view.
    """

    @abstractmethod
    def get_profile(self, symbol: str) -> CompanyProfile:
        """Return the company profile for the (already-normalized) symbol.

        Raises:
            StockNotFound: the symbol is not covered by the source.
            StockDataUnavailable: the upstream source failed.
        """
        raise NotImplementedError


class AnalystEstimatesProvider(ABC):
    """A gateway for a stock's forward analyst consensus estimates.

    Forward EPS/revenue expectations come from an estimates source — not the
    price feed or company filings — so this carries consensus *estimates*, never
    reported actuals. Best-effort enrichment on the stock snapshot (it backs the
    forward P/E and forward P/S), so a failure here must not sink the price response.
    """

    @abstractmethod
    def get_estimates(self, symbol: str) -> AnalystEstimates:
        """Return forward consensus estimates for the (already-normalized) symbol.

        Returns an ``is_empty`` ``AnalystEstimates`` (all ``None``) when the source
        covers no forward fiscal year for the symbol — "no data" is not an error for
        best-effort enrichment.

        Raises:
            StockNotFound: the symbol is not covered by the source.
            StockDataUnavailable: the upstream source failed.
        """
        raise NotImplementedError


class LogoProvider(ABC):
    """A gateway for retrieving a company's logo image.

    Separate from StockDataProvider because logos and market data come from
    different vendors — Alpaca's logo endpoint is paywalled, so logos are
    sourced elsewhere without disturbing the price-data adapter.
    """

    @abstractmethod
    def get_logo(self, symbol: str) -> Logo:
        """Return the logo for the given (already-normalized) symbol.

        Raises:
            StockNotFound: no logo is available for the symbol.
            StockDataUnavailable: the upstream source failed.
        """
        raise NotImplementedError


class CandleProvider(ABC):
    """A gateway for retrieving historical OHLC candles (chart data)."""

    @abstractmethod
    def get_candles(
        self,
        symbol: str,
        timeframe: Timeframe,
        *,
        start: datetime | None,
        end: datetime | None,
    ) -> CandleSeries:
        """Return chronological candles for the (already-normalized) symbol.

        Args:
            timeframe: granularity of each candle.
            start: window start (UTC); None means "as far back as available".
            end: window end (UTC); None means "up to now".

        Raises:
            StockNotFound: the symbol has no candle data in the window.
            StockDataUnavailable: the upstream source failed.
        """
        raise NotImplementedError


class SectorPerformanceProvider(ABC):
    """A gateway for each market sector's performance on the day.

    Sectors are read through their proxy ETFs rather than a dedicated sector
    feed, so this sits alongside the other price-derived ports.
    """

    @abstractmethod
    def get_sector_performance(self) -> list[SectorPerformance]:
        """Return the day's performance for every covered market sector.

        Raises:
            StockNotFound: no sector data is available at all.
            StockDataUnavailable: the upstream source failed.
        """
        raise NotImplementedError


class InvestmentAnalysisProvider(ABC):
    """A gateway that turns the data already gathered for a stock into a short,
    AI-generated buy / hold / sell read.

    Unlike the other ports this one isn't handed a symbol to look up — the use
    case has already assembled everything the read reasons over: the enriched
    ``Stock`` snapshot (price, performance, trailing + forward valuation/health
    metrics) and, when available, the recent quarterly and annual earnings
    timelines plus the options-market read. The adapter only reasons over what
    it's given and never fetches outside data. This backs a dedicated endpoint
    (its own reason to exist, not best-effort enrichment), so a failure surfaces
    as an error rather than being swallowed.
    """

    @abstractmethod
    def analyze(
        self,
        stock: Stock,
        quarterly: QuarterlyEarningsTimeline | None = None,
        annual: AnnualEarningsTimeline | None = None,
        options: TickerOptionsMetrics | None = None,
    ) -> InvestmentAnalysis:
        """Return a buy/hold/sell analysis built from the supplied data.

        Every argument beyond ``stock`` is best-effort *context* the use case
        gathers — the same data the ticker card and the earnings endpoints serve.
        Each is ``None`` when its source is unconfigured, uncovered, or briefly
        unreachable; the analysis stands on whatever it's handed.

        Args:
            stock: the enriched snapshot to reason over (price, performance,
                trailing + forward valuation/health metrics).
            quarterly: the recent quarterly earnings timeline, else ``None``.
            annual: the recent annual (fiscal-year) earnings timeline, else
                ``None``.
            options: the options-market read (implied volatility, expected move,
                cost of protection, put/call lean), else ``None``.

        Raises:
            StockDataUnavailable: the model call failed or returned no usable
                result.
        """
        raise NotImplementedError
