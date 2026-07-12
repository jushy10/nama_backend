"""Application ports: the abstractions the AI-analysis use cases depend on.

One provider port per analyser — each is handed data the use case has already
gathered (never a symbol to look up) and returns an entity. The Bedrock adapters
in ``adapters/bedrock/`` implement them. The result caches are the persistence
ports here: the hand-written ``StockScorecardCache`` / ``InvestmentAnalysisCache``
(the stock scorecard and the ETF analysis) and the generic ``AiAnalysisCache``
the five remaining reads share.
"""

from abc import ABC, abstractmethod
from typing import Generic, TypeVar

from app.stocks.analysis.entities import (
    EarningsAnalysis,
    FundamentalsAnalysis,
    InvestmentAnalysis,
    MarketSummary,
    RatingsAnalysis,
    SectorAnalysis,
    StockScorecard,
)
from app.stocks.earnings.annual.entities import AnnualEarningsTimeline
from app.stocks.earnings.quarterly.entities import QuarterlyEarningsTimeline
from app.stocks.entities import Stock
from app.stocks.market.entities import MarketIndexPerformance, SectorPerformance
from app.stocks.recommendations.entities import AnalystRecommendations, FirmRating
from app.stocks.ticker.entities import PeHistoryStats
from app.stocks.universe.entities import IndustryValuation


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
        pe_history: PeHistoryStats | None = None,
    ) -> FundamentalsAnalysis:
        """Return a fundamentals analysis built from the supplied snapshot.

        Args:
            stock: the enriched stock snapshot — its ``metrics`` (trailing valuation /
                profitability / health / growth), ``analyst_estimates`` (forward consensus),
                dividend and market cap. The symbol is read off it.
            industry_valuation: the stock's industry-P/E peer benchmark, else ``None``.
            pe_history: where the current trailing P/E sits within the stock's own history
                (percentile + cheap/fair/expensive signal), else ``None`` — the "cheap for
                this stock?" anchor that complements the peer benchmark.

        Raises:
            StockDataUnavailable: the model call failed or returned no usable
                result.
        """
        raise NotImplementedError
