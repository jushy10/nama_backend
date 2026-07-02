"""Application port: the abstraction the use case depends on.

This is the Dependency Inversion that makes the slice clean: the use case
depends on this interface, and the adapter layer provides the Alpaca-backed
implementation. The core never imports Alpaca; Alpaca imports the core.
"""

from abc import ABC, abstractmethod
from datetime import date, datetime

from app.stocks.entities import (
    AllTimeHigh,
    AnalystEstimates,
    AnalystRecommendations,
    CandleSeries,
    CompanyProfile,
    Constituent,
    EarningsHistory,
    InvestmentAnalysis,
    Logo,
    NextEarnings,
    Quote,
    SectorPerformance,
    Stock,
    StockFundamentals,
    StockPerformance,
    Timeframe,
)


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


class EarningsHistoryProvider(ABC):
    """A gateway for a stock's recent quarterly earnings surprises.

    Actual-vs-estimate EPS comes from a fundamentals/estimates vendor, not the
    price feed. This backs a dedicated endpoint (not best-effort enrichment),
    so failures surface as errors rather than being swallowed.
    """

    @abstractmethod
    def get_earnings_history(self, symbol: str, *, limit: int) -> EarningsHistory:
        """Return up to ``limit`` recent quarters for the (normalized) symbol,
        newest first.

        Raises:
            StockNotFound: the symbol has no earnings data.
            StockDataUnavailable: the upstream source failed.
        """
        raise NotImplementedError


class EarningsCalendarProvider(ABC):
    """A gateway for a stock's next scheduled earnings report.

    The forward view — expected report date plus the consensus EPS/revenue
    going in — from an earnings-calendar vendor. Best-effort enrichment on the
    earnings endpoint: ``None`` (not an error) when nothing is scheduled, so a
    name between reporting cycles simply has no forward bar.
    """

    @abstractmethod
    def get_next_earnings(self, symbol: str) -> NextEarnings | None:
        """Return the next scheduled report for the (normalized) symbol, or
        ``None`` when none is scheduled.

        Raises:
            StockDataUnavailable: the upstream source failed.
        """
        raise NotImplementedError


class RecommendationProvider(ABC):
    """A gateway for a stock's analyst recommendation trends.

    The sell-side buy/hold/sell split, by month — the "what does the street
    think?" forward view, from a ratings vendor rather than the price feed. Backs
    a dedicated endpoint (its own card on the stock page), so it's primary data
    for that endpoint and failures surface as errors. "No analyst coverage" is
    not a failure, though: it returns an empty ``AnalystRecommendations`` rather
    than raising.
    """

    @abstractmethod
    def get_recommendations(self, symbol: str) -> AnalystRecommendations:
        """Return recommendation trends for the (already-normalized) symbol,
        newest snapshot first (empty when no analyst covers it).

        Raises:
            StockNotFound: the symbol does not exist / isn't covered.
            StockDataUnavailable: the upstream source failed.
        """
        raise NotImplementedError


class RevenueHistoryProvider(ABC):
    """A gateway for a stock's recently-reported quarterly revenue (actuals).

    Reported figures, not the price feed or an estimates vendor — so it carries
    only what the company actually reported, never a consensus estimate.
    Best-effort enrichment on the earnings endpoint: the use case aligns these
    figures onto the EPS beat history by fiscal period end.
    """

    @abstractmethod
    def get_quarterly_revenue(self, symbol: str) -> dict[date, float]:
        """Return recently-reported quarterly revenue keyed by fiscal period end.

        Each key is a quarter's period-end date and each value the revenue
        reported for that quarter (raw, e.g. USD). Quarters that can't be derived
        are simply absent; an empty map means no revenue was available
        (best-effort, never an error for "no data").

        Raises:
            StockNotFound: the symbol isn't covered by the source.
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


class QuoteBatchProvider(ABC):
    """A gateway for many symbols' latest quotes in as few calls as possible.

    Backs the screener, which ranks a whole index's day move: fetching symbols
    one at a time would be far too many round-trips. Coverage is best-effort —
    a symbol the feed doesn't carry is simply omitted — so callers must tolerate
    a partial map.
    """

    @abstractmethod
    def get_quotes(self, symbols: list[str]) -> dict[str, Quote]:
        """Return the latest quote for each symbol that has one, keyed by symbol.

        Best-effort and total: symbols the source can't price are left out, and
        a transport failure yields a (partial or empty) map rather than raising
        — the screener decides what an empty result means.
        """
        raise NotImplementedError


class ConstituentRepository(ABC):
    """A source of the screener's universe: index membership + sector per name.

    This is static reference data, not a live feed — hence a repository rather
    than a price ``*Provider``. The use case applies the index/sector filtering;
    the repository just hands over the full universe.
    """

    @abstractmethod
    def all(self) -> tuple[Constituent, ...]:
        """Return every known constituent."""
        raise NotImplementedError


class InvestmentAnalysisProvider(ABC):
    """A gateway that turns the data already gathered for a stock into a short,
    AI-generated buy / hold / sell read.

    Unlike the other ports this one isn't handed a symbol to look up — the use
    case has already assembled the enriched ``Stock`` (price, performance,
    valuation/health metrics) and, when available, the recent
    ``EarningsHistory``. The adapter only reasons over what it's given and never
    fetches outside data. This backs a dedicated endpoint (its own reason to
    exist, not best-effort enrichment), so a failure surfaces as an error rather
    than being swallowed.
    """

    @abstractmethod
    def analyze(
        self, stock: Stock, earnings: EarningsHistory | None = None
    ) -> InvestmentAnalysis:
        """Return a buy/hold/sell analysis built from the supplied data.

        Args:
            stock: the enriched snapshot to reason over (price, performance,
                valuation/health metrics).
            earnings: recent quarterly beat history when available, else
                ``None`` (the earnings source isn't configured, or doesn't cover
                the symbol) — the analysis can stand without it.

        Raises:
            StockDataUnavailable: the model call failed or returned no usable
                result.
        """
        raise NotImplementedError
