"""Application port: the abstraction the use case depends on.

This is the Dependency Inversion that makes the slice clean: the use case
depends on this interface, and the adapter layer provides the Alpaca-backed
implementation. The core never imports Alpaca; Alpaca imports the core.
"""

from abc import ABC, abstractmethod
from collections.abc import Sequence
from datetime import date, datetime
from typing import Generic, TypeVar

from app.stocks.earnings.annual.entities import AnnualEarningsTimeline
from app.stocks.earnings.quarterly.entities import QuarterlyEarningsTimeline
from app.stocks.entities import (
    AllTimeHigh,
    AnalystEstimates,
    CandleSeries,
    CompanyProfile,
    EarningsAnalysis,
    FundamentalsAnalysis,
    InvestmentAnalysis,
    Logo,
    MarketIndexPerformance,
    MarketSummary,
    Quote,
    RatingsAnalysis,
    SectorAnalysis,
    SectorPerformance,
    Stock,
    StockFundamentals,
    StockPerformance,
    StockScorecard,
    Timeframe,
)
from app.stocks.recommendations.entities import AnalystRecommendations, FirmRating
from app.stocks.universe.entities import IndustryValuation


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


class BulkQuoteProvider(ABC):
    """A gateway for many symbols' live quotes in one call — the batched cousin of
    ``StockQuoteProvider``.

    Backs a view that colours a whole board by the day's move (the heat map): one request for
    the entire symbol list instead of N per-symbol calls. **Best-effort per symbol** — a symbol
    the feed carries no quote for (e.g. not on the free IEX feed) is simply *absent* from the
    returned map, never an error, so the caller can size that tile from stored facts and leave
    it uncoloured. Only a hard feed failure over the whole batch is fatal.
    """

    @abstractmethod
    def get_quotes(self, symbols: Sequence[str]) -> dict[str, Quote]:
        """Return the latest quote for each recognized symbol, keyed by symbol.

        Symbols the feed has no quote for are omitted (a partial map is normal, not an error);
        order and duplicates in the input don't matter. Given an empty input, returns an empty
        map without a call.

        Raises:
            StockDataUnavailable: the upstream feed failed for the whole request.
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


class BulkPerformanceProvider(ABC):
    """A gateway for many symbols' trailing performance in one batched read — the bulk
    cousin of ``StockPerformanceProvider``.

    Backs the heat map's timeframe windows: instead of colouring the board only by the
    day's move, each tile also carries its trailing return over the standard windows
    (1W…1Y, YTD), computed once for the whole index rather than N per-symbol calls.
    **Best-effort per symbol** — a symbol the feed has no history for (e.g. not on the
    historical feed, or too newly listed) is simply *absent* from the returned map, so
    the caller leaves that tile's trailing windows blank; only a hard feed failure over
    the whole batch is fatal.
    """

    @abstractmethod
    def get_bulk_performance(
        self, symbols: Sequence[str]
    ) -> dict[str, StockPerformance]:
        """Return trailing-window performance for each recognized symbol, keyed by symbol.

        Symbols the feed has no history for are omitted (a partial map is normal, not an
        error); order and duplicates in the input don't matter. Given an empty input,
        returns an empty map without a call.

        Raises:
            StockDataUnavailable: the upstream feed failed for the whole request.
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


class MarketOverviewProvider(ABC):
    """A gateway for the headline US indices' performance on the day.

    Like ``SectorPerformanceProvider``, the indices aren't directly tradable, so
    each is read through its proxy ETF (SPY -> S&P 500, QQQ -> Nasdaq); this sits
    alongside the other price-derived ports.
    """

    @abstractmethod
    def get_market_overview(self) -> list[MarketIndexPerformance]:
        """Return the day's performance for each headline US index.

        Raises:
            StockNotFound: no index data is available at all.
            StockDataUnavailable: the upstream source failed.
        """
        raise NotImplementedError


class StockScorecardProvider(ABC):
    """A gateway that turns the data already gathered for a stock into a short,
    AI-generated, **sectioned** buy / hold / sell read (a ``StockScorecard``).

    Unlike the other ports this one isn't handed a symbol to look up — the use
    case has already assembled everything the read reasons over: the enriched
    ``Stock`` snapshot (price, performance, trailing + forward valuation/health
    metrics) and, when available, the recent quarterly and annual earnings
    timelines, the analyst recommendation trends, and the stock's industry P/E
    benchmark. The adapter only reasons over what it's given and never fetches
    outside data. This backs a dedicated endpoint (its own reason to exist, not
    best-effort enrichment), so a failure surfaces as an error rather than being
    swallowed.
    """

    @abstractmethod
    def analyze(
        self,
        stock: Stock,
        quarterly: QuarterlyEarningsTimeline | None = None,
        annual: AnnualEarningsTimeline | None = None,
        recommendations: AnalystRecommendations | None = None,
        industry_valuation: IndustryValuation | None = None,
    ) -> StockScorecard:
        """Return a sectioned buy/hold/sell scorecard built from the supplied data.

        Every argument beyond ``stock`` is best-effort *context* the use case
        gathers — the same data the earnings and recommendations endpoints serve.
        Each is ``None`` when its source is unconfigured, uncovered, or briefly
        unreachable; the analysis stands on whatever it's handed.

        Args:
            stock: the enriched snapshot to reason over (price, performance,
                trailing + forward valuation/health metrics).
            quarterly: the recent quarterly earnings timeline, else ``None``.
            annual: the recent annual (fiscal-year) earnings timeline, else
                ``None``.
            recommendations: the analyst recommendation trends (the sell-side
                buy/hold/sell consensus and its direction), else ``None``.
            industry_valuation: the industry P/E benchmark (median + quartiles
                over the stock's screened peers), so the model can judge its
                trailing multiple against its peers, else ``None``.

        Raises:
            StockDataUnavailable: the model call failed or returned no usable
                result.
        """
        raise NotImplementedError


class StockScorecardCache(ABC):
    """A persistence gateway that stores the most recent ``StockScorecard`` per symbol.

    The scorecard is expensive to produce (a language-model call on top of a
    multi-source data gather) yet only drifts as the underlying figures do, so a
    read-through cache lets a burst of viewers — and repeat views within the
    window — collapse onto one generation. The **freshness policy is the use
    case's** (it compares ``generated_at`` against a TTL): this port only stores
    and returns the latest stored read, one row per symbol.

    The sectioned sibling of ``InvestmentAnalysisCache`` (which the ETF analysis
    still uses): same best-effort contract, a different stored shape. Both
    operations are best-effort — a read failure (a DB hiccup) is treated as a miss
    so the caller regenerates, and a write failure is swallowed (the caller already
    holds a good answer). Neither ever raises.
    """

    @abstractmethod
    def get(self, symbol: str) -> StockScorecard | None:
        """Return the stored scorecard for ``symbol`` (any age), or ``None`` on a
        miss or a cache-read failure. The caller decides whether it's fresh."""
        raise NotImplementedError

    @abstractmethod
    def put(self, scorecard: StockScorecard) -> None:
        """Store ``scorecard`` as the latest for its symbol (upsert). A write
        failure is swallowed — caching must never sink the request."""
        raise NotImplementedError


class InvestmentAnalysisCache(ABC):
    """A persistence gateway that stores the most recent AI analysis per symbol.

    The analysis is expensive to produce (a language-model call on top of a
    multi-source data gather) yet only drifts as the underlying figures do, so a
    read-through cache lets a burst of viewers — and repeat views within the
    window — collapse onto one generation. The **freshness policy is the use
    case's** (it compares ``generated_at`` against a TTL): this port only stores
    and returns the latest stored read, one row per symbol.

    Now the **ETF** analysis's cache (the stock endpoint moved to the sectioned
    ``StockScorecardCache``); the concrete adapter is instantiated per *kind* so a
    fund never collides with a stock of the same ticker.

    Being a cache, both operations are best-effort: a read failure (a DB hiccup)
    is treated as a miss so the caller regenerates, and a write failure is
    swallowed — the caller already holds a good answer. Neither ever raises.
    """

    @abstractmethod
    def get(self, symbol: str) -> InvestmentAnalysis | None:
        """Return the stored analysis for ``symbol`` (any age), or ``None`` on a
        miss or a cache-read failure. The caller decides whether it's fresh."""
        raise NotImplementedError

    @abstractmethod
    def put(self, analysis: InvestmentAnalysis) -> None:
        """Store ``analysis`` as the latest for its symbol (upsert). A write
        failure is swallowed — caching must never sink the request."""
        raise NotImplementedError


T = TypeVar("T")


class AiAnalysisCache(ABC, Generic[T]):
    """A read-through result cache for one *kind* of AI analysis, keyed by a string.

    The generic counterpart to ``StockScorecardCache`` / ``InvestmentAnalysisCache``:
    those two are hand-written per shape, but the five remaining AI reads (earnings,
    ratings, fundamentals, sector, market) share this one parameterized port so the
    slice doesn't grow five near-identical ABCs. Each is expensive to produce (a
    language-model call over a multi-source gather) yet only drifts as its underlying
    figures do, so a fresh stored read lets a burst of viewers — and repeat views
    within the window — collapse onto one generation.

    The **freshness policy is the use case's** (it ages ``generated_at`` against a
    TTL): this port only stores and returns the latest read for a ``key``. The ``key``
    is the normalized symbol for a per-symbol read, or a fixed sentinel for a
    market-wide one (which takes no symbol) — the concrete adapter is bound to a
    *kind* so the two never collide, exactly like the existing two caches.

    Being a cache, both operations are best-effort: a read failure (a DB hiccup, or a
    stored enum this build no longer parses) is treated as a miss so the caller
    regenerates, and a write failure is swallowed — the caller already holds a good
    answer. Neither ever raises, so a cache problem can never sink an analysis request.
    """

    @abstractmethod
    def get(self, key: str) -> T | None:
        """Return the stored analysis for ``key`` (any age), or ``None`` on a miss or
        a cache-read failure. The caller decides whether it's fresh."""
        raise NotImplementedError

    @abstractmethod
    def put(self, key: str, analysis: T) -> None:
        """Store ``analysis`` as the latest for ``key`` (upsert). A write failure is
        swallowed — caching must never sink the request."""
        raise NotImplementedError


class SectorAnalysisProvider(ABC):
    """A gateway that turns the day's ranked sector board into a short,
    AI-generated read of which market sectors are leading and lagging.

    The market-wide sibling of ``StockScorecardProvider``: like it, this port
    isn't handed a lookup key — the use case has already assembled the board (each
    sector's daily move + trailing returns). The adapter reasons only over what
    it's given and fetches nothing. This backs a dedicated endpoint (its own reason
    to exist, not best-effort enrichment), so a failure surfaces as an error rather
    than being swallowed.
    """

    @abstractmethod
    def analyze(self, sectors: list[SectorPerformance]) -> SectorAnalysis:
        """Return a market-sector analysis built from the ranked board.

        Args:
            sectors: the day's sectors, already ranked best performer first, each
                carrying its daily move and best-effort trailing-window returns.

        Raises:
            StockDataUnavailable: the model call failed or returned no usable
                result.
        """
        raise NotImplementedError


class MarketSummaryProvider(ABC):
    """A gateway that turns the day's index board into a short, AI-generated
    overview of how the US market has moved over the past year, month and week.

    The market-wide sibling of ``SectorAnalysisProvider``: like it, this port
    isn't handed a lookup key — the use case has already assembled the board (each
    index's daily move + trailing returns). The adapter reasons only over what
    it's given and fetches nothing. This backs a dedicated endpoint (its own
    reason to exist, not best-effort enrichment), so a failure surfaces as an
    error rather than being swallowed.
    """

    @abstractmethod
    def analyze(self, indexes: list[MarketIndexPerformance]) -> MarketSummary:
        """Return a market summary built from the index board.

        Args:
            indexes: the day's headline indices, each carrying its daily move and
                best-effort trailing-window returns.

        Raises:
            StockDataUnavailable: the model call failed or returned no usable
                result.
        """
        raise NotImplementedError


class EarningsAnalysisProvider(ABC):
    """A gateway that turns a stock's earnings timelines into a short,
    AI-generated read of its earnings story.

    The earnings-focused sibling of ``StockScorecardProvider``: the use case
    has already gathered the quarterly and annual earnings timelines, and the
    adapter reasons only over what it's handed (the beats/misses, EPS and revenue
    trajectory, and the forward consensus) — it fetches nothing itself. This backs
    a dedicated endpoint (its own reason to exist, not best-effort enrichment), so
    a failure surfaces as an error rather than being swallowed.
    """

    @abstractmethod
    def analyze(
        self,
        symbol: str,
        quarterly: QuarterlyEarningsTimeline | None = None,
        annual: AnnualEarningsTimeline | None = None,
    ) -> EarningsAnalysis:
        """Return an earnings analysis built from the supplied timelines.

        Args:
            symbol: the ticker being analysed (for labelling and error context).
            quarterly: the recent quarterly earnings timeline, else ``None``.
            annual: the recent annual (fiscal-year) earnings timeline, else
                ``None``.

        Raises:
            StockDataUnavailable: the model call failed or returned no usable
                result.
        """
        raise NotImplementedError


class RatingsAnalysisProvider(ABC):
    """A gateway that turns a stock's analyst coverage into a short, AI-generated read.

    The analyst-ratings sibling of ``EarningsAnalysisProvider``: the use case has already
    gathered the recommendation consensus (trends + price targets) and the most credible covering
    firms' stances, and the adapter reasons only over what it's handed — it fetches nothing
    itself. This backs a dedicated endpoint (its own reason to exist, not best-effort
    enrichment), so a failure surfaces as an error rather than being swallowed.
    """

    @abstractmethod
    def analyze(
        self,
        symbol: str,
        recommendations: AnalystRecommendations | None = None,
        top_firms: tuple[FirmRating, ...] = (),
    ) -> RatingsAnalysis:
        """Return a ratings analysis built from the supplied coverage.

        Args:
            symbol: the ticker being analysed (for labelling and error context).
            recommendations: the sell-side buy/hold/sell consensus + price targets, else
                ``None``.
            top_firms: the most credible covering firms' current stances (may be empty).

        Raises:
            StockDataUnavailable: the model call failed or returned no usable
                result.
        """
        raise NotImplementedError


class FundamentalsAnalysisProvider(ABC):
    """A gateway that turns a stock's fundamentals into a short, AI-generated read.

    The fundamentals-focused sibling of ``EarningsAnalysisProvider`` and
    ``RatingsAnalysisProvider``: the use case has already assembled the enriched stock snapshot
    (the trailing/forward valuation multiples, the profitability and balance-sheet metrics, the
    growth figures, the dividend and market cap) and, best-effort, the stock's industry-P/E
    benchmark, and the adapter reasons only over what it's handed — it fetches nothing itself.
    This backs a dedicated endpoint (its own reason to exist, not best-effort enrichment), so a
    failure surfaces as an error rather than being swallowed.
    """

    @abstractmethod
    def analyze(
        self,
        stock: Stock,
        industry_valuation: IndustryValuation | None = None,
    ) -> FundamentalsAnalysis:
        """Return a fundamentals analysis built from the supplied snapshot.

        Args:
            stock: the enriched stock snapshot — its ``metrics`` (trailing valuation /
                profitability / health / growth), ``analyst_estimates`` (forward consensus),
                dividend and market cap. The symbol is read off it.
            industry_valuation: the stock's industry-P/E peer benchmark, else ``None``.

        Raises:
            StockDataUnavailable: the model call failed or returned no usable
                result.
        """
        raise NotImplementedError
