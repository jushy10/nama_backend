from datetime import date, datetime, timezone

from app.domains.research.agent.entities import (
    MarketSentimentResult,
    StockScreenResult,
    ToolMessage,
)
from app.domains.research.agent.tools import MarketSentimentTool, SearchStocksTool
from app.domains.shared.exceptions import StockDataUnavailable
from app.domains.macro.sentiment.entities import (
    FearGreedSnapshot,
    MarketSentiment,
    VixSnapshot,
)
from app.domains.listings.universe.entities import (
    MarketCapTier,
    SortDirection,
    StockSearchPage,
    StockSearchResult,
    StockSort,
)


class _FakeSearch:
    def __init__(self, results=()) -> None:
        self._results = tuple(results)
        self.kwargs: dict | None = None

    def execute(self, **kwargs) -> StockSearchPage:
        self.kwargs = kwargs
        return StockSearchPage(
            results=self._results, total=len(self._results), limit=kwargs.get("limit", 25), offset=0
        )


def _row(ticker="NVDA", **over) -> StockSearchResult:
    base = dict(
        ticker=ticker,
        name="NVIDIA Corp",
        sector="technology",
        industry="semiconductors",
        market_cap=3_400_000_000_000.0,
        pe_ratio=55.2,
        fcf_yield=1.2,
        ev_ebitda=40.0,
        revenue_growth_yoy=94.0,
        eps_growth_yoy=100.0,
        fcf_growth_yoy=80.0,
        forward_revenue_growth_yoy=50.0,
        forward_eps_growth_yoy=55.0,
        in_sp500=True,
        in_nasdaq100=True,
    )
    base.update(over)
    return StockSearchResult(**base)


def test_search_tool_schema_advertises_the_enum_vocabularies():
    schema = SearchStocksTool(_FakeSearch()).spec.input_schema
    props = schema["properties"]
    assert props["market_cap_tiers"]["items"]["enum"] == [t.value for t in MarketCapTier]
    assert props["sort"]["enum"] == [s.value for s in StockSort]
    assert schema["type"] == "object"


def test_search_tool_coerces_arguments_onto_the_use_case():
    fake = _FakeSearch(results=[_row()])
    out = SearchStocksTool(fake).run(
        {
            "query": " NVDA ",
            "sectors": ["technology", "  "],  # blank dropped
            "market_cap_tiers": ["mega", "gigantic"],  # unknown tier dropped
            "sort": "market_cap",
            "direction": "desc",
            "in_sp500": True,
            "limit": 999,  # clamped to the tool's row cap
        }
    )
    assert fake.kwargs["query"] == "NVDA"
    assert fake.kwargs["sectors"] == ("technology",)
    assert fake.kwargs["market_cap_tiers"] == (MarketCapTier.MEGA,)
    assert fake.kwargs["sort"] is StockSort.MARKET_CAP
    assert fake.kwargs["direction"] is SortDirection.DESC
    assert fake.kwargs["in_sp500"] is True
    assert fake.kwargs["limit"] == 15  # _MAX_SCREEN_ROWS
    # The payload carries the selected row fields, untouched.
    assert isinstance(out, StockScreenResult) and out.total == 1
    row = out.results[0]
    assert (row.ticker, row.name, row.sector) == ("NVDA", "NVIDIA Corp", "technology")
    assert (row.market_cap, row.pe_ratio, row.revenue_growth_yoy) == (
        3_400_000_000_000.0,
        55.2,
        94.0,
    )


def test_search_tool_defaults_direction_and_reports_no_matches():
    fake = _FakeSearch(results=[])
    out = SearchStocksTool(fake).run({"sectors": ["energy"]})
    assert fake.kwargs["direction"] is SortDirection.DESC  # unset -> default
    assert isinstance(out, ToolMessage) and "No stocks" in out.message


def test_search_tool_passes_absent_figures_as_none():
    # A thinly covered row (no P/E, no growth) keeps its shape; the gaps are honest nulls.
    fake = _FakeSearch(results=[_row(pe_ratio=None, revenue_growth_yoy=None)])
    out = SearchStocksTool(fake).run({"query": "NVDA"})
    row = out.results[0]
    assert row.pe_ratio is None and row.revenue_growth_yoy is None
    assert row.market_cap == 3_400_000_000_000.0


class _FakeSentiment:
    def __init__(self, *, result=None, error=None) -> None:
        self._result = result
        self._error = error

    def execute(self) -> MarketSentiment:
        if self._error is not None:
            raise self._error
        return self._result


def test_sentiment_tool_renders_both_legs():
    sentiment = MarketSentiment(
        vix=VixSnapshot(as_of=date(2026, 7, 20), value=17.16, previous_close=18.0),
        fear_greed=FearGreedSnapshot(
            score=72.0, as_of=datetime(2026, 7, 20, tzinfo=timezone.utc), rating="Greed"
        ),
    )
    out = MarketSentimentTool(_FakeSentiment(result=sentiment)).run({})
    assert isinstance(out, MarketSentimentResult)
    assert (out.vix.value, out.vix.regime, out.vix.as_of) == (17.16, "normal", date(2026, 7, 20))
    assert (out.fear_greed.score, out.fear_greed.cnn_rating) == (72.0, "Greed")


def test_sentiment_tool_reports_unavailable_instead_of_raising():
    tool = MarketSentimentTool(
        _FakeSentiment(error=StockDataUnavailable("*", "sources down"))
    )
    out = tool.run({})
    assert isinstance(out, ToolMessage) and "unavailable" in out.message.lower()


def test_sentiment_tool_schema_takes_no_arguments():
    schema = MarketSentimentTool(_FakeSentiment()).spec.input_schema
    assert schema["properties"] == {}
