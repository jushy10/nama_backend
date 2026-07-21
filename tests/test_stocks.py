from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.stocks.adapters.bedrock.bedrock_stock_scorecard_adapter import (
    BedrockStockScorecardAdapter,
    _SECTIONS as _SCORECARD_SECTIONS,
)
from app.stocks.adapters.bedrock.bedrock_earnings_analysis_adapter import (
    BedrockEarningsAnalysisAdapter,
)
from app.stocks.adapters.bedrock.bedrock_market_summary_adapter import (
    BedrockMarketSummaryAdapter,
)
from app.stocks.adapters.bedrock.bedrock_sector_analysis_adapter import (
    BedrockSectorAnalysisAdapter,
)
from app.stocks.company.charts.chart_window import ChartRange, resolve_window
from app.stocks.ai.analysis.entities import (
    Confidence,
    EarningsAnalysis,
    EarningsTrend,
    MarketIndexReturn,
    MarketPeriod,
    MarketPeriodHighlight,
    MarketSummary,
    MarketTone,
    Recommendation,
    ScorecardSection,
    SectionMetric,
    SectionStance,
    SectorAnalysis,
    SectorContext,
    SectorHeadline,
    SectorHighlight,
    SectorMover,
    StockScorecard,
)
from app.stocks.ai.analysis.interfaces import (
    AiAnalysisCacheAdapter,
    EarningsAnalysisAdapter,
    MarketSummaryAdapter,
    SectorAnalysisAdapter,
    StockScorecardCacheAdapter,
    StockScorecardAdapter,
)
from app.stocks.ai.analysis.use_cases import (
    GetEarningsAnalysis,
    GetMarketSummary,
    GetSectorAnalysis,
    GetStockAnalysis,
    GetStockInfo,
)
from app.stocks.company.charts.indicators import TrendDirection, TrendReading
from app.stocks.company.charts.ports import CandleProvider
from app.stocks.company.charts.use_cases import (
    GetStockCandles,
    GetStockEma,
    GetStockSupportLevels,
    GetStockTrend,
)
from app.stocks.company.earnings.annual.entities import (
    AnnualEarnings,
    AnnualEarningsTimeline,
)
from app.stocks.company.earnings.annual.ports import AnnualEarningsProvider
from app.stocks.company.earnings.quarterly.entities import (
    QuarterlyEarnings,
    QuarterlyEarningsTimeline,
)
from app.stocks.company.earnings.quarterly.ports import QuarterlyEarningsProvider
from app.stocks.endpoints.analysis_endpoints import (
    get_market_summary,
    get_sector_analysis,
    get_stock_analysis,
)
from app.stocks.endpoints.chart_endpoints import (
    get_stock_candles,
    get_stock_ema,
    get_stock_support_levels,
    get_stock_trend,
)
from app.stocks.endpoints.logo_endpoints import get_stock_logo
from app.stocks.endpoints.market_endpoints import get_sector_performance
from app.stocks.entities import (
    AllTimeHigh,
    AnalystEstimates,
    Candle,
    CandleSeries,
    GrowthMetrics,
    KeyMetrics,
    MarketSession,
    Quote,
    Stock,
    StockPerformance,
    Timeframe,
    market_session_at,
    normalize_symbol,
)
from app.stocks.exceptions import StockDataUnavailable, StockNotFound
from app.stocks.company.logo.entities import Logo
from app.stocks.company.logo.ports import LogoProvider
from app.stocks.company.logo.use_cases import GetStockLogo
from app.stocks.market.boards.entities import MarketIndexPerformance, SectorPerformance
from app.stocks.market.boards.ports import MarketOverviewProvider, SectorPerformanceProvider
from app.stocks.market.boards.use_cases import GetMarketOverview, GetSectorPerformance
from app.stocks.ports import (
    AllTimeHighProvider,
    AnalystEstimatesProvider,
    StockDataProvider,
    StockPerformanceProvider,
)
from app.stocks.company.recommendations.entities import (
    AnalystRecommendations,
    RecommendationTrend,
)
from app.stocks.company.recommendations.ports import RecommendationProvider
from app.stocks.company.news.entities import NewsArticle, StockNews
from app.stocks.company.news.repository import NewsRepository
from app.stocks.catalog.universe.entities import (
    AnchorMetrics,
    IndustryValuation,
    MarketCapTier,
    SortDirection,
    StockSearchCriteria,
    StockSearchPage,
    StockSearchResult,
    StockSort,
)
from app.stocks.catalog.universe.repository import StockSearchRepository
from app.stocks.wiring import analysis_cache_ttl


class FakeProvider(StockDataProvider):
    def __init__(self, stock: Stock | None = None, raises: Exception | None = None):
        self._stock = stock
        self._raises = raises
        self.received: list[str] = []

    def get_stock(self, symbol: str) -> Stock:
        self.received.append(symbol)
        if self._raises is not None:
            raise self._raises
        assert self._stock is not None
        return self._stock


class FakeLogoProvider(LogoProvider):
    def __init__(self, logo: Logo | None = None, raises: Exception | None = None):
        self._logo = logo
        self._raises = raises
        self.received: list[str] = []

    def get_logo(self, symbol: str) -> Logo:
        self.received.append(symbol)
        if self._raises is not None:
            raise self._raises
        assert self._logo is not None
        return self._logo


class FakePerformanceProvider(StockPerformanceProvider):
    def __init__(self, performance=None, raises=None):
        self._performance = performance
        self._raises = raises
        self.received: list[str] = []

    def get_performance(self, symbol: str) -> StockPerformance:
        self.received.append(symbol)
        if self._raises is not None:
            raise self._raises
        assert self._performance is not None
        return self._performance


class FakeAllTimeHighProvider(AllTimeHighProvider):
    def __init__(self, all_time_high=None, raises=None):
        self._all_time_high = all_time_high
        self._raises = raises
        self.received: list[str] = []

    def get_all_time_high(self, symbol: str) -> AllTimeHigh:
        self.received.append(symbol)
        if self._raises is not None:
            raise self._raises
        assert self._all_time_high is not None
        return self._all_time_high


class FakeEstimatesProvider(AnalystEstimatesProvider):
    def __init__(self, estimates=None, raises=None):
        self._estimates = estimates
        self._raises = raises
        self.received: list[str] = []

    def get_estimates(self, symbol: str) -> AnalystEstimates:
        self.received.append(symbol)
        if self._raises is not None:
            raise self._raises
        assert self._estimates is not None
        return self._estimates


class FakeCandleProvider(CandleProvider):
    def __init__(
        self, series: CandleSeries | None = None, raises: Exception | None = None
    ):
        self._series = series
        self._raises = raises
        self.received: list[tuple] = []

    def get_candles(self, symbol, timeframe, *, start=None, end=None) -> CandleSeries:
        self.received.append((symbol, timeframe, start, end))
        if self._raises is not None:
            raise self._raises
        assert self._series is not None
        return self._series


class FakeSectorProvider(SectorPerformanceProvider):
    def __init__(self, sectors=None, raises: Exception | None = None):
        self._sectors = sectors
        self._raises = raises
        self.calls = 0

    def get_sector_performance(self) -> list[SectorPerformance]:
        self.calls += 1
        if self._raises is not None:
            raise self._raises
        assert self._sectors is not None
        return self._sectors


class FakeMarketOverviewProvider(MarketOverviewProvider):
    def __init__(self, indexes=None, raises: Exception | None = None):
        self._indexes = indexes
        self._raises = raises
        self.calls = 0

    def get_market_overview(self) -> list[MarketIndexPerformance]:
        self.calls += 1
        if self._raises is not None:
            raise self._raises
        assert self._indexes is not None
        return self._indexes


class FakeQuarterlyEarningsProvider(QuarterlyEarningsProvider):
    def __init__(self, timeline=None, raises: Exception | None = None):
        self._timeline = timeline
        self._raises = raises

    def get_quarterly_earnings(self, symbol: str) -> QuarterlyEarningsTimeline:
        if self._raises is not None:
            raise self._raises
        assert self._timeline is not None
        return self._timeline


class FakeAnnualEarningsProvider(AnnualEarningsProvider):
    def __init__(self, timeline=None, raises: Exception | None = None):
        self._timeline = timeline
        self._raises = raises

    def get_annual_earnings(self, symbol: str) -> AnnualEarningsTimeline:
        if self._raises is not None:
            raise self._raises
        assert self._timeline is not None
        return self._timeline


class FakeRecommendationProvider(RecommendationProvider):
    def __init__(self, recommendations=None, raises: Exception | None = None):
        self._recommendations = recommendations
        self._raises = raises

    def get_recommendations(self, symbol: str) -> AnalystRecommendations:
        if self._raises is not None:
            raise self._raises
        assert self._recommendations is not None
        return self._recommendations


class FakeSearchRepo(StockSearchRepository):
    def __init__(
        self,
        *,
        industry: str | None = None,
        pe_ratios: tuple[float, ...] = (),
        peers: tuple[tuple[float, MarketCapTier], ...] | None = None,
        anchor_tier: MarketCapTier | None = MarketCapTier.MID,
        fcf_per_share: float | None = None,
        revenue_growth_yoy: float | None = None,
        eps_growth_yoy: float | None = None,
        gross_margin: float | None = None,
        operating_margin: float | None = None,
        net_margin: float | None = None,
        return_on_equity: float | None = None,
        current_ratio: float | None = None,
        debt_to_equity: float | None = None,
        beta: float | None = None,
        book_value_per_share: float | None = None,
        sales_per_share: float | None = None,
        dividend_per_share: float | None = None,
        market_cap: float | None = None,
        name: str | None = None,
        raises: Exception | None = None,
    ):
        self._industry = industry
        self._peers = (
            peers
            if peers is not None
            else tuple((pe, MarketCapTier.MID) for pe in pe_ratios)
        )
        self._anchor_tier = anchor_tier
        self._anchor_metrics = AnchorMetrics(
            fcf_per_share=fcf_per_share,
            revenue_growth_yoy=revenue_growth_yoy,
            eps_growth_yoy=eps_growth_yoy,
            gross_margin=gross_margin,
            operating_margin=operating_margin,
            net_margin=net_margin,
            return_on_equity=return_on_equity,
            current_ratio=current_ratio,
            debt_to_equity=debt_to_equity,
            beta=beta,
            book_value_per_share=book_value_per_share,
            sales_per_share=sales_per_share,
            dividend_per_share=dividend_per_share,
            market_cap=market_cap,
            name=name,
        )
        self._raises = raises

    def industry_for_ticker(self, ticker: str) -> str | None:
        if self._raises is not None:
            raise self._raises
        return self._industry

    def anchor_metrics_for_ticker(self, ticker: str) -> AnchorMetrics:
        if self._raises is not None:
            raise self._raises
        return self._anchor_metrics

    def tier_for_ticker(self, ticker: str) -> MarketCapTier | None:
        if self._raises is not None:
            raise self._raises
        return self._anchor_tier

    def industry_peers(
        self, industry: str
    ) -> tuple[tuple[float, MarketCapTier], ...]:
        if self._raises is not None:
            raise self._raises
        return self._peers

    def pe_ratios_for_industry(
        self, industry: str
    ) -> tuple[float, ...]:  # pragma: no cover - the endpoint path, not the analysis one
        if self._raises is not None:
            raise self._raises
        return tuple(pe for pe, _ in self._peers)

    def peers_for_industry(self, industry):  # pragma: no cover - not the analysis path
        raise NotImplementedError

    def search(self, criteria):  # pragma: no cover - not used by the analysis path
        raise NotImplementedError

    def classifications(self):  # pragma: no cover - not used by the analysis path
        raise NotImplementedError


class FakeAnalysisProvider(StockScorecardAdapter):
    def __init__(
        self,
        analysis: StockScorecard | None = None,
        raises: Exception | None = None,
    ):
        self._analysis = analysis
        self._raises = raises
        self.received: list[tuple[str, bool]] = []
        self.last_stock = None
        self.last_quarterly = None
        self.last_annual = None
        self.last_recommendations = None
        self.last_industry_valuation = None

    def analyze(
        self,
        stock,
        quarterly=None,
        annual=None,
        recommendations=None,
        industry_valuation=None,
    ) -> StockScorecard:
        self.received.append((stock.symbol, quarterly is not None))
        self.last_stock = stock
        self.last_quarterly = quarterly
        self.last_annual = annual
        self.last_recommendations = recommendations
        self.last_industry_valuation = industry_valuation
        if self._raises is not None:
            raise self._raises
        assert self._analysis is not None
        return self._analysis


def a_section(**overrides) -> ScorecardSection:
    base = dict(
        key="business_quality",
        title="Business quality",
        stance=SectionStance.POSITIVE,
        label="Exceptional",
        summary="Keeps roughly half of every dollar of sales as profit.",
        metrics=(SectionMetric("Net margin", "25.00%"),),
    )
    base.update(overrides)
    return ScorecardSection(**base)


def an_analysis(**overrides) -> StockScorecard:
    base = dict(
        symbol="AAPL",
        recommendation=Recommendation.HOLD,
        confidence=Confidence.MEDIUM,
        thesis="Solid franchise; the valuation already reflects much of the growth.",
        sections=(
            a_section(),
            a_section(
                key="valuation",
                title="Valuation",
                stance=SectionStance.NEGATIVE,
                label="Expensive",
                summary="Priced well above its industry peers.",
                metrics=(SectionMetric("P/E (trailing)", "28.50"),),
            ),
            a_section(
                key="earnings",
                title="Earnings",
                stance=SectionStance.POSITIVE,
                label="Beating estimates",
                summary="Has topped expectations every recent quarter.",
                metrics=(SectionMetric("Beat rate", "4/4 quarters"),),
            ),
            a_section(
                key="analyst_view",
                title="Analyst view",
                stance=SectionStance.POSITIVE,
                label="Mostly buys",
                summary="Most covering analysts rate it a buy.",
                metrics=(SectionMetric("Consensus", "Buy"),),
            ),
        ),
        model="claude-opus-4-8",
        generated_at=datetime(2026, 6, 18, 20, 0, tzinfo=timezone.utc),
    )
    base.update(overrides)
    return StockScorecard(**base)


class FakeAnalysisCache(StockScorecardCacheAdapter):
    def __init__(self, stored: StockScorecard | None = None) -> None:
        self._store = {stored.symbol: stored} if stored is not None else {}
        self.puts: list[StockScorecard] = []

    def get(self, symbol: str) -> StockScorecard | None:
        return self._store.get(symbol)

    def put(self, analysis: StockScorecard) -> None:
        self.puts.append(analysis)
        self._store[analysis.symbol] = analysis


class FakeSectorAnalysisAdapter(SectorAnalysisAdapter):
    def __init__(
        self,
        analysis: SectorAnalysis | None = None,
        raises: Exception | None = None,
    ):
        self._analysis = analysis
        self._raises = raises
        self.received: list[SectorContext] | None = None

    def analyze(self, contexts) -> SectorAnalysis:
        self.received = list(contexts)
        if self._raises is not None:
            raise self._raises
        assert self._analysis is not None
        return self._analysis


def a_sector_analysis(**overrides) -> SectorAnalysis:
    base = dict(
        summary="Growth-sensitive corners led while defensives lagged.",
        tone=MarketTone.RISK_ON,
        leaders=(
            SectorHighlight("Technology", "XLK", 1.8, "Chipmakers powered the tape."),
        ),
        laggards=(
            SectorHighlight("Utilities", "XLU", -0.9, "Money left the safe corners."),
        ),
        model="claude-opus-4-8",
        generated_at=datetime(2026, 6, 18, 20, 0, tzinfo=timezone.utc),
    )
    base.update(overrides)
    return SectorAnalysis(**base)


class FakeMarketSummaryAdapter(MarketSummaryAdapter):
    def __init__(
        self,
        summary: MarketSummary | None = None,
        raises: Exception | None = None,
    ):
        self._summary = summary
        self._raises = raises
        self.received: list[MarketIndexPerformance] | None = None

    def analyze(self, indexes) -> MarketSummary:
        self.received = list(indexes)
        if self._raises is not None:
            raise self._raises
        assert self._summary is not None
        return self._summary


def a_market_summary(**overrides) -> MarketSummary:
    base = dict(
        summary="The US market has climbed over the past year, easing lately.",
        tone=MarketTone.RISK_ON,
        periods=(
            MarketPeriodHighlight(
                MarketPeriod.YEAR,
                "Both indexes are well up over the year.",
                (
                    MarketIndexReturn("S&P 500", "SPY", 18.4),
                    MarketIndexReturn("Nasdaq", "QQQ", 24.1),
                ),
            ),
            MarketPeriodHighlight(
                MarketPeriod.MONTH,
                "Modest gains over the past month.",
                (
                    MarketIndexReturn("S&P 500", "SPY", 2.1),
                    MarketIndexReturn("Nasdaq", "QQQ", 3.0),
                ),
            ),
            MarketPeriodHighlight(
                MarketPeriod.WEEK,
                "A slight pullback this week.",
                (
                    MarketIndexReturn("S&P 500", "SPY", -0.6),
                    MarketIndexReturn("Nasdaq", "QQQ", -0.9),
                ),
            ),
        ),
        model="claude-opus-4-8",
        generated_at=datetime(2026, 6, 18, 20, 0, tzinfo=timezone.utc),
    )
    base.update(overrides)
    return MarketSummary(**base)


def a_logo(content: bytes = b"\x89PNG\r\n", media_type: str = "image/png") -> Logo:
    return Logo(content=content, media_type=media_type)


def a_candle(**overrides) -> Candle:
    base = dict(
        timestamp=datetime(2026, 6, 18, tzinfo=timezone.utc),
        open=298.44, high=300.56, low=295.63, close=299.50, volume=1278873,
    )
    base.update(overrides)
    return Candle(**base)


def a_series(candles=None, timeframe: Timeframe = Timeframe.DAY_1) -> CandleSeries:
    if candles is None:
        candles = (a_candle(),)
    return CandleSeries(symbol="AAPL", timeframe=timeframe, candles=tuple(candles))


def a_rising_series(
    n: int = 4, start_close: float = 100.0, timeframe: Timeframe = Timeframe.DAY_1
) -> CandleSeries:
    base = datetime(2026, 6, 1, tzinfo=timezone.utc)
    candles = tuple(
        a_candle(close=start_close + i, timestamp=base + timedelta(days=i))
        for i in range(n)
    )
    return a_series(candles, timeframe=timeframe)


def a_support_series(timeframe: Timeframe = Timeframe.DAY_1) -> CandleSeries:
    lows = [5.0, 4.0, 3.0, 4.0, 5.0, 4.0, 3.0, 4.0, 5.0]
    base = datetime(2026, 6, 1, tzinfo=timezone.utc)
    candles = tuple(
        a_candle(
            open=low,
            high=low + 0.5,
            low=low,
            close=low,
            timestamp=base + timedelta(days=i),
        )
        for i, low in enumerate(lows)
    )
    return a_series(candles, timeframe=timeframe)


def a_stock(**overrides) -> Stock:
    base = dict(
        symbol="AAPL", name="Apple Inc.", exchange="NASDAQ", price=297.86,
        open=298.44, high=300.56, low=295.635, previous_close=296.07,
        volume=1278873, bid=283.52, ask=313.43,
        as_of=datetime(2026, 6, 18, 19, 59, 59, tzinfo=timezone.utc),
    )
    base.update(overrides)
    return Stock(**base)


def a_quote(**overrides) -> Quote:
    base = dict(
        symbol="AAPL", price=297.86, previous_close=296.07,
        bid=283.52, ask=313.43,
        as_of=datetime(2026, 6, 18, 19, 59, 59, tzinfo=timezone.utc),
    )
    base.update(overrides)
    return Quote(**base)


def a_performance(**overrides) -> StockPerformance:
    base = dict(
        one_week=1.2, one_month=-0.4, three_month=5.1,
        six_month=8.7, ytd=12.3, one_year=21.0,
    )
    base.update(overrides)
    return StockPerformance(**base)


def an_all_time_high(**overrides) -> AllTimeHigh:
    base = dict(
        price=350.0, reached_on=date(2026, 1, 5), since=date(2016, 1, 4),
    )
    base.update(overrides)
    return AllTimeHigh(**base)


def a_key_metrics(**overrides) -> KeyMetrics:
    base = dict(
        # Valuation / health / market (stay on the stock snapshot)
        pe=28.5, pb=45.2, ps=7.1, beta=1.2,
        fcf_per_share=6.43, roe=147.4,
        current_ratio=0.9, debt_to_equity=1.5,
        week_52_high=320.0, week_52_low=210.0,
        # Earnings-flavored (relocated to the earnings endpoint)
        eps=6.1, eps_growth_yoy=12.0, revenue_growth_yoy=8.0,
        gross_margin=44.0, operating_margin=30.0, net_margin=25.0,
    )
    base.update(overrides)
    return KeyMetrics(**base)


def an_estimates(**overrides) -> AnalystEstimates:
    base = dict(
        fiscal_year=2026, period_end=date(2026, 9, 30),
        eps_avg=8.0, revenue_avg=420_000_000_000.0,
        fiscal_year_fy2=2027, eps_avg_fy2=9.2, revenue_avg_fy2=455_000_000_000.0,
    )
    base.update(overrides)
    return AnalystEstimates(**base)


def a_sector(**overrides) -> SectorPerformance:
    base = dict(
        sector="Technology", symbol="XLK", price=255.0, previous_close=250.0,
        as_of=datetime(2026, 6, 18, 19, 59, 59, tzinfo=timezone.utc),
    )
    base.update(overrides)
    return SectorPerformance(**base)


def a_market_index(**overrides) -> MarketIndexPerformance:
    base = dict(
        name="S&P 500", symbol="SPY", price=550.0, previous_close=545.0,
        as_of=datetime(2026, 6, 18, 19, 59, 59, tzinfo=timezone.utc),
    )
    base.update(overrides)
    return MarketIndexPerformance(**base)


def a_quarter(**overrides) -> QuarterlyEarnings:
    base = dict(
        fiscal_year=2026, fiscal_quarter=1, period_end=date(2026, 3, 31),
        report_date=date(2026, 5, 1), eps_actual=2.18, eps_estimate=2.10,
        eps_surprise=0.08, eps_surprise_percent=3.81, revenue_estimate=None,
        revenue_actual=95_000_000_000.0,
    )
    base.update(overrides)
    return QuarterlyEarnings(**base)


def a_quarterly_timeline(quarters=None, symbol: str = "AAPL") -> QuarterlyEarningsTimeline:
    if quarters is None:
        quarters = (a_quarter(),)
    return QuarterlyEarningsTimeline(symbol=symbol, quarters=tuple(quarters))


def an_annual_year(**overrides) -> AnnualEarnings:
    base = dict(
        fiscal_year=2025, period_end=date(2025, 9, 30),
        eps_actual=6.10, eps_estimate=None,
        revenue_actual=400_000_000_000.0, revenue_estimate=None,
        net_income=100_000_000_000.0, eps_actual_consensus=6.50,
    )
    base.update(overrides)
    return AnnualEarnings(**base)


def an_annual_timeline(years=None, symbol: str = "AAPL") -> AnnualEarningsTimeline:
    if years is None:
        years = (
            an_annual_year(),  # a reported fiscal year
            AnnualEarnings(  # a forward (estimated) year
                fiscal_year=2026, period_end=date(2026, 9, 30),
                eps_actual=None, eps_estimate=8.0,
                revenue_actual=None, revenue_estimate=420_000_000_000.0,
                net_income=None, eps_actual_consensus=None,
            ),
        )
    return AnnualEarningsTimeline(symbol=symbol, years=tuple(years))


def an_analyst_recommendations(
    trends=None, symbol: str = "AAPL"
) -> AnalystRecommendations:
    if trends is None:
        trends = (
            # Newest first: a Buy consensus this month, a notch more bullish than
            # last month, so `direction` reads "upgraded".
            RecommendationTrend(
                period=date(2026, 6, 1),
                strong_buy=13, buy=24, hold=7, sell=0, strong_sell=0,
            ),
            RecommendationTrend(
                period=date(2026, 5, 1),
                strong_buy=10, buy=20, hold=12, sell=2, strong_sell=0,
            ),
        )
    return AnalystRecommendations(symbol=symbol, trends=tuple(trends))


# --------------------------- entity rules (pure) ---------------------------

def test_entity_change_and_percent():
    s = a_stock(price=110.0, previous_close=100.0)
    assert s.change == 10.0
    assert s.change_percent == 10.0


def test_entity_change_none_without_previous_close():
    s = a_stock(previous_close=None)
    assert s.change is None
    assert s.change_percent is None


def test_entity_change_percent_guards_zero_division():
    assert a_stock(previous_close=0).change_percent is None


def test_entity_spread():
    assert a_stock(bid=283.52, ask=313.43).spread == 29.91
    assert a_stock(bid=None).spread is None


def test_entity_drawdown_from_high_is_negative_below_the_high():
    s = a_stock(price=80.0, all_time_high=an_all_time_high(price=100.0))
    assert s.drawdown_from_high == -20.0  # 20% below the all-time high


def test_entity_drawdown_from_high_is_zero_at_the_high():
    s = a_stock(price=100.0, all_time_high=an_all_time_high(price=100.0))
    assert s.drawdown_from_high == 0.0


def test_entity_drawdown_from_high_none_without_a_high():
    assert a_stock(all_time_high=None).drawdown_from_high is None


def test_entity_drawdown_from_high_guards_zero_high():
    # A zero/missing high price can't anchor a percentage.
    assert a_stock(all_time_high=an_all_time_high(price=0.0)).drawdown_from_high is None


def test_quote_entity_mirrors_stock_change_rules():
    # Quote duplicates Stock's change/spread rules on purpose — they must agree.
    q = a_quote(price=110.0, previous_close=100.0)
    assert q.change == 10.0
    assert q.change_percent == 10.0
    assert q.spread == 29.91
    assert a_quote(previous_close=None).change is None
    assert a_quote(previous_close=0).change_percent is None
    assert a_quote(bid=None).spread is None


def test_market_session_at_buckets_by_eastern_time():
    # 2026-07-17 is a Friday; UTC runs 4h ahead of ET (EDT) that week.
    at = lambda h, m=0, d=17: datetime(2026, 7, d, h, m, tzinfo=timezone.utc)
    assert market_session_at(at(12)) is MarketSession.PRE_MARKET  # 08:00 ET
    assert market_session_at(at(18)) is MarketSession.REGULAR  # 14:00 ET
    assert market_session_at(at(20, 33)) is MarketSession.AFTER_HOURS  # 16:33 ET
    assert market_session_at(at(2)) is MarketSession.CLOSED  # 22:00 ET Thursday (overnight)
    # The boundaries are half-open: 09:30 opens regular, 16:00 opens after-hours.
    assert market_session_at(at(13, 30)) is MarketSession.REGULAR  # 09:30 ET sharp
    assert market_session_at(at(20)) is MarketSession.AFTER_HOURS  # 16:00 ET sharp
    # The weekend is closed regardless of time of day (Saturday the 18th).
    assert market_session_at(at(18, d=18)) is MarketSession.CLOSED
    # A naive timestamp is read as UTC (the feed stamps trades in UTC).
    assert market_session_at(datetime(2026, 7, 17, 20, 33)) is MarketSession.AFTER_HOURS


def test_quote_extended_hours_splits_an_after_hours_print():
    # 16:33 ET Friday: the latest print is an after-hours one, so the quote splits into
    # the regular close (the anchor) and the extended print measured against it.
    q = a_quote(
        price=333.75,
        previous_close=333.10,
        regular_close=333.23,
        as_of=datetime(2026, 7, 17, 20, 33, tzinfo=timezone.utc),
    )
    eh = q.extended_hours
    assert eh is not None
    assert eh.session is MarketSession.AFTER_HOURS
    assert eh.price == 333.75
    assert eh.regular_close == 333.23
    assert eh.change == 0.52  # the after-bell move: print vs the regular close
    assert eh.change_percent == 0.16
    # The day's move stays separate, off the regular close (not the extended print).
    assert q.regular_change == 0.13
    assert q.regular_change_percent == 0.04
    # And the top-level change is still the blended latest-print move (yesterday → after-hours).
    assert q.change == 0.65


def test_quote_extended_hours_none_during_the_regular_session():
    # A regular-session print carries no split — price/change already tell the story.
    q = a_quote(regular_close=297.9, as_of=datetime(2026, 7, 17, 18, tzinfo=timezone.utc))
    assert q.extended_hours is None


def test_quote_extended_hours_none_without_a_regular_close():
    # The Canadian (Yahoo) feed carries no regular close, so there's nothing to anchor an
    # extended split against — no block, even on an after-hours timestamp.
    q = a_quote(
        regular_close=None, as_of=datetime(2026, 7, 17, 20, 33, tzinfo=timezone.utc)
    )
    assert q.extended_hours is None
    assert q.regular_change is None
    assert q.regular_change_percent is None


def test_key_metrics_peg_divides_pe_by_growth():
    assert KeyMetrics(pe=30.0, eps_growth_yoy=15.0).peg == 2.0
    assert KeyMetrics(pe=28.5, eps_growth_yoy=10.0).peg == 2.85  # rounded to 2dp


@pytest.mark.parametrize(
    "pe, growth",
    [
        (30.0, None),   # growth unknown
        (None, 15.0),   # P/E unknown
        (30.0, 0.0),    # zero growth -> undefined
        (30.0, -5.0),   # shrinking earnings -> meaningless
        (-12.0, 15.0),  # negative P/E (losses) -> meaningless
    ],
)
def test_key_metrics_peg_none_when_inputs_missing_or_nonpositive(pe, growth):
    assert KeyMetrics(pe=pe, eps_growth_yoy=growth).peg is None


def test_candle_is_bullish():
    assert a_candle(open=100.0, close=110.0).is_bullish is True   # up -> green
    assert a_candle(open=110.0, close=100.0).is_bullish is False  # down -> red
    assert a_candle(open=100.0, close=100.0).is_bullish is True   # doji -> green


def test_analyst_estimates_forward_pe():
    est = an_estimates(eps_avg=8.0)
    assert est.forward_pe(280.0) == 35.0                          # 280 / 8.0
    assert est.forward_pe(None) is None                           # no price
    assert an_estimates(eps_avg=None).forward_pe(280.0) is None   # no estimate
    assert an_estimates(eps_avg=-1.0).forward_pe(280.0) is None   # expected loss


def test_analyst_estimates_forward_ps():
    est = an_estimates(revenue_avg=400e9)
    assert est.forward_ps(2_000e9) == 5.0                         # 2.0T / 400B
    assert est.forward_ps(None) is None
    assert an_estimates(revenue_avg=None).forward_ps(2_000e9) is None


def test_analyst_estimates_is_empty():
    assert an_estimates(eps_avg=None, revenue_avg=None).is_empty is True
    assert an_estimates(eps_avg=8.0).is_empty is False


def test_stock_forward_pe_and_ps_delegate_to_estimates():
    stock = a_stock(
        price=280.0, market_cap=2_000e9,
        analyst_estimates=an_estimates(eps_avg=8.0, revenue_avg=400e9),
    )
    assert stock.forward_pe == 35.0
    assert stock.forward_ps == 5.0


def test_stock_forward_pe_none_without_estimates():
    stock = a_stock()
    assert stock.forward_pe is None
    assert stock.forward_ps is None


def test_analyst_estimates_forward_growth():
    est = an_estimates()  # EPS 8.0→9.2, revenue 420→455 (B)
    assert est.forward_eps_growth() == 15.0       # 9.2/8.0 - 1, FY1→FY2
    assert est.forward_revenue_growth() == 8.33   # 455/420 - 1, FY1→FY2


def test_analyst_estimates_forward_growth_none_without_fy2():
    bare = AnalystEstimates(
        fiscal_year=2026, period_end=date(2026, 9, 30), eps_avg=8.0,
        revenue_avg=400e9,
    )
    assert bare.forward_eps_growth() is None      # only FY1, no FY2 to compare


def test_growth_metrics_build_combines_trailing_and_forward():
    g = GrowthMetrics.build(a_key_metrics(), an_estimates())
    assert g.revenue_yoy == 8.0     # trailing, from KeyMetrics (Finnhub TTM)
    assert g.eps_yoy == 12.0
    assert g.forward_eps_growth == 15.0       # forward FY1→FY2, from the estimates
    assert g.forward_revenue_growth == 8.33


def test_growth_metrics_build_trailing_only_without_estimates():
    g = GrowthMetrics.build(a_key_metrics(), None)
    assert g.revenue_yoy == 8.0
    assert g.forward_eps_growth is None


def test_growth_metrics_build_none_when_no_growth_anywhere():
    assert GrowthMetrics.build(None, None) is None
    assert GrowthMetrics.build(KeyMetrics(pe=20.0), None) is None  # no growth fields


def test_stock_growth_property():
    stock = a_stock(metrics=a_key_metrics(), analyst_estimates=an_estimates())
    assert stock.growth.eps_yoy == 12.0            # trailing
    assert stock.growth.forward_eps_growth == 15.0  # forward FY1→FY2
    assert a_stock().growth is None                # neither source attached


# --------------------------- chart window (range -> start/end) ---------------------------

def test_resolve_window_lookback():
    now = datetime(2026, 6, 21, 12, 0, tzinfo=timezone.utc)
    start, end = resolve_window(ChartRange.MONTH_1, now=now)
    assert end == now
    assert start == now - (now - start)  # sanity
    assert (now - start).days == 31


def test_resolve_window_max_has_no_start():
    now = datetime(2026, 6, 21, tzinfo=timezone.utc)
    start, end = resolve_window(ChartRange.MAX, now=now)
    assert start is None
    assert end == now


def test_resolve_window_ytd_starts_at_jan_1():
    now = datetime(2026, 6, 21, 12, 30, tzinfo=timezone.utc)
    start, end = resolve_window(ChartRange.YTD, now=now)
    assert start == datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert end == now


def test_use_case_normalizes_symbol():
    fake = FakeProvider(stock=a_stock())
    GetStockInfo(fake).execute("  aapl ")
    assert fake.received == ["AAPL"]


@pytest.mark.parametrize("bad", ["", "   ", "123", "AA1", "AA.B", "TOOLONG"])
def test_use_case_rejects_invalid_symbols(bad):
    fake = FakeProvider(stock=a_stock())
    with pytest.raises(ValueError):
        GetStockInfo(fake).execute(bad)
    assert fake.received == []  # provider untouched on invalid input


# --------------------------- normalize_symbol ---------------------------
# The guard every per-symbol read shares. The dashed forms below are all real rows the universe
# screen stores, so rejecting one 400s a ticker card the search list happily lists.


@pytest.mark.parametrize(
    "symbol",
    [
        "BRK-B",  # US share class
        "BRK-A",
        "TECK-B.TO",  # Canadian share class
        "RCI-A.TO",
        "CAR-UN.TO",  # Canadian REIT/trust unit
        "BEP-UN.TO",
        "U-UN.TO",  # single-letter root
        "FIH-U.TO",  # single-letter series
        "POW-PE.TO",  # preferred series
        "WFC-PC",
        "VITL-UN.TO",  # 4-letter root + 2-letter series
    ],
)
def test_normalize_symbol_accepts_class_and_series_lines(symbol):
    assert normalize_symbol(symbol) == symbol


def test_normalize_symbol_upper_cases_a_class_line():
    assert normalize_symbol("  brk-b ") == "BRK-B"


@pytest.mark.parametrize(
    "bad",
    [
        "BRK.B",  # Alpaca's dot convention — the anchor stores BRK-B; translating is an
        "AA.B",  # adapter's job, so the dot stays invalid here
        "AA-",  # dangling dash / empty series
        "-B",  # no root
        "A-B-C",  # two dashes
        "AA-1",  # non-letter series
        "AA-BCDE",  # over-long series
        "TOOLONG-B",  # over-long root
    ],
)
def test_normalize_symbol_still_rejects_junk(bad):
    with pytest.raises(ValueError):
        normalize_symbol(bad)


def test_use_case_propagates_not_found():
    fake = FakeProvider(raises=StockNotFound("ZZZZ"))
    with pytest.raises(StockNotFound):
        GetStockInfo(fake).execute("ZZZZ")


def test_use_case_merges_enrichment():
    # GetStockInfo layers on the best-effort enrichment it still owns — the trailing
    # performance windows and the forward analyst estimates. The trailing fundamentals
    # (market cap, dividend, metrics) are no longer read here; they're overlaid from the
    # ``stocks`` anchor downstream, so this snapshot leaves them unset.
    info = GetStockInfo(
        FakeProvider(stock=a_stock()),
        FakePerformanceProvider(a_performance()),
        estimates_provider=FakeEstimatesProvider(an_estimates()),
    )
    stock = info.execute("AAPL")
    assert stock.performance.one_year == 21.0
    assert stock.analyst_estimates.eps_avg == 8.0
    # Not read here anymore — filled from the anchor by the analysis overlay.
    assert stock.market_cap is None
    assert stock.dividend_per_share is None
    assert stock.metrics is None


def test_use_case_keeps_the_feed_name():
    # GetStockInfo leaves the price-feed's name as-is; the clean display name is overlaid
    # from the anchor downstream (the analysis path), not by this use case.
    stock = GetStockInfo(
        FakeProvider(stock=a_stock(name="Apple Inc. Common Stock"))
    ).execute("AAPL")
    assert stock.name == "Apple Inc. Common Stock"


def test_use_case_without_enrichment_leaves_fields_none():
    stock = GetStockInfo(FakeProvider(stock=a_stock())).execute("AAPL")
    assert stock.market_cap is None
    assert stock.dividend_yield is None
    assert stock.performance is None
    assert stock.metrics is None
    assert stock.analyst_estimates is None
    assert stock.forward_pe is None


def test_use_case_attaches_analyst_estimates():
    info = GetStockInfo(
        FakeProvider(stock=a_stock(price=280.0)),
        estimates_provider=FakeEstimatesProvider(an_estimates(eps_avg=8.0)),
    )
    stock = info.execute("AAPL")
    assert stock.analyst_estimates.eps_avg == 8.0
    assert stock.forward_pe == 35.0  # 280 / 8.0


def test_use_case_estimates_are_best_effort():
    info = GetStockInfo(
        FakeProvider(stock=a_stock()),
        estimates_provider=FakeEstimatesProvider(
            raises=StockDataUnavailable("AAPL", "boom")
        ),
    )
    stock = info.execute("AAPL")  # a miss must not raise
    assert stock.analyst_estimates is None
    assert stock.forward_pe is None


def test_use_case_drops_empty_estimates_block():
    # An uncovered symbol comes back as an all-None estimates -> omitted, not attached.
    empty = AnalystEstimates(
        fiscal_year=None, period_end=None, eps_avg=None, revenue_avg=None,
    )
    info = GetStockInfo(
        FakeProvider(stock=a_stock()),
        estimates_provider=FakeEstimatesProvider(empty),
    )
    assert info.execute("AAPL").analyst_estimates is None


def test_use_case_enrichment_is_best_effort():
    info = GetStockInfo(
        FakeProvider(stock=a_stock()),
        FakePerformanceProvider(raises=StockDataUnavailable("AAPL", "boom")),
        FakeAllTimeHighProvider(raises=StockDataUnavailable("AAPL", "boom")),
        FakeEstimatesProvider(raises=StockDataUnavailable("AAPL", "boom")),
    )
    stock = info.execute("AAPL")  # enrichment failures must not raise
    assert stock.price == 297.86
    assert stock.performance is None
    assert stock.market_cap is None  # never read here now
    assert stock.all_time_high is None
    assert stock.drawdown_from_high is None
    assert stock.analyst_estimates is None


def test_use_case_attaches_all_time_high():
    info = GetStockInfo(
        FakeProvider(stock=a_stock(price=297.86)),
        all_time_high_provider=FakeAllTimeHighProvider(an_all_time_high(price=350.0)),
    )
    stock = info.execute("AAPL")
    assert stock.all_time_high.price == 350.0
    assert stock.all_time_high.since == date(2016, 1, 4)
    assert stock.drawdown_from_high == -14.9  # ~15% off the high


def test_use_case_folds_live_price_into_a_fresh_all_time_high():
    # The history feed lags the live trade, so a stock printing a new high comes
    # back with a recorded peak *below* the current price. The use case folds the
    # live price in: the high becomes "now", and the drawdown reads 0 (not +).
    info = GetStockInfo(
        FakeProvider(stock=a_stock(price=297.86)),
        all_time_high_provider=FakeAllTimeHighProvider(
            an_all_time_high(price=290.0, reached_on=date(2025, 12, 1))
        ),
    )
    stock = info.execute("AAPL")
    assert stock.all_time_high.price == 297.86                  # the live price wins
    assert stock.all_time_high.reached_on == date(2026, 6, 18)  # as of the trade
    assert stock.all_time_high.since == date(2016, 1, 4)        # bound preserved
    assert stock.drawdown_from_high == 0.0


def test_use_case_all_time_high_none_without_provider():
    stock = GetStockInfo(FakeProvider(stock=a_stock())).execute("AAPL")
    assert stock.all_time_high is None
    assert stock.drawdown_from_high is None


def test_logo_use_case_normalizes_symbol():
    fake = FakeLogoProvider(logo=a_logo(content=b"PNG"))
    assert GetStockLogo(fake).execute("  aapl ").content == b"PNG"
    assert fake.received == ["AAPL"]


@pytest.mark.parametrize(
    "symbol,expected",
    [
        ("ry.to", "RY.TO"),  # Canadian TSX listing — the suffix is kept and upper-cased
        ("SHOP.TO", "SHOP.TO"),
        ("x.v", "X.V"),  # TSX Venture
        ("brk.b", "BRK.B"),  # US class share
        ("aapl", "AAPL"),  # a bare US ticker is unchanged
    ],
)
def test_logo_use_case_keeps_exchange_and_class_suffixes(symbol, expected):
    # Logo.dev is exchange-aware, so the suffix must reach it (T.TO is Telus, bare T is AT&T).
    fake = FakeLogoProvider(logo=a_logo(content=b"PNG"))
    GetStockLogo(fake).execute(symbol)
    assert fake.received == [expected]


@pytest.mark.parametrize(
    "bad", ["", "   ", "123", "AA1", "TOOLONG", "RY.", ".TO", "RY..TO", "A/B", "RY TO"]
)
def test_logo_use_case_rejects_invalid_symbols(bad):
    fake = FakeLogoProvider(logo=a_logo())
    with pytest.raises(ValueError):
        GetStockLogo(fake).execute(bad)
    assert fake.received == []  # provider untouched on invalid input


def test_candles_use_case_normalizes_symbol_and_forwards_window():
    fake = FakeCandleProvider(series=a_series())
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    end = datetime(2026, 6, 1, tzinfo=timezone.utc)
    GetStockCandles(fake).execute("  aapl ", Timeframe.HOUR_1, start=start, end=end)
    assert fake.received == [("AAPL", Timeframe.HOUR_1, start, end)]


@pytest.mark.parametrize("bad", ["", "   ", "123", "AA1", "AA.B", "TOOLONG"])
def test_candles_use_case_rejects_invalid_symbols(bad):
    fake = FakeCandleProvider(series=a_series())
    with pytest.raises(ValueError):
        GetStockCandles(fake).execute(bad, Timeframe.DAY_1)
    assert fake.received == []  # provider untouched on invalid input


def test_candles_use_case_rejects_inverted_window():
    fake = FakeCandleProvider(series=a_series())
    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    end = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with pytest.raises(ValueError):
        GetStockCandles(fake).execute("AAPL", Timeframe.DAY_1, start=start, end=end)
    assert fake.received == []


def test_candles_use_case_propagates_not_found():
    fake = FakeCandleProvider(raises=StockNotFound("ZZZZ"))
    with pytest.raises(StockNotFound):
        GetStockCandles(fake).execute("ZZZZ", Timeframe.DAY_1)


def test_ema_use_case_warms_up_before_the_window():
    fake = FakeCandleProvider(series=a_rising_series())
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    end = datetime(2026, 6, 1, tzinfo=timezone.utc)
    GetStockEma(fake).execute(
        "  aapl ", Timeframe.HOUR_1, periods=[2, 3], start=start, end=end
    )
    # Symbol normalized; the fetch reaches back a warmup before `start` (max
    # period 3 × 1-hour bars × the 3× factor = 9h) so the EMA is warm on screen.
    symbol, timeframe, fetch_start, fetch_end = fake.received[0]
    assert symbol == "AAPL"
    assert timeframe is Timeframe.HOUR_1
    assert fetch_end == end
    assert fetch_start == start - timedelta(hours=9)


def test_ema_use_case_trims_warmup_bars_to_the_visible_window():
    # Ten daily candles; the fake ignores the window and returns them all, so the
    # EMA is computed across the lot and then trimmed back to the visible start.
    base = datetime(2026, 6, 1, tzinfo=timezone.utc)
    candles = tuple(
        a_candle(close=100.0 + i, timestamp=base + timedelta(days=i)) for i in range(10)
    )
    fake = FakeCandleProvider(series=a_series(candles))
    visible_start = base + timedelta(days=5)
    result = GetStockEma(fake).execute(
        "AAPL",
        Timeframe.DAY_1,
        periods=[3],
        start=visible_start,
        end=base + timedelta(days=9),
    )
    points = result.lines[0].points
    assert points  # some survive the trim
    assert all(p.timestamp >= visible_start for p in points)  # warmup bars dropped


@pytest.mark.parametrize("bad", ["", "   ", "123", "AA1", "AA.B", "TOOLONG"])
def test_ema_use_case_rejects_invalid_symbols(bad):
    fake = FakeCandleProvider(series=a_rising_series())
    with pytest.raises(ValueError):
        GetStockEma(fake).execute(bad, Timeframe.DAY_1, periods=[20])
    assert fake.received == []  # provider untouched on invalid input


def test_ema_use_case_rejects_inverted_window():
    fake = FakeCandleProvider(series=a_rising_series())
    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    end = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with pytest.raises(ValueError):
        GetStockEma(fake).execute(
            "AAPL", Timeframe.DAY_1, periods=[20], start=start, end=end
        )
    assert fake.received == []


def test_ema_use_case_propagates_not_found():
    fake = FakeCandleProvider(raises=StockNotFound("ZZZZ"))
    with pytest.raises(StockNotFound):
        GetStockEma(fake).execute("ZZZZ", Timeframe.DAY_1, periods=[20])


def test_ema_use_case_computes_one_line_per_period():
    result = GetStockEma(FakeCandleProvider(series=a_rising_series())).execute(
        "AAPL", Timeframe.DAY_1, periods=[2, 3]
    )
    assert [line.period for line in result.lines] == [2, 3]


def test_support_levels_use_case_normalizes_symbol_and_forwards_window():
    fake = FakeCandleProvider(series=a_support_series())
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    end = datetime(2026, 6, 1, tzinfo=timezone.utc)
    GetStockSupportLevels(fake).execute(
        "  aapl ", Timeframe.HOUR_1, start=start, end=end
    )
    assert fake.received == [("AAPL", Timeframe.HOUR_1, start, end)]


@pytest.mark.parametrize("bad", ["", "   ", "123", "AA1", "AA.B", "TOOLONG"])
def test_support_levels_use_case_rejects_invalid_symbols(bad):
    fake = FakeCandleProvider(series=a_support_series())
    with pytest.raises(ValueError):
        GetStockSupportLevels(fake).execute(bad, Timeframe.DAY_1)
    assert fake.received == []  # provider untouched on invalid input


def test_support_levels_use_case_rejects_inverted_window():
    fake = FakeCandleProvider(series=a_support_series())
    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    end = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with pytest.raises(ValueError):
        GetStockSupportLevels(fake).execute(
            "AAPL", Timeframe.DAY_1, start=start, end=end
        )
    assert fake.received == []


def test_support_levels_use_case_propagates_not_found():
    fake = FakeCandleProvider(raises=StockNotFound("ZZZZ"))
    with pytest.raises(StockNotFound):
        GetStockSupportLevels(fake).execute("ZZZZ", Timeframe.DAY_1)


def test_support_levels_use_case_detects_from_fetched_candles():
    # Double-bottom at 3.0, series ending at 5.0 -> one moderate support level.
    result = GetStockSupportLevels(
        FakeCandleProvider(series=a_support_series())
    ).execute("AAPL", Timeframe.DAY_1, window=2)
    assert result.reference_price == 5.0
    assert [level.price for level in result.levels] == [3.0]
    assert result.levels[0].touches == 2
    assert result.levels[0].strength.value == "moderate"


def test_trend_use_case_normalizes_symbol_and_warms_up_before_the_window():
    fake = FakeCandleProvider(series=a_rising_series(n=20))
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    end = datetime(2026, 6, 1, tzinfo=timezone.utc)
    GetStockTrend(fake).execute(
        "  aapl ",
        Timeframe.HOUR_1,
        short_period=3,
        medium_period=5,
        long_period=8,
        start=start,
        end=end,
    )
    # Symbol normalized; the fetch reaches back a warmup before `start` (long
    # period 8 × 1-hour bars × the 3× factor = 24h) so the long EMA is warm.
    symbol, timeframe, fetch_start, fetch_end = fake.received[0]
    assert symbol == "AAPL"
    assert timeframe is Timeframe.HOUR_1
    assert fetch_end == end
    assert fetch_start == start - timedelta(hours=24)


@pytest.mark.parametrize("bad", ["", "   ", "123", "AA1", "AA.B", "TOOLONG"])
def test_trend_use_case_rejects_invalid_symbols(bad):
    fake = FakeCandleProvider(series=a_rising_series(n=20))
    with pytest.raises(ValueError):
        GetStockTrend(fake).execute(bad, Timeframe.DAY_1)
    assert fake.received == []  # provider untouched on invalid input


@pytest.mark.parametrize(
    "periods",
    [
        {"short_period": 1, "medium_period": 5, "long_period": 8},  # period < 2
        {"short_period": 50, "medium_period": 50, "long_period": 200},  # not increasing
        {"short_period": 20, "medium_period": 200, "long_period": 50},  # medium > long
        {"short_period": 60, "medium_period": 100, "long_period": 20},  # descending
    ],
)
def test_trend_use_case_rejects_bad_periods(periods):
    fake = FakeCandleProvider(series=a_rising_series(n=20))
    with pytest.raises(ValueError):
        GetStockTrend(fake).execute("AAPL", Timeframe.DAY_1, **periods)
    assert fake.received == []  # validated before any fetch


def test_trend_use_case_rejects_inverted_window():
    fake = FakeCandleProvider(series=a_rising_series(n=20))
    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    end = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with pytest.raises(ValueError):
        GetStockTrend(fake).execute("AAPL", Timeframe.DAY_1, start=start, end=end)
    assert fake.received == []


def test_trend_use_case_propagates_not_found():
    fake = FakeCandleProvider(raises=StockNotFound("ZZZZ"))
    with pytest.raises(StockNotFound):
        GetStockTrend(fake).execute("ZZZZ", Timeframe.DAY_1)


def test_trend_use_case_classifies_from_fetched_candles():
    # A strictly rising series -> all three horizons up and aligned -> strong uptrend.
    result = GetStockTrend(FakeCandleProvider(series=a_rising_series(n=20))).execute(
        "AAPL", Timeframe.DAY_1, short_period=3, medium_period=5, long_period=8
    )
    assert result.short_term.direction is TrendDirection.UP
    assert result.medium_term.direction is TrendDirection.UP
    assert result.long_term.direction is TrendDirection.UP
    assert result.reading is TrendReading.STRONG_UPTREND


def test_sector_entity_change_and_percent():
    s = a_sector(price=110.0, previous_close=100.0)
    assert s.change == 10.0
    assert s.change_percent == 10.0
    assert a_sector(previous_close=None).change_percent is None


def test_sector_use_case_ranks_best_performer_first():
    tech = a_sector(sector="Technology", price=110.0, previous_close=100.0)    # +10%
    energy = a_sector(sector="Energy", price=95.0, previous_close=100.0)       # -5%
    health = a_sector(sector="Health Care", price=102.0, previous_close=100.0) # +2%
    ranked = GetSectorPerformance(FakeSectorProvider([energy, health, tech])).execute()
    assert [s.sector for s in ranked] == ["Technology", "Health Care", "Energy"]


def test_sector_use_case_sorts_missing_percent_last():
    up = a_sector(sector="Technology", price=110.0, previous_close=100.0)  # +10%
    unknown = a_sector(sector="Energy", previous_close=None)              # no percent
    ranked = GetSectorPerformance(FakeSectorProvider([unknown, up])).execute()
    assert [s.sector for s in ranked] == ["Technology", "Energy"]


def test_sector_use_case_propagates_not_found():
    fake = FakeSectorProvider(raises=StockNotFound("sectors"))
    with pytest.raises(StockNotFound):
        GetSectorPerformance(fake).execute()


@pytest.fixture
def make_client():
    def _make(
        provider: StockDataProvider | None = None,
        logo_provider: LogoProvider | None = None,
        performance_provider: StockPerformanceProvider | None = None,
        ath_provider: AllTimeHighProvider | None = None,
        estimates_provider: AnalystEstimatesProvider | None = None,
        candle_provider: CandleProvider | None = None,
        ema_provider: CandleProvider | None = None,
        support_levels_provider: CandleProvider | None = None,
        trend_provider: CandleProvider | None = None,
        sector_provider: SectorPerformanceProvider | None = None,
        earnings_provider: QuarterlyEarningsProvider | None = None,
        annual_earnings_provider: AnnualEarningsProvider | None = None,
        recommendations_provider: RecommendationProvider | None = None,
        analysis_provider: StockScorecardAdapter | None = None,
        industry_repository: StockSearchRepository | None = None,
        sector_analysis_provider: SectorAnalysisAdapter | None = None,
        market_overview_provider: MarketOverviewProvider | None = None,
        market_summary_provider: MarketSummaryAdapter | None = None,
    ) -> TestClient:
        if logo_provider is not None:
            app.dependency_overrides[get_stock_logo] = lambda: GetStockLogo(logo_provider)
        if candle_provider is not None:
            app.dependency_overrides[get_stock_candles] = (
                lambda: GetStockCandles(candle_provider)
            )
        if ema_provider is not None:
            app.dependency_overrides[get_stock_ema] = lambda: GetStockEma(ema_provider)
        if support_levels_provider is not None:
            app.dependency_overrides[get_stock_support_levels] = (
                lambda: GetStockSupportLevels(support_levels_provider)
            )
        if trend_provider is not None:
            app.dependency_overrides[get_stock_trend] = (
                lambda: GetStockTrend(trend_provider)
            )
        if sector_provider is not None:
            app.dependency_overrides[get_sector_performance] = (
                lambda: GetSectorPerformance(sector_provider)
            )
        if analysis_provider is not None:
            app.dependency_overrides[get_stock_analysis] = lambda: GetStockAnalysis(
                GetStockInfo(
                    provider,
                    performance_provider,
                    ath_provider,
                    estimates_provider,
                ),
                analysis_provider,
                earnings_provider,
                annual_earnings_provider,
                recommendations_provider,
                industry_repository,
            )
        if sector_analysis_provider is not None:
            # Wire the sector-analysis use case with its board provider (the same
            # GetSectorPerformance the /sectors endpoint uses) and the analyzer.
            board = sector_provider or FakeSectorProvider([a_sector()])
            app.dependency_overrides[get_sector_analysis] = (
                lambda: GetSectorAnalysis(
                    GetSectorPerformance(board), sector_analysis_provider
                )
            )
        if market_summary_provider is not None:
            # Wire the market-summary use case with its index-board provider (the
            # same GetMarketOverview the endpoint uses) and the analyzer.
            overview = market_overview_provider or FakeMarketOverviewProvider(
                [a_market_index()]
            )
            app.dependency_overrides[get_market_summary] = (
                lambda: GetMarketSummary(
                    GetMarketOverview(overview), market_summary_provider
                )
            )
        return TestClient(app)

    yield _make
    app.dependency_overrides.clear()


# A stub Bedrock client so the real adapter's prompt-building and parse/translate
# logic runs offline — no anthropic package, no network. Attribute shapes match
# what the Anthropic SDK returns (message.content -> blocks with .type/.name/.input).
class _StubBlock:
    def __init__(self, type, name=None, input=None):
        self.type = type
        self.name = name
        self.input = input


class _StubMessage:
    def __init__(self, content):
        self.content = content


class _StubMessages:
    def __init__(self, message, recorder):
        self._message = message
        self._recorder = recorder

    def create(self, **kwargs):
        self._recorder.append(kwargs)
        return self._message


class _StubClient:
    def __init__(self, message):
        self.calls: list[dict] = []
        self.messages = _StubMessages(message, self.calls)


class _BoomMessages:
    def create(self, **kwargs):
        raise RuntimeError("bedrock exploded")


class _BoomClient:
    messages = _BoomMessages()


class _SeqStubMessages:
    def __init__(self, messages, recorder):
        self._messages = list(messages)
        self._recorder = recorder

    def create(self, **kwargs):
        self._recorder.append(kwargs)
        return self._messages[min(len(self._recorder) - 1, len(self._messages) - 1)]


class _SeqStubClient:
    def __init__(self, messages):
        self.calls: list[dict] = []
        self.messages = _SeqStubMessages(messages, self.calls)


# The adapter stubs derive their sections from the live registry, so adding a section
# to the scorecard doesn't break these fixtures (they always fill exactly what the
# forced tool requires).
_SCORECARD_SECTION_KEYS = tuple(s.key for s in _SCORECARD_SECTIONS)


def _section_payload(**overrides) -> dict:
    base = dict(stance="positive", label="Solid", summary="Reads well on balance.")
    base.update(overrides)
    return base


def _tool_message(**input_overrides) -> _StubMessage:
    # A complete scorecard: the overall verdict plus a filled read for every registry
    # section. `input_overrides` can replace the verdict or any section. (No confidence
    # — the service computes it from coverage, not the model.)
    payload = dict(recommendation="hold", thesis="Balanced.")
    payload.update({key: _section_payload() for key in _SCORECARD_SECTION_KEYS})
    payload.update(input_overrides)
    return _StubMessage([_StubBlock("tool_use", name="submit_scorecard", input=payload)])


def _blank_section() -> dict:
    # The fast-tier failure: a section returned with its words left empty (the metrics
    # are attached by the service regardless).
    return {"stance": "neutral", "label": "", "summary": ""}


def _blank_sections_message(**overrides) -> _StubMessage:
    # A scorecard whose overall verdict is filled but every section is blank — the miss
    # the targeted sections-only retry recovers from. `overrides` can fill a section (or
    # the verdict) back in.
    fields = {key: _blank_section() for key in _SCORECARD_SECTION_KEYS}
    fields.update(overrides)
    return _tool_message(**fields)


def _sections_recovery_message(**input_overrides) -> _StubMessage:
    # The lighter recovery tool the retry path forces — only the section reads.
    payload = {
        key: {"stance": "positive", "label": "Solid", "summary": "A clear read."}
        for key in _SCORECARD_SECTION_KEYS
    }
    payload.update(input_overrides)
    return _StubMessage([_StubBlock("tool_use", name="submit_sections", input=payload)])


def test_analysis_use_case_passes_stock_and_earnings():
    analyzer = FakeAnalysisProvider(an_analysis())
    info = GetStockInfo(FakeProvider(stock=a_stock()))
    use_case = GetStockAnalysis(
        info, analyzer, FakeQuarterlyEarningsProvider(a_quarterly_timeline())
    )
    analysis = use_case.execute("aapl")
    assert analysis.recommendation is Recommendation.HOLD
    assert analyzer.received == [("AAPL", True)]  # normalized symbol, earnings supplied


def test_analysis_served_from_fresh_cache_skips_generation():
    # A fresh stored read is returned verbatim — no snapshot gather, no model call.
    fresh = an_analysis(generated_at=datetime.now(timezone.utc))
    analyzer = FakeAnalysisProvider(an_analysis(thesis="regenerated"))
    info = GetStockInfo(FakeProvider(stock=a_stock()))
    cache = FakeAnalysisCache(stored=fresh)
    use_case = GetStockAnalysis(info, analyzer, cache=cache)
    result = use_case.execute("aapl")  # normalizes to AAPL, matching the cached key
    assert result is fresh
    assert analyzer.received == []  # model never called (nor the snapshot gather)
    assert cache.puts == []  # nothing re-stored


def test_analysis_stale_cache_is_regenerated_and_stored():
    # Past the TTL the stored read is stale: regenerate and overwrite it.
    stale = an_analysis(generated_at=datetime(2020, 1, 1, tzinfo=timezone.utc))
    generated = an_analysis(thesis="a fresh take")
    analyzer = FakeAnalysisProvider(generated)
    info = GetStockInfo(FakeProvider(stock=a_stock()))
    cache = FakeAnalysisCache(stored=stale)
    use_case = GetStockAnalysis(
        info, analyzer, cache=cache, cache_ttl=timedelta(minutes=30)
    )
    result = use_case.execute("AAPL")
    assert result is generated  # regenerated, not the stale read
    assert analyzer.received == [("AAPL", False)]
    assert cache.puts == [generated]  # stored for the next viewer


def test_analysis_cache_miss_generates_and_stores():
    generated = an_analysis()
    analyzer = FakeAnalysisProvider(generated)
    info = GetStockInfo(FakeProvider(stock=a_stock()))
    cache = FakeAnalysisCache()  # empty
    use_case = GetStockAnalysis(info, analyzer, cache=cache)
    result = use_case.execute("AAPL")
    assert result is generated
    assert cache.puts == [generated]


def test_analysis_incomplete_read_is_not_cached():
    # A model read with a blank section summary is returned to the caller but never
    # frozen in the cache, so the next view regenerates instead of serving an empty
    # section for the whole TTL.
    incomplete = an_analysis(
        sections=(a_section(summary=""),)  # a section with no summary -> not complete
    )
    analyzer = FakeAnalysisProvider(incomplete)
    info = GetStockInfo(FakeProvider(stock=a_stock()))
    cache = FakeAnalysisCache()  # empty
    result = GetStockAnalysis(info, analyzer, cache=cache).execute("AAPL")
    assert result is incomplete  # still returned to the caller
    assert cache.puts == []  # but not stored


# --- result cache: the newer AI analyses (earnings / sector / market) --------------
#
# Ratings + fundamentals cache scenarios live in their own test modules; here are the
# three that share this file's fakes. Each proves the generic AiAnalysisCacheAdapter wiring: a
# fresh stored read skips the gather + model call, a miss generates and stores, and an
# incomplete read is returned but not frozen. The two market-wide reads take no symbol,
# so they key on the "_MARKET_" sentinel.

_MARKET_KEY = "_MARKET_"


class FakeAiAnalysisCacheAdapter(AiAnalysisCacheAdapter):
    def __init__(self, stored=None, key=None):
        self._store = {key: stored} if stored is not None else {}
        self.puts: list[tuple] = []

    def get(self, key):
        return self._store.get(key)

    def put(self, key, analysis):
        self.puts.append((key, analysis))
        self._store[key] = analysis


class FakeEarningsAnalysisAdapter(EarningsAnalysisAdapter):
    def __init__(self, result=None, *, raises=None):
        self._result = result
        self._raises = raises
        self.received: list[str] = []

    def analyze(self, symbol, quarterly=None, annual=None) -> EarningsAnalysis:
        self.received.append(symbol)
        if self._raises is not None:
            raise self._raises
        return self._result


def an_earnings_analysis(
    symbol="AAPL", *, summary="Earnings are accelerating.", highlights=("Beat streak",),
    when=None,
) -> EarningsAnalysis:
    return EarningsAnalysis(
        symbol=symbol, summary=summary, trend=EarningsTrend.ACCELERATING,
        highlights=highlights, model="m",
        generated_at=when or datetime(2026, 7, 1, tzinfo=timezone.utc),
    )


def _earnings_use_case(analyzer, cache):
    return GetEarningsAnalysis(
        analyzer,
        FakeQuarterlyEarningsProvider(a_quarterly_timeline()),
        FakeAnnualEarningsProvider(an_annual_timeline()),
        cache=cache,
    )


def test_earnings_analysis_fresh_cache_skips_generation():
    fresh = an_earnings_analysis(when=datetime.now(timezone.utc))
    analyzer = FakeEarningsAnalysisAdapter(raises=AssertionError("model must not run"))
    result = _earnings_use_case(
        analyzer, FakeAiAnalysisCacheAdapter(stored=fresh, key="AAPL")
    ).execute("aapl")  # normalizes to AAPL, matching the cached key
    assert result is fresh
    assert analyzer.received == []  # model never called


def test_earnings_analysis_cache_miss_generates_and_stores():
    generated = an_earnings_analysis()
    cache = FakeAiAnalysisCacheAdapter()
    result = _earnings_use_case(
        FakeEarningsAnalysisAdapter(result=generated), cache
    ).execute("AAPL")
    assert result is generated
    assert cache.puts == [("AAPL", generated)]


def test_earnings_analysis_incomplete_read_is_not_cached():
    incomplete = an_earnings_analysis(summary="", highlights=())  # not is_complete
    cache = FakeAiAnalysisCacheAdapter()
    result = _earnings_use_case(
        FakeEarningsAnalysisAdapter(result=incomplete), cache
    ).execute("AAPL")
    assert result is incomplete  # still returned
    assert cache.puts == []  # but not stored


def test_sector_analysis_fresh_cache_skips_generation():
    fresh = a_sector_analysis(generated_at=datetime.now(timezone.utc))
    analyzer = FakeSectorAnalysisAdapter(raises=AssertionError("model must not run"))
    board = FakeSectorProvider([a_sector()])
    cache = FakeAiAnalysisCacheAdapter(stored=fresh, key=_MARKET_KEY)
    result = GetSectorAnalysis(
        GetSectorPerformance(board), analyzer, cache=cache
    ).execute()
    assert result is fresh
    assert analyzer.received is None  # analyze never called
    assert board.calls == 0  # the board gather is skipped too


def test_sector_analysis_cache_miss_generates_and_stores():
    generated = a_sector_analysis()
    cache = FakeAiAnalysisCacheAdapter()
    result = GetSectorAnalysis(
        GetSectorPerformance(FakeSectorProvider([a_sector()])),
        FakeSectorAnalysisAdapter(generated),
        cache=cache,
    ).execute()
    assert result is generated
    assert cache.puts == [(_MARKET_KEY, generated)]


def test_market_summary_fresh_cache_skips_generation():
    fresh = a_market_summary(generated_at=datetime.now(timezone.utc))
    analyzer = FakeMarketSummaryAdapter(raises=AssertionError("model must not run"))
    board = FakeMarketOverviewProvider([a_market_index()])
    cache = FakeAiAnalysisCacheAdapter(stored=fresh, key=_MARKET_KEY)
    result = GetMarketSummary(
        GetMarketOverview(board), analyzer, cache=cache
    ).execute()
    assert result is fresh
    assert analyzer.received is None  # analyze never called
    assert board.calls == 0  # the board gather is skipped too


def test_market_summary_cache_miss_generates_and_stores():
    generated = a_market_summary()
    cache = FakeAiAnalysisCacheAdapter()
    result = GetMarketSummary(
        GetMarketOverview(FakeMarketOverviewProvider([a_market_index()])),
        FakeMarketSummaryAdapter(generated),
        cache=cache,
    ).execute()
    assert result is generated
    assert cache.puts == [(_MARKET_KEY, generated)]


def test_analysis_ttl_defaults_reflect_how_often_each_input_changes(monkeypatch):
    # Each kind's default is tuned to its input's change cadence — slow per-symbol
    # fundamentals data caches for hours; the intraday market board stays short.
    for var in (
        "ANALYSIS_CACHE_TTL_MINUTES", "ANALYSIS_CACHE_TTL_MINUTES_EARNINGS",
        "ANALYSIS_CACHE_TTL_MINUTES_SECTOR",
    ):
        monkeypatch.delenv(var, raising=False)
    assert analysis_cache_ttl("earnings") == timedelta(minutes=720)   # ~quarterly data
    assert analysis_cache_ttl("ratings") == timedelta(minutes=360)
    assert analysis_cache_ttl("etf") == timedelta(minutes=360)
    assert analysis_cache_ttl("stock") == timedelta(minutes=240)
    assert analysis_cache_ttl("fundamentals") == timedelta(minutes=240)
    assert analysis_cache_ttl("sector") == timedelta(minutes=30)      # intraday board
    assert analysis_cache_ttl("market") == timedelta(minutes=60)
    assert analysis_cache_ttl("nonesuch") == timedelta(minutes=30)    # unknown -> fallback


def test_analysis_ttl_per_kind_env_override_wins(monkeypatch):
    monkeypatch.delenv("ANALYSIS_CACHE_TTL_MINUTES", raising=False)
    monkeypatch.setenv("ANALYSIS_CACHE_TTL_MINUTES_EARNINGS", "90")
    assert analysis_cache_ttl("earnings") == timedelta(minutes=90)
    assert analysis_cache_ttl("ratings") == timedelta(minutes=360)  # others unaffected
    # A garbage per-kind value is skipped, falling through to the kind's default.
    monkeypatch.setenv("ANALYSIS_CACHE_TTL_MINUTES_EARNINGS", "later")
    assert analysis_cache_ttl("earnings") == timedelta(minutes=720)


def test_analysis_ttl_global_override_pins_every_kind(monkeypatch):
    # The global var (no per-kind override set) pins all kinds to one value.
    monkeypatch.delenv("ANALYSIS_CACHE_TTL_MINUTES_EARNINGS", raising=False)
    monkeypatch.setenv("ANALYSIS_CACHE_TTL_MINUTES", "45")
    assert analysis_cache_ttl("earnings") == timedelta(minutes=45)
    assert analysis_cache_ttl("sector") == timedelta(minutes=45)
    # ...but a per-kind override still beats the global.
    monkeypatch.setenv("ANALYSIS_CACHE_TTL_MINUTES_SECTOR", "10")
    assert analysis_cache_ttl("sector") == timedelta(minutes=10)
    assert analysis_cache_ttl("earnings") == timedelta(minutes=45)


def test_stock_info_gathers_the_enrichment_calls_concurrently():
    # Deterministic proof the two independent enrichment reads run in parallel, not in
    # series: each waits on a 2-party barrier. If they truly overlap, both arrive and the
    # barrier releases; a regression to serial gather would leave the first one blocked
    # until the barrier's timeout trips (BrokenBarrierError), failing this test loudly
    # rather than merely running slower. (The trailing fundamentals/profile are no longer
    # gathered here — they're overlaid from the anchor — so the pool now fans out over the
    # performance + all-time-high reads only; estimates stays on the calling thread.)
    import threading

    barrier = threading.Barrier(2)

    class _ConcurrentFake:
        def get_stock(self, symbol):
            return a_stock()

        def get_performance(self, symbol):
            barrier.wait(timeout=5)
            return None

        def get_all_time_high(self, symbol):
            barrier.wait(timeout=5)
            raise StockNotFound(symbol)  # caught in the use case -> None

    fake = _ConcurrentFake()
    info = GetStockInfo(fake, fake, fake)  # provider + performance + all-time-high

    stock = info.execute("AAPL")  # returns only if both rendezvous -> concurrent

    assert stock.symbol == "AAPL"


def test_analysis_use_case_earnings_is_best_effort():
    analyzer = FakeAnalysisProvider(an_analysis())
    info = GetStockInfo(FakeProvider(stock=a_stock()))
    use_case = GetStockAnalysis(
        info,
        analyzer,
        FakeQuarterlyEarningsProvider(raises=StockDataUnavailable("AAPL", "boom")),
    )
    analysis = use_case.execute("AAPL")  # earnings failure must not sink the analysis
    assert analysis.recommendation is Recommendation.HOLD
    assert analyzer.received == [("AAPL", False)]  # earnings omitted


def test_analysis_use_case_omits_an_empty_timeline():
    # An uncovered symbol yields an empty timeline, not an error — the analyzer
    # should see "no earnings context", not an empty shell.
    analyzer = FakeAnalysisProvider(an_analysis())
    info = GetStockInfo(FakeProvider(stock=a_stock()))
    use_case = GetStockAnalysis(
        info, analyzer, FakeQuarterlyEarningsProvider(a_quarterly_timeline(quarters=()))
    )
    use_case.execute("AAPL")
    assert analyzer.received == [("AAPL", False)]


def test_analysis_use_case_passes_annual_and_recommendations():
    # The annual timeline and the analyst recommendations reach the analyzer as
    # best-effort context alongside the quarterly timeline.
    analyzer = FakeAnalysisProvider(an_analysis())
    info = GetStockInfo(FakeProvider(stock=a_stock()))
    use_case = GetStockAnalysis(
        info,
        analyzer,
        FakeQuarterlyEarningsProvider(a_quarterly_timeline()),
        FakeAnnualEarningsProvider(an_annual_timeline()),
        FakeRecommendationProvider(an_analyst_recommendations()),
    )
    use_case.execute("aapl")
    assert analyzer.last_quarterly is not None
    assert analyzer.last_annual is not None
    assert analyzer.last_recommendations is not None


def test_analysis_use_case_annual_and_recommendations_are_best_effort():
    # A failing annual fetch and a failing recommendations read must not sink the
    # analysis — both degrade to omitted context, like the quarterly timeline.
    analyzer = FakeAnalysisProvider(an_analysis())
    info = GetStockInfo(FakeProvider(stock=a_stock()))
    use_case = GetStockAnalysis(
        info,
        analyzer,
        FakeQuarterlyEarningsProvider(a_quarterly_timeline()),
        FakeAnnualEarningsProvider(raises=StockDataUnavailable("AAPL", "boom")),
        FakeRecommendationProvider(raises=StockDataUnavailable("AAPL", "boom")),
    )
    analysis = use_case.execute("AAPL")
    assert analysis.recommendation is Recommendation.HOLD
    assert analyzer.last_annual is None
    assert analyzer.last_recommendations is None


def test_analysis_use_case_omits_empty_recommendations():
    # An uncovered symbol yields an empty run, not an error — omitted, the same
    # stance as the earnings timelines.
    analyzer = FakeAnalysisProvider(an_analysis())
    info = GetStockInfo(FakeProvider(stock=a_stock()))
    use_case = GetStockAnalysis(
        info,
        analyzer,
        recommendations_provider=FakeRecommendationProvider(
            an_analyst_recommendations(trends=())
        ),
    )
    use_case.execute("AAPL")
    assert analyzer.last_recommendations is None


def test_analysis_use_case_omits_an_empty_annual_timeline():
    # An uncovered symbol yields an empty annual timeline, not an error — omitted,
    # the same stance as the quarterly timeline.
    analyzer = FakeAnalysisProvider(an_analysis())
    info = GetStockInfo(FakeProvider(stock=a_stock()))
    use_case = GetStockAnalysis(
        info,
        analyzer,
        None,
        FakeAnnualEarningsProvider(an_annual_timeline(years=())),
    )
    use_case.execute("AAPL")
    assert analyzer.last_annual is None


def test_analysis_use_case_passes_industry_valuation():
    # The ticker's industry benchmark reaches the analyzer: the repo resolves the
    # industry, its peers' P/Es are summarized into the entity, and it's handed on.
    # Five peers — the smallest sample the representativeness gate lets through.
    analyzer = FakeAnalysisProvider(an_analysis())
    info = GetStockInfo(FakeProvider(stock=a_stock()))
    use_case = GetStockAnalysis(
        info,
        analyzer,
        industry_repository=FakeSearchRepo(
            industry="semiconductors", pe_ratios=(10.0, 20.0, 30.0, 40.0, 50.0)
        ),
    )
    use_case.execute("aapl")
    valuation = analyzer.last_industry_valuation
    assert valuation is not None
    assert valuation.industry == "semiconductors"
    assert valuation.count == 5
    assert valuation.median_pe == 30.0  # median of the five peers


def test_analysis_use_case_scopes_valuation_to_the_stocks_tier():
    # The benchmark handed to the model is scoped to the stock's own cap tier: a mega-cap's
    # three mega peers are thin, so the cohort widens to include the large-caps (a
    # representative sample) but leaves the mid-caps out — a like-for-like comparison, and the
    # cohort label says so.
    analyzer = FakeAnalysisProvider(an_analysis())
    info = GetStockInfo(FakeProvider(stock=a_stock()))
    peers = (
        (40.0, MarketCapTier.MEGA),
        (42.0, MarketCapTier.MEGA),
        (44.0, MarketCapTier.MEGA),
        (20.0, MarketCapTier.LARGE),
        (22.0, MarketCapTier.LARGE),
        (24.0, MarketCapTier.LARGE),
        (26.0, MarketCapTier.LARGE),
        (28.0, MarketCapTier.LARGE),
        (8.0, MarketCapTier.MID),
        (9.0, MarketCapTier.MID),
    )
    use_case = GetStockAnalysis(
        info,
        analyzer,
        industry_repository=FakeSearchRepo(
            industry="semiconductors", peers=peers, anchor_tier=MarketCapTier.MEGA
        ),
    )
    use_case.execute("nvda")
    valuation = analyzer.last_industry_valuation
    assert valuation is not None
    assert valuation.cohort == "large/mega"
    assert valuation.count == 8  # mega + large, the mid-caps excluded


def test_analysis_use_case_omits_thin_industry_valuation():
    # A benchmark under MIN_REPRESENTATIVE_PEERS (here 4 valued peers) is omitted:
    # a "median" over so few names describes those companies, not the industry, so
    # the model must not be handed it as a peer anchor. One below the entity's gate
    # — the boundary the previous test sits just above.
    analyzer = FakeAnalysisProvider(an_analysis())
    info = GetStockInfo(FakeProvider(stock=a_stock()))
    use_case = GetStockAnalysis(
        info,
        analyzer,
        industry_repository=FakeSearchRepo(
            industry="uranium", pe_ratios=(10.0, 20.0, 30.0, 40.0)
        ),
    )
    use_case.execute("AAPL")
    assert analyzer.last_industry_valuation is None


def test_analysis_use_case_omits_industry_valuation_when_unscreened():
    # A symbol with no industry on the anchor (unscreened/unclassified) yields no
    # benchmark rather than an empty shell.
    analyzer = FakeAnalysisProvider(an_analysis())
    info = GetStockInfo(FakeProvider(stock=a_stock()))
    use_case = GetStockAnalysis(
        info, analyzer, industry_repository=FakeSearchRepo(industry=None)
    )
    use_case.execute("AAPL")
    assert analyzer.last_industry_valuation is None


def test_analysis_use_case_omits_industry_valuation_when_no_valued_peers():
    # An industry no peer has a usable P/E in (count 0) is omitted — nothing to
    # anchor the comparison on.
    analyzer = FakeAnalysisProvider(an_analysis())
    info = GetStockInfo(FakeProvider(stock=a_stock()))
    use_case = GetStockAnalysis(
        info,
        analyzer,
        industry_repository=FakeSearchRepo(industry="biotech", pe_ratios=()),
    )
    use_case.execute("AAPL")
    assert analyzer.last_industry_valuation is None


def test_analysis_use_case_industry_valuation_is_best_effort():
    # A failing anchor read must not sink the analysis — it degrades to an omitted
    # benchmark, the same stance as the earnings/recommendations context.
    analyzer = FakeAnalysisProvider(an_analysis())
    info = GetStockInfo(FakeProvider(stock=a_stock()))
    use_case = GetStockAnalysis(
        info,
        analyzer,
        industry_repository=FakeSearchRepo(
            raises=StockDataUnavailable("AAPL", "db down")
        ),
    )
    analysis = use_case.execute("AAPL")
    assert analysis.recommendation is Recommendation.HOLD
    assert analyzer.last_industry_valuation is None


def test_analysis_use_case_sources_fcf_from_the_anchor():
    # FCF per share must come from the DB the annual slice materializes on the anchor — the
    # overlay builds the snapshot's metrics block from it, so the scorecard's cash read
    # matches the ticker card.
    analyzer = FakeAnalysisProvider(an_analysis())
    info = GetStockInfo(FakeProvider(stock=a_stock()))
    use_case = GetStockAnalysis(
        info, analyzer, industry_repository=FakeSearchRepo(fcf_per_share=9.99)
    )
    use_case.execute("AAPL")
    assert analyzer.last_stock.metrics.fcf_per_share == 9.99  # from the anchor


def test_analysis_use_case_fcf_is_none_when_anchor_unsynced():
    # The DB is the only source: an unsynced anchor (no stored fcf) overwrites any snapshot
    # value to None rather than leaving a stale figure.
    analyzer = FakeAnalysisProvider(an_analysis())
    info = GetStockInfo(
        FakeProvider(stock=a_stock(metrics=a_key_metrics(fcf_per_share=6.43)))
    )
    use_case = GetStockAnalysis(
        info, analyzer, industry_repository=FakeSearchRepo(fcf_per_share=None)
    )
    use_case.execute("AAPL")
    assert analyzer.last_stock.metrics.fcf_per_share is None  # overwritten, not 6.43


def test_analysis_use_case_sources_growth_from_the_anchor():
    # Trailing revenue/EPS growth, like FCF, comes from the DB the annual slice
    # materializes on the anchor (consensus basis), so the scorecard's Growth section
    # matches the rest of the app.
    analyzer = FakeAnalysisProvider(an_analysis())
    info = GetStockInfo(FakeProvider(stock=a_stock()))
    use_case = GetStockAnalysis(
        info,
        analyzer,
        industry_repository=FakeSearchRepo(
            revenue_growth_yoy=15.5, eps_growth_yoy=22.0
        ),
    )
    use_case.execute("AAPL")
    metrics = analyzer.last_stock.metrics
    assert metrics.revenue_growth_yoy == 15.5  # from the anchor
    assert metrics.eps_growth_yoy == 22.0


def test_analysis_use_case_growth_is_none_when_anchor_unsynced():
    # DB-only, same as FCF: an unsynced anchor overwrites the growth reads to None rather
    # than leaving stale snapshot values.
    analyzer = FakeAnalysisProvider(an_analysis())
    info = GetStockInfo(
        FakeProvider(
            stock=a_stock(
                metrics=a_key_metrics(revenue_growth_yoy=8.0, eps_growth_yoy=12.0)
            )
        )
    )
    use_case = GetStockAnalysis(info, analyzer, industry_repository=FakeSearchRepo())
    use_case.execute("AAPL")
    metrics = analyzer.last_stock.metrics
    assert metrics.revenue_growth_yoy is None  # overwritten, not 8.0
    assert metrics.eps_growth_yoy is None  # overwritten, not 12.0


def test_analysis_use_case_prices_pe_on_the_consensus_basis():
    # The trailing P/E handed to the analyzer is recomputed on the consensus basis — the
    # live price over the quarterly slice's TTM EPS (the ticker card's figure) — so it sits
    # on the same basis as the industry-median P/E it's compared against. Four reported
    # quarters of 2.0 -> TTM 8.0; price 200 -> P/E 25.0.
    analyzer = FakeAnalysisProvider(an_analysis())
    info = GetStockInfo(FakeProvider(stock=a_stock(price=200.0)))
    quarters = tuple(
        a_quarter(fiscal_year=2025, fiscal_quarter=q, eps_actual=2.0) for q in (1, 2, 3, 4)
    )
    use_case = GetStockAnalysis(
        info,
        analyzer,
        quarterly_provider=FakeQuarterlyEarningsProvider(a_quarterly_timeline(quarters)),
        industry_repository=FakeSearchRepo(),
    )
    use_case.execute("AAPL")
    assert analyzer.last_stock.metrics.pe == 25.0  # 200 / 8.0


def test_analysis_use_case_pe_is_none_without_four_cached_quarters():
    # No consensus P/E is derivable without a full trailing year, so any snapshot P/E is
    # overwritten to None — the same DB-only stance as FCF/growth.
    analyzer = FakeAnalysisProvider(an_analysis())
    info = GetStockInfo(
        FakeProvider(stock=a_stock(price=200.0, metrics=a_key_metrics(pe=28.5)))
    )
    use_case = GetStockAnalysis(
        info,
        analyzer,
        quarterly_provider=FakeQuarterlyEarningsProvider(
            a_quarterly_timeline((a_quarter(eps_actual=2.0),))  # one quarter -> no TTM
        ),
        industry_repository=FakeSearchRepo(),
    )
    use_case.execute("AAPL")
    assert analyzer.last_stock.metrics.pe is None  # overwritten, not 28.5


def test_analysis_use_case_sources_margins_from_the_anchor():
    # The trailing margins fill off the anchor (the fundamentals slice's write), overlaid
    # onto the snapshot's metrics block so the scorecard's Business-quality section reads
    # the same figures the ticker card shows.
    analyzer = FakeAnalysisProvider(an_analysis())
    info = GetStockInfo(FakeProvider(stock=a_stock()))
    use_case = GetStockAnalysis(
        info,
        analyzer,
        industry_repository=FakeSearchRepo(
            gross_margin=44.0, operating_margin=30.0, net_margin=25.0
        ),
    )
    use_case.execute("AAPL")
    metrics = analyzer.last_stock.metrics
    assert metrics.gross_margin == 44.0  # from the anchor
    assert metrics.operating_margin == 30.0
    assert metrics.net_margin == 25.0


def test_analysis_use_case_sources_market_cap_and_dividend_from_the_anchor():
    # Market cap and the dividend (per share + a live-priced yield) fill off the anchor —
    # a $2 dividend at a $200 quote is a 1.0% yield.
    analyzer = FakeAnalysisProvider(an_analysis())
    info = GetStockInfo(FakeProvider(stock=a_stock(price=200.0)))
    use_case = GetStockAnalysis(
        info,
        analyzer,
        industry_repository=FakeSearchRepo(
            market_cap=2_500_000_000_000.0, dividend_per_share=2.0
        ),
    )
    use_case.execute("AAPL")
    stock = analyzer.last_stock
    assert stock.market_cap == 2_500_000_000_000.0  # from the anchor
    assert stock.dividend_per_share == 2.0
    assert stock.dividend_yield == 1.0  # 2.0 / 200 * 100, priced on the live quote


def test_analysis_use_case_sources_clean_name_from_the_anchor():
    # The anchor's clean display name replaces the price feed's fuller legal title.
    analyzer = FakeAnalysisProvider(an_analysis())
    info = GetStockInfo(FakeProvider(stock=a_stock(name="Apple Inc. Common Stock")))
    use_case = GetStockAnalysis(
        info, analyzer, industry_repository=FakeSearchRepo(name="Apple Inc.")
    )
    use_case.execute("AAPL")
    assert analyzer.last_stock.name == "Apple Inc."  # from the anchor


def test_analysis_use_case_propagates_stock_not_found():
    analyzer = FakeAnalysisProvider(an_analysis())
    info = GetStockInfo(FakeProvider(raises=StockNotFound("ZZZZ")))
    with pytest.raises(StockNotFound):
        GetStockAnalysis(info, analyzer).execute("ZZZZ")
    assert analyzer.received == []  # analyzer never called when the snapshot fails


def test_analysis_use_case_propagates_model_failure():
    analyzer = FakeAnalysisProvider(raises=StockDataUnavailable("AAPL", "model down"))
    info = GetStockInfo(FakeProvider(stock=a_stock()))
    with pytest.raises(StockDataUnavailable):
        GetStockAnalysis(info, analyzer).execute("AAPL")


def test_bedrock_adapter_parses_tool_call_into_entity():
    client = _StubClient(
        _tool_message(
            valuation={
                "stance": "negative",
                "label": "Expensive",
                "summary": "Priced richly.",
            }
        )
    )
    provider = BedrockStockScorecardAdapter(client=client, model_id="test-model")
    scorecard = provider.analyze(
        a_stock(metrics=a_key_metrics()), a_quarterly_timeline()
    )
    assert scorecard.symbol == "AAPL"
    assert scorecard.recommendation is Recommendation.HOLD
    # Every registry section comes back, in card order, each carrying the model's read.
    assert [s.key for s in scorecard.sections] == [s.key for s in _SCORECARD_SECTIONS]
    valuation = next(s for s in scorecard.sections if s.key == "valuation")
    assert valuation.stance is SectionStance.NEGATIVE
    assert valuation.label == "Expensive"
    assert valuation.summary  # a plain-language read is present
    # The supporting chips are attached from the gathered data, not the model — the
    # trailing P/E rides the KeyMetrics figure.
    assert any(m.label == "P/E (trailing)" for m in valuation.metrics)
    assert scorecard.model == "test-model"
    # The model was actually pinned to the forced tool, with our model id.
    assert client.calls[0]["tool_choice"] == {
        "type": "tool",
        "name": "submit_scorecard",
    }
    assert client.calls[0]["model"] == "test-model"


def test_bedrock_adapter_renders_figures_into_prompt():
    client = _StubClient(_tool_message())
    BedrockStockScorecardAdapter(client=client).analyze(
        a_stock(metrics=a_key_metrics()), a_quarterly_timeline()
    )
    prompt = client.calls[0]["messages"][0]["content"]
    assert "Stock: AAPL" in prompt
    assert "P/E (trailing): 28.50" in prompt  # a metric rendered from KeyMetrics
    assert "FCF/share (trailing): 6.43" in prompt  # ROE/FCF per share ride along too
    assert "ROE %: 147.40" in prompt
    assert "Recent quarterly earnings" in prompt  # the beat history was included


def test_bedrock_adapter_renders_forward_recommendations_and_annual_into_prompt():
    # The richer context — forward consensus (from estimates), the analyst
    # recommendations, and the annual timeline — each renders into its own section.
    client = _StubClient(_tool_message())
    BedrockStockScorecardAdapter(client=client).analyze(
        a_stock(metrics=a_key_metrics(), analyst_estimates=an_estimates()),
        a_quarterly_timeline(),
        an_annual_timeline(),
        an_analyst_recommendations(),
    )
    prompt = client.calls[0]["messages"][0]["content"]
    assert "Forward P/E (consensus): 37.23" in prompt  # price / FY1 consensus EPS
    assert "Expected EPS growth next year %: 15.00" in prompt  # FY1 -> FY2
    assert "Analyst recommendations" in prompt
    assert "Consensus: Buy" in prompt  # the consensus label from the trend
    assert "upgraded" in prompt  # the month-over-month direction
    assert "Annual earnings (fiscal years):" in prompt
    assert "FY2025 reported" in prompt
    assert "FY2026 estimated" in prompt


def test_bedrock_adapter_renders_industry_valuation_into_prompt():
    # The industry benchmark renders its own labelled block, so the model can weigh
    # the stock's own trailing P/E against its peers.
    client = _StubClient(_tool_message())
    BedrockStockScorecardAdapter(client=client).analyze(
        a_stock(metrics=a_key_metrics()),
        a_quarterly_timeline(),
        industry_valuation=IndustryValuation.from_pe_ratios(
            "semiconductors", (10.0, 20.0, 30.0, 40.0)
        ),
    )
    prompt = client.calls[0]["messages"][0]["content"]
    assert "Industry valuation benchmark" in prompt
    assert "Industry: semiconductors" in prompt
    assert "Peer group: industry" in prompt  # whole-industry benchmark
    assert "in the same industry" in prompt
    assert "Median P/E: 25.00" in prompt  # interpolated median of the four peers
    assert "25th-75th percentile): 17.50 to 32.50" in prompt


def test_bedrock_adapter_renders_a_tier_scoped_cohort_as_same_size_peers():
    # A benchmark scoped to the stock's own cap tier renders as a like-for-like block, so the
    # model reads a mega-cap median as same-size, not industry-wide.
    client = _StubClient(_tool_message())
    # Five mega peers (a representative same-tier cohort) alongside some mid-caps that stay
    # out — so the cohort is the mega slice, not the whole industry.
    peers = tuple((pe, MarketCapTier.MEGA) for pe in (20.0, 30.0, 40.0, 50.0, 60.0)) + (
        (8.0, MarketCapTier.MID),
        (9.0, MarketCapTier.MID),
    )
    BedrockStockScorecardAdapter(client=client).analyze(
        a_stock(metrics=a_key_metrics()),
        a_quarterly_timeline(),
        industry_valuation=IndustryValuation.for_stock_peers(
            "semiconductors", MarketCapTier.MEGA, peers
        ),
    )
    prompt = client.calls[0]["messages"][0]["content"]
    assert "Peer group: mega" in prompt
    assert "of the same size (mega-cap) in the industry" in prompt


def test_bedrock_adapter_omits_industry_valuation_block_when_absent():
    # No benchmark supplied -> no block (the section is skipped, not rendered empty).
    client = _StubClient(_tool_message())
    BedrockStockScorecardAdapter(client=client).analyze(a_stock(metrics=a_key_metrics()))
    prompt = client.calls[0]["messages"][0]["content"]
    assert "Industry valuation benchmark" not in prompt


def test_bedrock_adapter_raises_when_no_tool_call():
    client = _StubClient(_StubMessage([_StubBlock("text")]))  # model didn't call it
    with pytest.raises(StockDataUnavailable):
        BedrockStockScorecardAdapter(client=client).analyze(a_stock())


def test_bedrock_adapter_maps_client_error_to_domain_error():
    with pytest.raises(StockDataUnavailable):
        BedrockStockScorecardAdapter(client=_BoomClient()).analyze(a_stock())


def test_bedrock_adapter_rejects_offschema_value():
    client = _StubClient(_tool_message(recommendation="mega_buy"))  # not in the enum
    with pytest.raises(StockDataUnavailable):
        BedrockStockScorecardAdapter(client=client).analyze(a_stock())


def test_bedrock_adapter_neutral_stance_when_section_stance_off_enum():
    # A section whose stance isn't a known value degrades to neutral rather than
    # sinking the whole scorecard (a cosmetic field, unlike the overall verdict).
    client = _StubClient(_tool_message(valuation=_section_payload(stance="wildly_off")))
    scorecard = BedrockStockScorecardAdapter(client=client).analyze(a_stock())
    valuation = next(s for s in scorecard.sections if s.key == "valuation")
    assert valuation.stance is SectionStance.NEUTRAL


def test_bedrock_adapter_confidence_reflects_data_coverage():
    # Confidence is the service's deterministic read of how many data sources resolved
    # (sections with real chips), not the model's guess. A full multi-source snapshot
    # reads HIGH, a fundamentals-only one MEDIUM, a bare quote LOW.
    full = BedrockStockScorecardAdapter(client=_StubClient(_tool_message())).analyze(
        a_stock(metrics=a_key_metrics()),
        a_quarterly_timeline(),
        recommendations=an_analyst_recommendations(),
    )
    assert full.confidence is Confidence.HIGH

    partial = BedrockStockScorecardAdapter(client=_StubClient(_tool_message())).analyze(
        a_stock(metrics=a_key_metrics())  # fundamentals only — no earnings/analyst
    )
    assert partial.confidence is Confidence.MEDIUM

    bare = BedrockStockScorecardAdapter(client=_StubClient(_tool_message())).analyze(
        a_stock()  # a quote with no fundamentals, earnings, or analyst coverage
    )
    assert bare.confidence is Confidence.LOW


def test_bedrock_adapter_recovers_blank_sections_with_targeted_retry():
    # The verdict comes back rich but the four sections blank (the SNDK failure); the
    # adapter re-issues a *sections-only* call and merges the recovered reads in, so the
    # served scorecard is complete (and cacheable) rather than showing empty sections.
    client = _SeqStubClient(
        [
            _blank_sections_message(),
            _sections_recovery_message(
                valuation={
                    "stance": "negative",
                    "label": "Expensive",
                    "summary": "Priced richly.",
                }
            ),
        ]
    )

    scorecard = BedrockStockScorecardAdapter(client=client).analyze(
        a_stock(metrics=a_key_metrics())
    )

    assert len(client.calls) == 2  # retried exactly once
    # the recovery is the lighter, sections-only forced tool, not the full scorecard
    assert client.calls[1]["tool_choice"] == {"type": "tool", "name": "submit_sections"}
    valuation = next(s for s in scorecard.sections if s.key == "valuation")
    assert valuation.label == "Expensive"
    assert valuation.summary  # the recovered read filled the blank
    assert scorecard.is_complete
    # The overall verdict from the first pass is preserved through the merge.
    assert scorecard.thesis == "Balanced."


def test_bedrock_adapter_merge_keeps_a_section_the_first_pass_already_wrote():
    # A first pass that filled one section but blanked the rest: the retry fills only the
    # blanks and never overwrites the good section.
    first = _blank_sections_message(
        profitability=_section_payload(label="Exceptional", summary="Best in class.")
    )
    client = _SeqStubClient([first, _sections_recovery_message()])

    scorecard = BedrockStockScorecardAdapter(client=client).analyze(a_stock())

    prof = next(s for s in scorecard.sections if s.key == "profitability")
    assert prof.label == "Exceptional"  # kept from the first pass, not the recovery
    assert scorecard.is_complete


def test_bedrock_adapter_accepts_blank_sections_after_exhausting_retries():
    # If every attempt comes back blank, keep the read (the verdict still lands) rather
    # than looping forever or raising — and the use case refuses to cache it, so the next
    # view regenerates.
    client = _SeqStubClient([_blank_sections_message()])  # repeats the blank message

    scorecard = BedrockStockScorecardAdapter(client=client).analyze(a_stock())

    # initial call + the bounded retries, then accept
    assert len(client.calls) == 1 + BedrockStockScorecardAdapter._MAX_INCOMPLETE_RETRIES
    assert not scorecard.is_complete
    assert scorecard.thesis  # the overall verdict still comes through


def test_bedrock_adapter_does_not_retry_a_complete_scorecard():
    # The happy path pays no retry cost: a first pass with all four sections filled is
    # returned after a single call.
    client = _SeqStubClient([_tool_message(), _sections_recovery_message()])

    scorecard = BedrockStockScorecardAdapter(client=client).analyze(a_stock())

    assert len(client.calls) == 1  # no recovery call
    assert scorecard.is_complete


def test_get_analysis_returns_200(make_client):
    client = make_client(
        provider=FakeProvider(stock=a_stock()),
        analysis_provider=FakeAnalysisProvider(an_analysis()),
    )
    r = client.get("/stocks/AAPL/analysis")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["symbol"] == "AAPL"
    assert body["recommendation"] == "hold"
    assert body["confidence"] == "medium"
    assert [s["key"] for s in body["sections"]] == [
        "business_quality",
        "valuation",
        "earnings",
        "analyst_view",
    ]
    valuation = body["sections"][1]
    assert valuation["stance"] == "negative"
    assert valuation["label"] == "Expensive"
    assert valuation["summary"]
    assert valuation["metrics"][0]["label"] == "P/E (trailing)"
    assert "not financial advice" in body["disclaimer"].lower()
    assert body["model"] == "claude-opus-4-8"


def test_get_analysis_normalizes_and_supplies_earnings(make_client):
    analyzer = FakeAnalysisProvider(an_analysis())
    client = make_client(
        provider=FakeProvider(stock=a_stock()),
        analysis_provider=analyzer,
        earnings_provider=FakeQuarterlyEarningsProvider(a_quarterly_timeline()),
    )
    assert client.get("/stocks/aapl/analysis").status_code == 200
    assert analyzer.received == [("AAPL", True)]


def test_get_analysis_supplies_annual_and_recommendations_context(make_client):
    # The endpoint wires the annual timeline and the analyst recommendations
    # through to the analyzer as context, alongside the quarterly timeline.
    analyzer = FakeAnalysisProvider(an_analysis())
    client = make_client(
        provider=FakeProvider(stock=a_stock()),
        analysis_provider=analyzer,
        earnings_provider=FakeQuarterlyEarningsProvider(a_quarterly_timeline()),
        annual_earnings_provider=FakeAnnualEarningsProvider(an_annual_timeline()),
        recommendations_provider=FakeRecommendationProvider(
            an_analyst_recommendations()
        ),
    )
    assert client.get("/stocks/AAPL/analysis").status_code == 200
    assert analyzer.last_annual is not None
    assert analyzer.last_recommendations is not None


def test_get_analysis_supplies_industry_valuation_context(make_client):
    # The endpoint wires the industry P/E benchmark through to the analyzer: the
    # ticker's industry is resolved and its peers summarized into the entity
    # (enough of them to clear the representativeness gate).
    analyzer = FakeAnalysisProvider(an_analysis())
    client = make_client(
        provider=FakeProvider(stock=a_stock()),
        analysis_provider=analyzer,
        industry_repository=FakeSearchRepo(
            industry="semiconductors", pe_ratios=(10.0, 20.0, 30.0, 40.0, 50.0)
        ),
    )
    assert client.get("/stocks/AAPL/analysis").status_code == 200
    valuation = analyzer.last_industry_valuation
    assert valuation is not None
    assert valuation.industry == "semiconductors"
    assert valuation.count == 5


def test_get_analysis_404_when_symbol_unknown(make_client):
    client = make_client(
        provider=FakeProvider(raises=StockNotFound("ZZZZ")),
        analysis_provider=FakeAnalysisProvider(an_analysis()),
    )
    assert client.get("/stocks/ZZZZ/analysis").status_code == 404


def test_get_analysis_502_when_model_fails(make_client):
    client = make_client(
        provider=FakeProvider(stock=a_stock()),
        analysis_provider=FakeAnalysisProvider(
            raises=StockDataUnavailable("AAPL", "bedrock timeout")
        ),
    )
    assert client.get("/stocks/AAPL/analysis").status_code == 502


def test_get_analysis_400_on_bad_symbol(make_client):
    client = make_client(
        provider=FakeProvider(stock=a_stock()),
        analysis_provider=FakeAnalysisProvider(an_analysis()),
    )
    assert client.get("/stocks/TOOLONG/analysis").status_code == 400


def test_get_logo_returns_png_bytes(make_client):
    client = make_client(logo_provider=FakeLogoProvider(a_logo(content=b"\x89PNG\r\n")))
    r = client.get("/stocks/AAPL/logo")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "image/png"
    assert r.content == b"\x89PNG\r\n"


def test_get_logo_passes_through_media_type(make_client):
    svg = a_logo(content=b"<svg/>", media_type="image/svg+xml")
    client = make_client(logo_provider=FakeLogoProvider(svg))
    r = client.get("/stocks/AAPL/logo")
    assert r.headers["content-type"] == "image/svg+xml"


def test_get_logo_serves_a_canadian_ticker_keeping_the_suffix(make_client):
    # A Canadian listing (RY.TO) reaches the provider with its suffix intact — Logo.dev is
    # exchange-aware, so the suffix is what fetches Royal Bank's logo (and disambiguates a
    # collision like T.TO / T). Before the fix the dotted symbol was rejected as a 400.
    fake = FakeLogoProvider(a_logo(content=b"\x89PNG\r\n"))
    client = make_client(logo_provider=fake)
    r = client.get("/stocks/RY.TO/logo")
    assert r.status_code == 200, r.text
    assert fake.received == ["RY.TO"]


def test_get_logo_invalid_symbol_400(make_client):
    client = make_client(logo_provider=FakeLogoProvider(a_logo()))
    assert client.get("/stocks/123/logo").status_code == 400


def test_get_logo_missing_404(make_client):
    client = make_client(logo_provider=FakeLogoProvider(raises=StockNotFound("ZZZZ")))
    assert client.get("/stocks/ZZZZ/logo").status_code == 404


def test_get_logo_upstream_failure_502(make_client):
    fake = FakeLogoProvider(raises=StockDataUnavailable("AAPL", "boom"))
    client = make_client(logo_provider=fake)
    assert client.get("/stocks/AAPL/logo").status_code == 502


def test_get_candles_returns_200_with_chart_shape(make_client):
    up = a_candle(open=100.0, close=110.0, timestamp=datetime(2026, 6, 18, tzinfo=timezone.utc))
    down = a_candle(open=110.0, close=105.0, timestamp=datetime(2026, 6, 19, tzinfo=timezone.utc))
    client = make_client(candle_provider=FakeCandleProvider(a_series((up, down))))
    r = client.get("/stocks/ticker/AAPL/candles")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["symbol"] == "AAPL"
    assert body["timeframe"] == "1Day"
    assert body["count"] == 2
    first = body["candles"][0]
    assert first["open"] == 100.0 and first["close"] == 110.0
    assert first["direction"] == "up"                     # green
    assert body["candles"][1]["direction"] == "down"      # red
    assert first["time"] == int(datetime(2026, 6, 18, tzinfo=timezone.utc).timestamp())


def test_get_candles_defaults_to_6m_daily(make_client):
    fake = FakeCandleProvider(a_series())
    client = make_client(candle_provider=fake)
    assert client.get("/stocks/ticker/AAPL/candles").status_code == 200
    symbol, timeframe, start, end = fake.received[0]
    assert symbol == "AAPL"
    assert timeframe is Timeframe.DAY_1                    # default timeframe
    assert start is not None and end is not None
    assert (end - start).days == 183                       # default range = 6M


def test_get_candles_honors_timeframe_and_range(make_client):
    fake = FakeCandleProvider(a_series(timeframe=Timeframe.HOUR_1))
    client = make_client(candle_provider=fake)
    r = client.get("/stocks/ticker/AAPL/candles", params={"timeframe": "1Hour", "range": "7D"})
    assert r.status_code == 200
    _, timeframe, start, end = fake.received[0]
    assert timeframe is Timeframe.HOUR_1
    assert (end - start).days == 7


def test_get_candles_explicit_window_overrides_range(make_client):
    fake = FakeCandleProvider(a_series())
    client = make_client(candle_provider=fake)
    r = client.get(
        "/stocks/ticker/AAPL/candles",
        params={"start": "2026-01-01T00:00:00Z", "end": "2026-02-01T00:00:00Z"},
    )
    assert r.status_code == 200
    _, _, start, end = fake.received[0]
    assert start == datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert end == datetime(2026, 2, 1, tzinfo=timezone.utc)


def test_get_candles_invalid_timeframe_422(make_client):
    client = make_client(candle_provider=FakeCandleProvider(a_series()))
    assert client.get("/stocks/ticker/AAPL/candles", params={"timeframe": "1Year"}).status_code == 422


def test_get_candles_invalid_symbol_400(make_client):
    client = make_client(candle_provider=FakeCandleProvider(a_series()))
    assert client.get("/stocks/ticker/123/candles").status_code == 400


def test_get_candles_inverted_window_400(make_client):
    client = make_client(candle_provider=FakeCandleProvider(a_series()))
    r = client.get(
        "/stocks/ticker/AAPL/candles",
        params={"start": "2026-02-01T00:00:00Z", "end": "2026-01-01T00:00:00Z"},
    )
    assert r.status_code == 400


def test_get_candles_unknown_symbol_404(make_client):
    client = make_client(candle_provider=FakeCandleProvider(raises=StockNotFound("ZZZZ")))
    assert client.get("/stocks/ticker/ZZZZ/candles").status_code == 404


def test_get_candles_upstream_failure_502(make_client):
    fake = FakeCandleProvider(raises=StockDataUnavailable("AAPL", "boom"))
    client = make_client(candle_provider=fake)
    assert client.get("/stocks/ticker/AAPL/candles").status_code == 502


def test_get_ema_returns_200_with_one_line_per_period(make_client):
    client = make_client(ema_provider=FakeCandleProvider(a_rising_series()))
    r = client.get("/stocks/ticker/AAPL/ema", params={"period": [2, 3]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["symbol"] == "AAPL"
    assert body["timeframe"] == "1Day"
    assert [line["period"] for line in body["lines"]] == [2, 3]
    line = body["lines"][0]
    assert line["count"] == 3                       # 4 candles, period 2
    assert line["latest"] is not None
    assert set(line["points"][0]) == {"time", "timestamp", "value"}


def test_get_ema_defaults_to_9_21_50_200_over_6m_daily(make_client):
    fake = FakeCandleProvider(a_series())           # a single candle
    client = make_client(ema_provider=fake)
    r = client.get("/stocks/ticker/AAPL/ema")
    assert r.status_code == 200, r.text
    symbol, timeframe, start, end = fake.received[0]
    assert symbol == "AAPL"
    assert timeframe is Timeframe.DAY_1
    # 6M visible window (183d) plus the EMA warmup reach-back before it (now sized to
    # the deepest default line, the 200-EMA, so a deeper reach-back).
    assert (end - start).days > 183
    body = r.json()
    assert [line["period"] for line in body["lines"]] == [9, 21, 50, 200]
    # One candle can't warm any of them: graceful empty lines, not an error.
    assert all(line["count"] == 0 and line["latest"] is None for line in body["lines"])


def test_get_ema_dedupes_periods_keeping_order(make_client):
    client = make_client(ema_provider=FakeCandleProvider(a_rising_series()))
    r = client.get("/stocks/ticker/AAPL/ema", params={"period": [50, 20, 50]})
    assert r.status_code == 200, r.text
    assert [line["period"] for line in r.json()["lines"]] == [50, 20]


def test_get_ema_honors_timeframe_and_range(make_client):
    fake = FakeCandleProvider(a_rising_series(timeframe=Timeframe.HOUR_1))
    client = make_client(ema_provider=fake)
    r = client.get(
        "/stocks/ticker/AAPL/ema",
        params={"timeframe": "1Hour", "range": "7D", "period": 3},
    )
    assert r.status_code == 200, r.text
    _, timeframe, start, end = fake.received[0]
    assert timeframe is Timeframe.HOUR_1
    assert (end - start).days == 7


@pytest.mark.parametrize("bad_period", [1, 0, -5, 401])
def test_get_ema_out_of_range_period_400(make_client, bad_period):
    client = make_client(ema_provider=FakeCandleProvider(a_rising_series()))
    r = client.get("/stocks/ticker/AAPL/ema", params={"period": bad_period})
    assert r.status_code == 400


def test_get_ema_too_many_lines_400(make_client):
    client = make_client(ema_provider=FakeCandleProvider(a_rising_series()))
    r = client.get(
        "/stocks/ticker/AAPL/ema", params={"period": [5, 10, 20, 50, 100, 200]}
    )
    assert r.status_code == 400


def test_get_ema_invalid_symbol_400(make_client):
    client = make_client(ema_provider=FakeCandleProvider(a_rising_series()))
    assert client.get("/stocks/ticker/123/ema").status_code == 400


def test_get_ema_unknown_symbol_404(make_client):
    client = make_client(ema_provider=FakeCandleProvider(raises=StockNotFound("ZZZZ")))
    assert client.get("/stocks/ticker/ZZZZ/ema").status_code == 404


def test_get_ema_upstream_failure_502(make_client):
    fake = FakeCandleProvider(raises=StockDataUnavailable("AAPL", "boom"))
    client = make_client(ema_provider=fake)
    assert client.get("/stocks/ticker/AAPL/ema").status_code == 502


def test_get_support_levels_returns_200_with_levels(make_client):
    client = make_client(support_levels_provider=FakeCandleProvider(a_support_series()))
    r = client.get("/stocks/ticker/AAPL/support-levels", params={"window": 2})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["symbol"] == "AAPL"
    assert body["timeframe"] == "1Day"
    assert body["reference_price"] == 5.0
    assert body["count"] == 1
    level = body["levels"][0]
    assert level["price"] == 3.0
    assert level["touches"] == 2
    assert level["strength"] == "moderate"
    assert level["distance_percent"] == -40.0
    assert set(level) == {"price", "touches", "last_touched", "strength", "distance_percent"}


def test_get_support_levels_defaults_to_1y_daily(make_client):
    fake = FakeCandleProvider(a_support_series())
    client = make_client(support_levels_provider=fake)
    r = client.get("/stocks/ticker/AAPL/support-levels")
    assert r.status_code == 200, r.text
    symbol, timeframe, start, end = fake.received[0]
    assert symbol == "AAPL"
    assert timeframe is Timeframe.DAY_1                 # default timeframe
    assert (end - start).days == 366                    # default range = 1Y


def test_get_support_levels_honors_timeframe_range_and_window(make_client):
    fake = FakeCandleProvider(a_support_series(timeframe=Timeframe.HOUR_1))
    client = make_client(support_levels_provider=fake)
    r = client.get(
        "/stocks/ticker/AAPL/support-levels",
        params={"timeframe": "1Hour", "range": "7D", "window": 2},
    )
    assert r.status_code == 200, r.text
    _, timeframe, start, end = fake.received[0]
    assert timeframe is Timeframe.HOUR_1
    assert (end - start).days == 7


def test_get_support_levels_explicit_window_overrides_range(make_client):
    fake = FakeCandleProvider(a_support_series())
    client = make_client(support_levels_provider=fake)
    r = client.get(
        "/stocks/ticker/AAPL/support-levels",
        params={"start": "2026-01-01T00:00:00Z", "end": "2026-02-01T00:00:00Z"},
    )
    assert r.status_code == 200
    _, _, start, end = fake.received[0]
    assert start == datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert end == datetime(2026, 2, 1, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    "params",
    [
        {"window": 1},
        {"window": 51},
        {"tolerance": 0},
        {"tolerance": 1},
        {"max_levels": 0},
        {"max_levels": 21},
        {"timeframe": "1Year"},
    ],
)
def test_get_support_levels_invalid_params_422(make_client, params):
    client = make_client(support_levels_provider=FakeCandleProvider(a_support_series()))
    assert client.get("/stocks/ticker/AAPL/support-levels", params=params).status_code == 422


def test_get_support_levels_invalid_symbol_400(make_client):
    client = make_client(support_levels_provider=FakeCandleProvider(a_support_series()))
    assert client.get("/stocks/ticker/123/support-levels").status_code == 400


def test_get_support_levels_inverted_window_400(make_client):
    client = make_client(support_levels_provider=FakeCandleProvider(a_support_series()))
    r = client.get(
        "/stocks/ticker/AAPL/support-levels",
        params={"start": "2026-02-01T00:00:00Z", "end": "2026-01-01T00:00:00Z"},
    )
    assert r.status_code == 400


def test_get_support_levels_unknown_symbol_404(make_client):
    client = make_client(
        support_levels_provider=FakeCandleProvider(raises=StockNotFound("ZZZZ"))
    )
    assert client.get("/stocks/ticker/ZZZZ/support-levels").status_code == 404


def test_get_support_levels_upstream_failure_502(make_client):
    fake = FakeCandleProvider(raises=StockDataUnavailable("AAPL", "boom"))
    client = make_client(support_levels_provider=fake)
    assert client.get("/stocks/ticker/AAPL/support-levels").status_code == 502


def test_get_trend_returns_200_with_all_three_horizons(make_client):
    client = make_client(trend_provider=FakeCandleProvider(a_rising_series(n=20)))
    r = client.get(
        "/stocks/ticker/AAPL/trend",
        params={"short_period": 3, "medium_period": 5, "long_period": 8},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["symbol"] == "AAPL"
    assert body["timeframe"] == "1Day"
    assert body["reading"] == "strong_uptrend"          # rising series, all aligned
    assert body["short_term"]["direction"] == "up"
    assert body["medium_term"]["direction"] == "up"
    assert body["long_term"]["direction"] == "up"
    # Rising series: price leads each line, so the effective read matches the slope.
    assert body["short_term"]["effective_direction"] == "up"
    assert set(body["short_term"]) == {
        "period", "lookback", "direction", "effective_direction", "slope_percent",
        "change_percent", "price_vs_ema_percent", "ema",
    }


def test_get_trend_defaults_to_20_50_200_over_1y_daily(make_client):
    fake = FakeCandleProvider(a_series())               # a single candle
    client = make_client(trend_provider=fake)
    r = client.get("/stocks/ticker/AAPL/trend")
    assert r.status_code == 200, r.text
    symbol, timeframe, start, end = fake.received[0]
    assert symbol == "AAPL"
    assert timeframe is Timeframe.DAY_1
    # 1Y visible window (366d) plus the warmup reach-back before it (now sized to the
    # 200-bar long horizon, so an even deeper reach-back).
    assert (end - start).days > 366
    body = r.json()
    # One candle can't warm any horizon: graceful unknown, not an error.
    assert body["reading"] == "unknown"
    assert body["short_term"] is None
    assert body["medium_term"] is None
    assert body["long_term"] is None


def test_get_trend_honors_timeframe_and_range(make_client):
    fake = FakeCandleProvider(a_rising_series(n=20, timeframe=Timeframe.HOUR_1))
    client = make_client(trend_provider=fake)
    r = client.get(
        "/stocks/ticker/AAPL/trend",
        params={
            "timeframe": "1Hour", "range": "7D",
            "short_period": 3, "medium_period": 5, "long_period": 8,
        },
    )
    assert r.status_code == 200, r.text
    _, timeframe, start, end = fake.received[0]
    assert timeframe is Timeframe.HOUR_1
    # 7D window plus the long-EMA warmup reach-back before it.
    assert (end - start) > timedelta(days=7)


def test_get_trend_flat_threshold_widens_the_sideways_band(make_client):
    # A gently drifting series: a tiny threshold reads up, a large one reads flat.
    base = datetime(2026, 6, 1, tzinfo=timezone.utc)
    candles = tuple(
        a_candle(close=100.0 + i * 0.02, timestamp=base + timedelta(days=i))
        for i in range(20)
    )
    client = make_client(trend_provider=FakeCandleProvider(a_series(candles)))
    tight = client.get(
        "/stocks/ticker/AAPL/trend",
        params={
            "short_period": 3, "medium_period": 5, "long_period": 8,
            "flat_threshold": 0.0,
        },
    ).json()
    assert tight["long_term"]["direction"] == "up"
    loose = client.get(
        "/stocks/ticker/AAPL/trend",
        params={
            "short_period": 3, "medium_period": 5, "long_period": 8,
            "flat_threshold": 5.0,
        },
    ).json()
    assert loose["long_term"]["direction"] == "sideways"


@pytest.mark.parametrize(
    "params",
    [
        {"short_period": 1},
        {"short_period": 401},
        {"medium_period": 1},
        {"medium_period": 401},
        {"long_period": 1},
        {"long_period": 401},
        {"flat_threshold": -0.1},
        {"flat_threshold": 6},
        {"timeframe": "1Year"},
    ],
)
def test_get_trend_invalid_params_422(make_client, params):
    client = make_client(trend_provider=FakeCandleProvider(a_rising_series(n=20)))
    assert client.get("/stocks/ticker/AAPL/trend", params=params).status_code == 422


def test_get_trend_periods_not_strictly_increasing_400(make_client):
    # All in range but not short < medium < long — a domain rule, so a 400 from the
    # use case (not a 422 Query-bound rejection).
    client = make_client(trend_provider=FakeCandleProvider(a_rising_series(n=20)))
    r = client.get(
        "/stocks/ticker/AAPL/trend",
        params={"short_period": 50, "medium_period": 30, "long_period": 20},
    )
    assert r.status_code == 400
    # Medium out of order (short < long but medium above long) is a 400 too.
    r = client.get(
        "/stocks/ticker/AAPL/trend",
        params={"short_period": 10, "medium_period": 300, "long_period": 200},
    )
    assert r.status_code == 400


def test_get_trend_invalid_symbol_400(make_client):
    client = make_client(trend_provider=FakeCandleProvider(a_rising_series(n=20)))
    assert client.get("/stocks/ticker/123/trend").status_code == 400


def test_get_trend_unknown_symbol_404(make_client):
    client = make_client(trend_provider=FakeCandleProvider(raises=StockNotFound("ZZZZ")))
    assert client.get("/stocks/ticker/ZZZZ/trend").status_code == 404


def test_get_trend_upstream_failure_502(make_client):
    fake = FakeCandleProvider(raises=StockDataUnavailable("AAPL", "boom"))
    client = make_client(trend_provider=fake)
    assert client.get("/stocks/ticker/AAPL/trend").status_code == 502


def test_get_sectors_returns_200_ranked_with_computed_fields(make_client):
    tech = a_sector(sector="Technology", symbol="XLK", price=110.0, previous_close=100.0)
    energy = a_sector(sector="Energy", symbol="XLE", price=95.0, previous_close=100.0)
    client = make_client(sector_provider=FakeSectorProvider([energy, tech]))
    r = client.get("/sectors")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["count"] == 2
    first = body["sectors"][0]
    assert first["sector"] == "Technology"          # best performer first
    assert first["symbol"] == "XLK"
    assert first["change"] == 10.0                  # entity rule via presenter
    assert first["change_percent"] == 10.0
    assert body["sectors"][1]["change_percent"] == -5.0


def test_get_sectors_includes_trailing_performance_alias_keys(make_client):
    client = make_client(sector_provider=FakeSectorProvider([a_sector(performance=a_performance())]))
    body = client.get("/sectors").json()
    # trailing windows serialize with the finance-style JSON keys
    assert body["sectors"][0]["performance"] == {
        "1w": 1.2, "1m": -0.4, "3m": 5.1, "6m": 8.7, "ytd": 12.3, "1y": 21.0,
    }


def test_get_sectors_without_performance_is_null(make_client):
    client = make_client(sector_provider=FakeSectorProvider([a_sector()]))
    assert client.get("/sectors").json()["sectors"][0]["performance"] is None


def test_get_sectors_unknown_404(make_client):
    client = make_client(sector_provider=FakeSectorProvider(raises=StockNotFound("sectors")))
    assert client.get("/sectors").status_code == 404


def test_get_sectors_upstream_failure_502(make_client):
    fake = FakeSectorProvider(raises=StockDataUnavailable("sectors", "boom"))
    client = make_client(sector_provider=fake)
    assert client.get("/sectors").status_code == 502


# Reuses the Bedrock stub client defined in the AI-analysis section above, but
# forces the sector tool (submit_sector_analysis) instead of submit_analysis.
def _sector_tool_message(**input_overrides) -> _StubMessage:
    payload = dict(
        summary="Growth-sensitive sectors led; defensives lagged.",
        tone="risk_on",
        leaders=[{"sector": "Technology", "note": "Chipmakers rallied."}],
        laggards=[{"sector": "Energy", "note": "Crude slipped."}],
    )
    payload.update(input_overrides)
    return _StubMessage(
        [_StubBlock("tool_use", name="submit_sector_analysis", input=payload)]
    )


def _ctx(
    sector, symbol, change_percent, *, performance=None, movers=(), headlines=()
) -> SectorContext:
    return SectorContext(
        sector=sector,
        symbol=symbol,
        change_percent=change_percent,
        performance=performance,
        movers=tuple(movers),
        headlines=tuple(headlines),
    )


def _a_board() -> list[SectorContext]:
    # Two sectors with known day moves: Technology +10%, Energy -5%.
    return [
        _ctx("Technology", "XLK", 10.0),
        _ctx("Energy", "XLE", -5.0),
    ]


def test_sector_analysis_parses_tool_call_into_entity():
    client = _StubClient(_sector_tool_message())
    provider = BedrockSectorAnalysisAdapter(client=client, model_id="test-model")

    analysis = provider.analyze(_a_board())

    assert analysis.summary.startswith("Growth-sensitive")
    assert analysis.tone is MarketTone.RISK_ON
    # Leader/laggard notes are joined back to the board for the real day move.
    assert analysis.leaders[0].sector == "Technology"
    assert analysis.leaders[0].symbol == "XLK"
    assert analysis.leaders[0].change_percent == 10.0  # from the board, not the model
    assert analysis.leaders[0].note == "Chipmakers rallied."
    assert analysis.laggards[0].change_percent == -5.0
    assert analysis.model == "test-model"
    # The model was actually pinned to the sector tool.
    assert client.calls[0]["tool_choice"] == {
        "type": "tool",
        "name": "submit_sector_analysis",
    }


def test_sector_analysis_renders_board_into_prompt():
    client = _StubClient(_sector_tool_message())
    board = [_ctx("Technology", "XLK", 10.0, performance=a_performance())]
    BedrockSectorAnalysisAdapter(client=client).analyze(board)

    prompt = client.calls[0]["messages"][0]["content"]
    assert "Market sectors today" in prompt
    assert "Technology (XLK)" in prompt
    assert "day 10.00%" in prompt
    assert "1y 21.00%" in prompt  # trailing windows render when present


def test_sector_analysis_renders_movers_breadth_and_headlines_into_prompt():
    # A sector's grounded drivers render as indented lines under it, so the model can
    # cite the specific stocks + catalyst behind the move.
    client = _StubClient(_sector_tool_message())
    board = [
        _ctx(
            "Technology",
            "XLK",
            2.1,
            movers=(
                SectorMover("NVDA", "NVIDIA", 6.2, 3.2e12),
                SectorMover("AVGO", "Broadcom", 4.1, 8.0e11),
            ),
            headlines=(
                SectorHeadline("NVDA", "NVIDIA beats on data-center demand"),
            ),
        )
    ]
    BedrockSectorAnalysisAdapter(client=client).analyze(board)

    prompt = client.calls[0]["messages"][0]["content"]
    assert "driven by: NVIDIA +6.20%; Broadcom +4.10%" in prompt
    assert "headline (NVDA): NVIDIA beats on data-center demand" in prompt


def test_sector_analysis_joins_movers_and_headlines_onto_the_highlight():
    # The model authors only the note; the movers + headlines on the highlight come from
    # the context (real, service-supplied), never the model.
    client = _StubClient(_sector_tool_message())
    movers = (SectorMover("NVDA", "NVIDIA", 6.2, 3.2e12),)
    headlines = (SectorHeadline("NVDA", "NVIDIA beats"),)
    board = [
        _ctx("Technology", "XLK", 2.1, movers=movers, headlines=headlines),
        _ctx("Energy", "XLE", -5.0),
    ]

    analysis = BedrockSectorAnalysisAdapter(client=client).analyze(board)

    leader = analysis.leaders[0]
    assert [m.ticker for m in leader.movers] == ["NVDA"]
    assert leader.movers[0].name == "NVIDIA"
    assert [h.title for h in leader.headlines] == ["NVIDIA beats"]


def test_sector_analysis_joins_real_percent_and_drops_unknown_sector():
    # The model names one sector on the board and one that isn't; the unknown one is
    # dropped, and the known one carries the board's real percent, never a model figure.
    client = _StubClient(
        _sector_tool_message(
            leaders=[
                {"sector": "Technology", "note": "on the board"},
                {"sector": "Nowhere", "note": "off the board"},
            ]
        )
    )
    analysis = BedrockSectorAnalysisAdapter(client=client).analyze(_a_board())
    assert [h.sector for h in analysis.leaders] == ["Technology"]
    assert analysis.leaders[0].change_percent == 10.0


def test_sector_analysis_drops_a_highlight_without_a_note():
    client = _StubClient(
        _sector_tool_message(laggards=[{"sector": "Energy", "note": ""}])
    )
    analysis = BedrockSectorAnalysisAdapter(client=client).analyze(_a_board())
    assert analysis.laggards == ()


def test_sector_analysis_raises_when_model_does_not_call_the_tool():
    client = _StubClient(_StubMessage([_StubBlock("text")]))  # no tool_use block
    with pytest.raises(StockDataUnavailable):
        BedrockSectorAnalysisAdapter(client=client).analyze(_a_board())


def test_sector_analysis_maps_a_client_error_to_a_domain_error():
    with pytest.raises(StockDataUnavailable):
        BedrockSectorAnalysisAdapter(client=_BoomClient()).analyze(_a_board())


def test_sector_analysis_rejects_an_offschema_tone():
    client = _StubClient(_sector_tool_message(tone="euphoric"))  # not in the enum
    with pytest.raises(StockDataUnavailable):
        BedrockSectorAnalysisAdapter(client=client).analyze(_a_board())


def test_sector_analysis_retries_once_when_lists_come_back_empty():
    # An empty leaders/laggards result is retried once and the recovered read used.
    empty = _sector_tool_message(leaders=[], laggards=[])
    full = _sector_tool_message()
    client = _SeqStubClient([empty, full])

    analysis = BedrockSectorAnalysisAdapter(client=client).analyze(_a_board())

    assert len(client.calls) == 2  # retried exactly once
    assert [h.sector for h in analysis.leaders] == ["Technology"]
    assert [h.sector for h in analysis.laggards] == ["Energy"]


def test_sector_analysis_use_case_hands_over_a_ranked_board():
    # The use case ranks the board best-first before handing it to the analyzer.
    analyzer = FakeSectorAnalysisAdapter(a_sector_analysis())
    tech = a_sector(sector="Technology", symbol="XLK", price=110.0, previous_close=100.0)
    energy = a_sector(sector="Energy", symbol="XLE", price=95.0, previous_close=100.0)
    use_case = GetSectorAnalysis(
        GetSectorPerformance(FakeSectorProvider([energy, tech])), analyzer
    )
    use_case.execute()
    assert [s.sector for s in analyzer.received] == ["Technology", "Energy"]


# --- sector attribution: the movers / breadth / headlines behind a move ("why") -----------


class _FakeConstituentsRepo(StockSearchRepository):
    def __init__(self, results=(), *, raises=None):
        self._results = tuple(results)
        self.criteria: StockSearchCriteria | None = None
        self._raises = raises

    def search(self, criteria):
        self.criteria = criteria
        if self._raises is not None:
            raise self._raises
        return StockSearchPage(
            results=self._results,
            total=len(self._results),
            limit=criteria.limit,
            offset=criteria.offset,
        )

    def classifications(self):  # pragma: no cover - unused by attribution
        raise NotImplementedError

    def pe_ratios_for_industry(self, industry):  # pragma: no cover - unused
        raise NotImplementedError

    def industry_for_ticker(self, ticker):  # pragma: no cover - unused
        raise NotImplementedError

    def anchor_metrics_for_ticker(self, ticker):  # pragma: no cover - unused
        raise NotImplementedError

    def tier_for_ticker(self, ticker):  # pragma: no cover - unused
        raise NotImplementedError

    def industry_peers(self, industry):  # pragma: no cover - unused
        raise NotImplementedError

    def peers_for_industry(self, industry):  # pragma: no cover - unused
        raise NotImplementedError


class _FakeBulkQuotes:
    def __init__(self, change_by_ticker=None, *, raises=None):
        self._changes = change_by_ticker or {}
        self._raises = raises
        self.requested: tuple[str, ...] | None = None

    def get_quotes(self, symbols):
        self.requested = tuple(symbols)
        if self._raises is not None:
            raise self._raises
        out = {}
        for s in symbols:
            pct = self._changes.get(s)
            if pct is None:  # feed carries no quote -> absent (best-effort per symbol)
                continue
            prev = 100.0
            out[s] = Quote(
                symbol=s,
                price=prev * (1 + pct / 100),
                previous_close=prev,
                bid=None,
                ask=None,
                as_of=datetime(2026, 7, 9, tzinfo=timezone.utc),
            )
        return out


class _FakeNewsRepo(NewsRepository):
    def __init__(self, news_by_symbol=None):
        self._news = news_by_symbol or {}
        self.requested: list[str] = []

    def get(self, symbol):
        self.requested.append(symbol)
        return self._news.get(symbol)

    def upsert(self, symbol, name, news):  # pragma: no cover - unused by the read path
        raise NotImplementedError

    def refresh_targets(self, limit):  # pragma: no cover - unused by the read path
        raise NotImplementedError


def _screened(ticker, sector, market_cap, *, name=None) -> StockSearchResult:
    return StockSearchResult(
        ticker=ticker,
        name=name or f"{ticker} Inc.",
        sector=sector,  # the stored Yahoo slug, e.g. "technology" / "financial_services"
        industry=None,
        market_cap=market_cap,
        pe_ratio=None,
        fcf_yield=None,
        ev_ebitda=None,
        revenue_growth_yoy=None,
        eps_growth_yoy=None,
        fcf_growth_yoy=None,
        forward_revenue_growth_yoy=None,
        forward_eps_growth_yoy=None,
        in_sp500=True,
        in_nasdaq100=False,
        performance=None,
    )


def test_sector_analysis_attributes_cap_weighted_gainers_for_an_up_sector():
    analyzer = FakeSectorAnalysisAdapter(a_sector_analysis())
    tech = a_sector(sector="Technology", symbol="XLK", price=110.0, previous_close=100.0)
    constituents = _FakeConstituentsRepo(
        [
            _screened("NVDA", "technology", 3.2e12, name="NVIDIA"),
            _screened("SMALL", "technology", 1.0e10, name="SmallCo"),
            _screened("AAPL", "technology", 3.0e12, name="Apple"),
        ]
    )
    quotes = _FakeBulkQuotes({"NVDA": 6.0, "SMALL": 20.0, "AAPL": 1.0})
    GetSectorAnalysis(
        GetSectorPerformance(FakeSectorProvider([tech])),
        analyzer,
        constituents=constituents,
        quotes=quotes,
    ).execute()

    (ctx,) = analyzer.received
    # Up sector -> gainers, ranked by cap x change: NVDA (1.92e13) > AAPL (3.0e12) > SMALL (2e11).
    assert [m.ticker for m in ctx.movers] == ["NVDA", "AAPL", "SMALL"]
    assert ctx.movers[0].name == "NVIDIA"
    assert ctx.movers[0].change_percent == 6.0
    assert (ctx.breadth.advancers, ctx.breadth.decliners, ctx.breadth.total) == (3, 0, 3)
    # The constituents are read from the S&P 500 members (the sector ETFs' own universe).
    assert constituents.criteria.in_sp500 is True


def test_sector_analysis_attributes_losers_for_a_down_sector():
    analyzer = FakeSectorAnalysisAdapter(a_sector_analysis())
    energy = a_sector(sector="Energy", symbol="XLE", price=95.0, previous_close=100.0)
    constituents = _FakeConstituentsRepo(
        [
            _screened("XOM", "energy", 5.0e11, name="Exxon"),
            _screened("CVX", "energy", 3.0e11, name="Chevron"),
            _screened("UP", "energy", 1.0e11, name="UpCo"),
        ]
    )
    quotes = _FakeBulkQuotes({"XOM": -3.0, "CVX": -2.0, "UP": 1.0})
    GetSectorAnalysis(
        GetSectorPerformance(FakeSectorProvider([energy])),
        analyzer,
        constituents=constituents,
        quotes=quotes,
    ).execute()

    (ctx,) = analyzer.received
    # Down sector -> losers, most-negative contribution first; the lone gainer is excluded.
    assert [m.ticker for m in ctx.movers] == ["XOM", "CVX"]
    assert (ctx.breadth.advancers, ctx.breadth.decliners, ctx.breadth.total) == (1, 2, 3)


def test_sector_analysis_maps_gics_board_name_to_the_yahoo_sector_slug():
    # The board says "Financials"; the universe stores "financial_services". The join must
    # still find the constituents (the bug this mapping exists to prevent).
    analyzer = FakeSectorAnalysisAdapter(a_sector_analysis())
    fins = a_sector(sector="Financials", symbol="XLF", price=101.0, previous_close=100.0)
    constituents = _FakeConstituentsRepo(
        [_screened("JPM", "financial_services", 6.0e11, name="JPMorgan")]
    )
    quotes = _FakeBulkQuotes({"JPM": 2.0})
    GetSectorAnalysis(
        GetSectorPerformance(FakeSectorProvider([fins])),
        analyzer,
        constituents=constituents,
        quotes=quotes,
    ).execute()

    (ctx,) = analyzer.received
    assert [m.ticker for m in ctx.movers] == ["JPM"]


def test_sector_analysis_attaches_catalyst_headlines_db_only():
    analyzer = FakeSectorAnalysisAdapter(a_sector_analysis())
    tech = a_sector(sector="Technology", symbol="XLK", price=110.0, previous_close=100.0)
    constituents = _FakeConstituentsRepo(
        [_screened("NVDA", "technology", 3.2e12, name="NVIDIA")]
    )
    quotes = _FakeBulkQuotes({"NVDA": 6.0})
    news = _FakeNewsRepo(
        {
            "NVDA": StockNews(
                "NVDA",
                (
                    NewsArticle(
                        id="1",
                        title="NVIDIA beats on data-center demand",
                        published_at=datetime(2026, 7, 9, tzinfo=timezone.utc),
                        publisher="Reuters",
                        link="http://example/nvda",
                    ),
                ),
            )
        }
    )
    GetSectorAnalysis(
        GetSectorPerformance(FakeSectorProvider([tech])),
        analyzer,
        constituents=constituents,
        quotes=quotes,
        news=news,
    ).execute()

    (ctx,) = analyzer.received
    assert [h.title for h in ctx.headlines] == ["NVIDIA beats on data-center demand"]
    assert ctx.headlines[0].ticker == "NVDA"
    assert news.requested == ["NVDA"]  # read DB-only for the surfaced mover


def test_sector_analysis_degrades_to_plain_board_without_attribution():
    # No attribution collaborators wired -> contexts carry the board row and nothing else,
    # so the analysis still runs (its prior behaviour) rather than failing.
    analyzer = FakeSectorAnalysisAdapter(a_sector_analysis())
    tech = a_sector(sector="Technology", symbol="XLK", price=110.0, previous_close=100.0)
    GetSectorAnalysis(
        GetSectorPerformance(FakeSectorProvider([tech])), analyzer
    ).execute()

    (ctx,) = analyzer.received
    assert ctx.sector == "Technology"
    assert ctx.movers == ()
    assert ctx.breadth is None
    assert ctx.headlines == ()


def test_sector_analysis_survives_a_quote_feed_failure():
    # A hard quote-feed failure is swallowed: the movers just carry no change (and drop from
    # the ranking), never sinking the analysis.
    analyzer = FakeSectorAnalysisAdapter(a_sector_analysis())
    tech = a_sector(sector="Technology", symbol="XLK", price=110.0, previous_close=100.0)
    constituents = _FakeConstituentsRepo(
        [_screened("NVDA", "technology", 3.2e12, name="NVIDIA")]
    )
    quotes = _FakeBulkQuotes(raises=StockDataUnavailable("quotes", "feed down"))
    result = GetSectorAnalysis(
        GetSectorPerformance(FakeSectorProvider([tech])),
        analyzer,
        constituents=constituents,
        quotes=quotes,
    ).execute()

    assert result is not None  # did not raise
    (ctx,) = analyzer.received
    assert ctx.movers == ()
    assert ctx.breadth is None


def test_sector_analysis_survives_a_constituent_read_failure():
    # A DB hiccup on the constituent read degrades to the plain board, never sinks the run.
    analyzer = FakeSectorAnalysisAdapter(a_sector_analysis())
    tech = a_sector(sector="Technology", symbol="XLK", price=110.0, previous_close=100.0)
    constituents = _FakeConstituentsRepo(raises=RuntimeError("db down"))
    quotes = _FakeBulkQuotes({"NVDA": 6.0})
    result = GetSectorAnalysis(
        GetSectorPerformance(FakeSectorProvider([tech])),
        analyzer,
        constituents=constituents,
        quotes=quotes,
    ).execute()

    assert result is not None
    (ctx,) = analyzer.received
    assert ctx.movers == ()


def test_sector_analysis_use_case_propagates_a_board_failure():
    analyzer = FakeSectorAnalysisAdapter(a_sector_analysis())
    use_case = GetSectorAnalysis(
        GetSectorPerformance(
            FakeSectorProvider(raises=StockDataUnavailable("sectors", "boom"))
        ),
        analyzer,
    )
    with pytest.raises(StockDataUnavailable):
        use_case.execute()


def test_get_sector_analysis_returns_200(make_client):
    client = make_client(
        sector_analysis_provider=FakeSectorAnalysisAdapter(a_sector_analysis())
    )
    r = client.get("/sectors/analysis")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["summary"]
    assert body["tone"] == "risk_on"
    assert body["leaders"][0]["sector"] == "Technology"
    assert body["leaders"][0]["change_percent"] == 1.8
    assert body["laggards"][0]["sector"] == "Utilities"
    assert "not financial advice" in body["disclaimer"].lower()
    assert body["model"] == "claude-opus-4-8"


def test_get_sector_analysis_serializes_movers_and_headlines(make_client):
    # The grounded receipts behind a note serialize onto the highlight (driver chips + catalyst).
    analysis = a_sector_analysis(
        leaders=(
            SectorHighlight(
                "Technology",
                "XLK",
                2.1,
                "Led by chipmakers after a strong earnings beat.",
                movers=(SectorMover("NVDA", "NVIDIA", 6.2, 3.2e12),),
                headlines=(
                    SectorHeadline(
                        "NVDA", "NVIDIA beats", None, "Reuters", "http://example/nvda"
                    ),
                ),
            ),
        )
    )
    client = make_client(
        sector_analysis_provider=FakeSectorAnalysisAdapter(analysis)
    )
    r = client.get("/sectors/analysis")
    assert r.status_code == 200, r.text
    lead = r.json()["leaders"][0]
    assert lead["movers"][0] == {
        "ticker": "NVDA",
        "name": "NVIDIA",
        "change_percent": 6.2,
        "market_cap": 3.2e12,
    }
    assert lead["headlines"][0]["ticker"] == "NVDA"
    assert lead["headlines"][0]["title"] == "NVIDIA beats"
    assert lead["headlines"][0]["link"] == "http://example/nvda"


def test_get_sector_analysis_502_when_model_fails(make_client):
    client = make_client(
        sector_analysis_provider=FakeSectorAnalysisAdapter(
            raises=StockDataUnavailable("sectors", "bedrock timeout")
        )
    )
    assert client.get("/sectors/analysis").status_code == 502


def test_get_sector_analysis_404_when_board_unavailable(make_client):
    # The board is primary — a not-found board surfaces as 404, like /sectors.
    client = make_client(
        sector_provider=FakeSectorProvider(raises=StockNotFound("sectors")),
        sector_analysis_provider=FakeSectorAnalysisAdapter(a_sector_analysis()),
    )
    assert client.get("/sectors/analysis").status_code == 404


# Reuses the Bedrock stub client from the AI-analysis section, forcing the market
# tool (submit_market_summary) with a note per timeframe.
def _market_tool_message(**input_overrides) -> _StubMessage:
    payload = dict(
        summary="The market has climbed over the year, easing this week.",
        tone="risk_on",
        periods=[
            {"period": "year", "note": "A strong year for both indexes."},
            {"period": "month", "note": "Modest monthly gains."},
            {"period": "week", "note": "A slight weekly pullback."},
        ],
    )
    payload.update(input_overrides)
    return _StubMessage(
        [_StubBlock("tool_use", name="submit_market_summary", input=payload)]
    )


def _a_market_board() -> list[MarketIndexPerformance]:
    # The S&P 500 and Nasdaq with known trailing windows (week/month/year).
    return [
        a_market_index(
            name="S&P 500", symbol="SPY", price=550.0, previous_close=545.0,
            performance=a_performance(one_week=-0.6, one_month=2.1, one_year=18.4),
        ),
        a_market_index(
            name="Nasdaq", symbol="QQQ", price=480.0, previous_close=475.0,
            performance=a_performance(one_week=-0.9, one_month=3.0, one_year=24.1),
        ),
    ]


def test_market_index_entity_change_and_percent():
    index = a_market_index(price=550.0, previous_close=545.0)
    assert index.change == 5.0
    assert index.change_percent == 0.92


def test_market_summary_parses_tool_call_into_entity():
    client = _StubClient(_market_tool_message())
    provider = BedrockMarketSummaryAdapter(client=client, model_id="test-model")

    summary = provider.analyze(_a_market_board())

    assert summary.summary.startswith("The market has climbed")
    assert summary.tone is MarketTone.RISK_ON
    # Periods always render in year -> month -> week order.
    assert [p.period for p in summary.periods] == [
        MarketPeriod.YEAR,
        MarketPeriod.MONTH,
        MarketPeriod.WEEK,
    ]
    year = summary.periods[0]
    assert year.note == "A strong year for both indexes."
    # Index returns are joined from the board's trailing windows, not authored by
    # the model — a real quote per index for that window.
    year_by_symbol = {r.symbol: r for r in year.indexes}
    assert year_by_symbol["SPY"].name == "S&P 500"
    assert year_by_symbol["SPY"].change_percent == 18.4  # from the board
    assert year_by_symbol["QQQ"].change_percent == 24.1
    week_by_symbol = {r.symbol: r.change_percent for r in summary.periods[2].indexes}
    assert week_by_symbol["SPY"] == -0.6 and week_by_symbol["QQQ"] == -0.9
    assert summary.model == "test-model"
    # The model was actually pinned to the market tool.
    assert client.calls[0]["tool_choice"] == {
        "type": "tool",
        "name": "submit_market_summary",
    }


def test_market_summary_renders_board_into_prompt():
    client = _StubClient(_market_tool_message())
    BedrockMarketSummaryAdapter(client=client).analyze(_a_market_board())

    prompt = client.calls[0]["messages"][0]["content"]
    assert "US market today" in prompt
    assert "S&P 500 (SPY)" in prompt
    assert "Nasdaq (QQQ)" in prompt
    assert "today 0.92%" in prompt  # SPY day move: (550-545)/545
    assert "past year 18.40%" in prompt
    assert "past week -0.60%" in prompt


def test_market_summary_keeps_a_period_even_without_a_note():
    # The numbers come from the board, so a timeframe the model didn't write about
    # still renders (with an empty note) rather than dropping its figures.
    client = _StubClient(
        _market_tool_message(periods=[{"period": "year", "note": "A strong year."}])
    )
    summary = BedrockMarketSummaryAdapter(client=client).analyze(_a_market_board())
    assert [p.period for p in summary.periods] == [
        MarketPeriod.YEAR,
        MarketPeriod.MONTH,
        MarketPeriod.WEEK,
    ]
    assert summary.periods[0].note == "A strong year."
    assert summary.periods[1].note == ""  # month: no note, but numbers still present
    assert summary.periods[1].indexes[0].change_percent == 2.1


def test_market_summary_builds_none_returns_without_history():
    # An index with no trailing performance still appears, with None returns.
    board = [a_market_index(name="S&P 500", symbol="SPY", performance=None)]
    client = _StubClient(_market_tool_message())
    summary = BedrockMarketSummaryAdapter(client=client).analyze(board)
    year = summary.periods[0]
    assert year.indexes[0].symbol == "SPY"
    assert year.indexes[0].change_percent is None


def test_market_summary_raises_when_model_does_not_call_the_tool():
    client = _StubClient(_StubMessage([_StubBlock("text")]))  # no tool_use block
    with pytest.raises(StockDataUnavailable):
        BedrockMarketSummaryAdapter(client=client).analyze(_a_market_board())


def test_market_summary_maps_a_client_error_to_a_domain_error():
    with pytest.raises(StockDataUnavailable):
        BedrockMarketSummaryAdapter(client=_BoomClient()).analyze(_a_market_board())


def test_market_summary_rejects_an_offschema_tone():
    client = _StubClient(_market_tool_message(tone="euphoric"))  # not in the enum
    with pytest.raises(StockDataUnavailable):
        BedrockMarketSummaryAdapter(client=client).analyze(_a_market_board())


def test_market_summary_retries_once_when_periods_come_back_empty():
    # An empty periods list (no per-timeframe notes) is retried once and recovered.
    empty = _market_tool_message(periods=[])
    full = _market_tool_message()
    client = _SeqStubClient([empty, full])

    summary = BedrockMarketSummaryAdapter(client=client).analyze(_a_market_board())

    assert len(client.calls) == 2  # retried exactly once
    assert summary.periods[0].note == "A strong year for both indexes."


# Reuses the Bedrock stub client, forcing the earnings tool
# (submit_earnings_analysis) with a plain summary, a trend, and highlights.
def _earnings_tool_message(**input_overrides) -> _StubMessage:
    payload = dict(
        summary="It keeps beating expectations and profit is climbing fast.",
        trend="accelerating",
        highlights=[
            "Beat estimates every recent quarter",
            "Profit and sales are both growing",
        ],
    )
    payload.update(input_overrides)
    return _StubMessage(
        [_StubBlock("tool_use", name="submit_earnings_analysis", input=payload)]
    )


def _earnings_highlights_message(**input_overrides) -> _StubMessage:
    # The lighter recovery tool the retry path forces — only the highlights list.
    payload = dict(
        highlights=[
            "Beat estimates every recent quarter",
            "Profit and sales are both growing",
        ],
    )
    payload.update(input_overrides)
    return _StubMessage(
        [_StubBlock("tool_use", name="submit_highlights", input=payload)]
    )


def test_earnings_analysis_parses_tool_call_into_entity():
    client = _StubClient(_earnings_tool_message())
    provider = BedrockEarningsAnalysisAdapter(client=client, model_id="test-model")

    analysis = provider.analyze(
        "aapl", a_quarterly_timeline(), an_annual_timeline()
    )

    assert analysis.symbol == "AAPL"  # normalized by the adapter
    assert analysis.summary.startswith("It keeps beating")
    assert analysis.trend is EarningsTrend.ACCELERATING
    assert analysis.highlights == (
        "Beat estimates every recent quarter",
        "Profit and sales are both growing",
    )
    assert analysis.model == "test-model"
    # The model was actually pinned to the earnings tool.
    assert client.calls[0]["tool_choice"] == {
        "type": "tool",
        "name": "submit_earnings_analysis",
    }


def test_earnings_analysis_renders_timelines_into_prompt():
    client = _StubClient(_earnings_tool_message())
    BedrockEarningsAnalysisAdapter(client=client).analyze(
        "AAPL", a_quarterly_timeline(), an_annual_timeline()
    )

    prompt = client.calls[0]["messages"][0]["content"]
    assert "Earnings for AAPL" in prompt
    # The reported quarter's real figures — beat tally, EPS vs estimate, revenue.
    assert "beat or met the estimate in 1 of 1" in prompt
    assert "EPS $2.18 vs est $2.10" in prompt
    assert "revenue $95.0B" in prompt
    # The reported fiscal year (consensus-basis EPS) and the forward consensus.
    assert "FY25: EPS $6.50" in prompt
    assert "Upcoming fiscal years" in prompt
    assert "est EPS $8.00" in prompt


def test_earnings_analysis_raises_when_model_does_not_call_the_tool():
    client = _StubClient(_StubMessage([_StubBlock("text")]))  # no tool_use block
    with pytest.raises(StockDataUnavailable):
        BedrockEarningsAnalysisAdapter(client=client).analyze(
            "AAPL", a_quarterly_timeline()
        )


def test_earnings_analysis_maps_a_client_error_to_a_domain_error():
    with pytest.raises(StockDataUnavailable):
        BedrockEarningsAnalysisAdapter(client=_BoomClient()).analyze(
            "AAPL", a_quarterly_timeline()
        )


def test_earnings_analysis_rejects_an_offschema_trend():
    client = _StubClient(_earnings_tool_message(trend="exploding"))  # not in enum
    with pytest.raises(StockDataUnavailable):
        BedrockEarningsAnalysisAdapter(client=client).analyze(
            "AAPL", a_quarterly_timeline()
        )


def test_earnings_analysis_retries_once_when_highlights_come_back_empty():
    # An empty highlights list is retried with the lighter highlights-only tool and
    # the recovered list merged in — a fraction of the tokens of a full re-run.
    empty = _earnings_tool_message(highlights=[])
    highlights = _earnings_highlights_message()  # the targeted recovery call
    client = _SeqStubClient([empty, highlights])

    analysis = BedrockEarningsAnalysisAdapter(client=client).analyze(
        "AAPL", a_quarterly_timeline()
    )

    assert len(client.calls) == 2  # retried exactly once
    # the recovery is the lighter, highlights-only forced tool, not the full analysis
    assert client.calls[1]["tool_choice"] == {"type": "tool", "name": "submit_highlights"}
    assert analysis.highlights == (
        "Beat estimates every recent quarter",
        "Profit and sales are both growing",
    )


def test_earnings_analysis_drops_string_highlights_instead_of_char_splitting():
    # Bedrock does not strictly enforce the tool schema, and Haiku occasionally
    # returns `highlights` as a single string (a leaked tool-call parameter)
    # rather than an array. Iterating a str would split it into characters, so
    # the coercion must reject a non-list — summary/trend still parse.
    leaked = '<parameter name="highlights">["Beat every quarter", "Profit climbing"]'
    client = _StubClient(_earnings_tool_message(highlights=leaked))

    analysis = BedrockEarningsAnalysisAdapter(client=client).analyze(
        "AAPL", a_quarterly_timeline()
    )

    assert analysis.highlights == ()  # not a wall of single characters
    assert analysis.trend is EarningsTrend.ACCELERATING
    assert analysis.summary.startswith("It keeps beating")


def test_market_summary_use_case_hands_over_the_board():
    analyzer = FakeMarketSummaryAdapter(a_market_summary())
    use_case = GetMarketSummary(
        GetMarketOverview(FakeMarketOverviewProvider(_a_market_board())), analyzer
    )
    use_case.execute()
    assert [i.symbol for i in analyzer.received] == ["SPY", "QQQ"]


def test_market_summary_use_case_propagates_a_board_failure():
    analyzer = FakeMarketSummaryAdapter(a_market_summary())
    use_case = GetMarketSummary(
        GetMarketOverview(
            FakeMarketOverviewProvider(raises=StockDataUnavailable("market", "boom"))
        ),
        analyzer,
    )
    with pytest.raises(StockDataUnavailable):
        use_case.execute()


def test_get_market_summary_returns_200(make_client):
    client = make_client(
        market_summary_provider=FakeMarketSummaryAdapter(a_market_summary())
    )
    r = client.get("/market/summary")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["summary"]
    assert body["tone"] == "risk_on"
    assert [p["period"] for p in body["periods"]] == ["year", "month", "week"]
    year = body["periods"][0]
    assert year["indexes"][0]["name"] == "S&P 500"
    assert year["indexes"][0]["change_percent"] == 18.4
    assert year["indexes"][1]["symbol"] == "QQQ"
    assert year["note"]
    assert "not financial advice" in body["disclaimer"].lower()
    assert body["model"] == "claude-opus-4-8"
    assert r.headers["cache-control"] == "public, max-age=900"


def test_get_market_summary_502_when_model_fails(make_client):
    client = make_client(
        market_summary_provider=FakeMarketSummaryAdapter(
            raises=StockDataUnavailable("market", "bedrock timeout")
        )
    )
    assert client.get("/market/summary").status_code == 502


def test_get_market_summary_404_when_board_unavailable(make_client):
    # The board is primary — a not-found board surfaces as 404, like /sectors.
    client = make_client(
        market_overview_provider=FakeMarketOverviewProvider(
            raises=StockNotFound("market")
        ),
        market_summary_provider=FakeMarketSummaryAdapter(a_market_summary()),
    )
    assert client.get("/market/summary").status_code == 404


def test_cors_allows_configured_origin(make_client):
    client = make_client(logo_provider=FakeLogoProvider(a_logo(content=b"\x89PNG\r\n")))
    origin = "https://namainsights.com"
    r = client.get("/stocks/AAPL/logo", headers={"Origin": origin})
    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] == origin


def test_cors_preflight_succeeds(make_client):
    client = make_client(logo_provider=FakeLogoProvider(a_logo()))
    r = client.options(
        "/stocks/AAPL/logo",
        headers={
            "Origin": "https://namainsights.com",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert r.status_code == 200  # was 405 before CORSMiddleware
    assert r.headers["access-control-allow-origin"] == "https://namainsights.com"
