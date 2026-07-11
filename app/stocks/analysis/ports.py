"""Application ports: the abstractions the AI-analysis use cases depend on.

One port per analyser — each is handed data the use case has already gathered
(never a symbol to look up) and returns an entity. The Bedrock adapters in
``adapters/bedrock/`` implement them; the result cache is the one persistence
port here, implemented by ``analysis/db_repository.py``.
"""

from abc import ABC, abstractmethod

from app.stocks.analysis.entities import (
    EarningsAnalysis,
    InvestmentAnalysis,
    MarketSummary,
    RatingsAnalysis,
    SectorAnalysis,
)
from app.stocks.earnings.annual.entities import AnnualEarningsTimeline
from app.stocks.earnings.quarterly.entities import QuarterlyEarningsTimeline
from app.stocks.entities import Stock
from app.stocks.market.entities import MarketIndexPerformance, SectorPerformance
from app.stocks.recommendations.entities import AnalystRecommendations, FirmRating
from app.stocks.universe.entities import IndustryValuation


class InvestmentAnalysisProvider(ABC):
    """A gateway that turns the data already gathered for a stock into a short,
    AI-generated buy / hold / sell read.

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
    ) -> InvestmentAnalysis:
        """Return a buy/hold/sell analysis built from the supplied data.

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


class InvestmentAnalysisCache(ABC):
    """A persistence gateway that stores the most recent AI analysis per symbol.

    The analysis is expensive to produce (a language-model call on top of a
    multi-source data gather) yet only drifts as the underlying figures do, so a
    read-through cache lets a burst of viewers — and repeat views within the
    window — collapse onto one generation. The **freshness policy is the use
    case's** (it compares ``generated_at`` against a TTL): this port only stores
    and returns the latest stored read, one row per symbol. Both the stock and the
    ETF analysers share the port; the concrete adapter is instantiated per *kind*
    (a stock vs. a fund) so the two never collide on a shared ticker.

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


class SectorAnalysisProvider(ABC):
    """A gateway that turns the day's ranked sector board into a short,
    AI-generated read of which market sectors are leading and lagging.

    The market-wide sibling of ``InvestmentAnalysisProvider``: like it, this port
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

    The earnings-focused sibling of ``InvestmentAnalysisProvider``: the use case
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
