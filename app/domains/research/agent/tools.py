from app.domains.research.agent.entities import (
    FearGreedReading,
    MarketSentimentResult,
    StockScreenResult,
    StockScreenRow,
    ToolMessage,
    ToolResult,
    ToolSpec,
    VixReading,
)
from app.domains.research.agent.tool import Tool
from app.domains.shared.exceptions import StockDataUnavailable, StockNotFound
from app.domains.macro.sentiment.use_cases import GetMarketSentiment
from app.domains.listings.universe.entities import (
    MarketCapTier,
    SortDirection,
    StockSort,
)
from app.domains.listings.universe.use_cases import SearchStocks

# Cap on rows returned to the model — keeps the prompt (and token bill) bounded.
_MAX_SCREEN_ROWS = 15

_SEARCH_STOCKS_SPEC = ToolSpec(
    name="search_stocks",
    description=(
        "Screen the US/Canada stock universe, or look up a single company. Use the "
        "'query' field to find one name or ticker (e.g. 'NVDA', 'Apple'); use the "
        "filters to screen a group (e.g. mega-cap technology, sorted by revenue "
        "growth). Returns matching companies with market cap, valuation, and growth "
        "figures. It never returns a stock outside the screened universe."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": (
                    "Free-text match on company name or ticker. Use to look up one "
                    "specific company; omit to screen a group by the filters."
                ),
            },
            "sectors": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Sector names to include (an OR set), e.g. 'technology', "
                    "'healthcare'. Omit to not filter by sector."
                ),
            },
            "market_cap_tiers": {
                "type": "array",
                "items": {
                    "type": "string",
                    "enum": [t.value for t in MarketCapTier],
                },
                "description": (
                    "Company size buckets (OR set): mega (>= $200B), large "
                    "($10-200B), mid ($2-10B), small (< $2B). Omit for every size."
                ),
            },
            "sort": {
                "type": "string",
                "enum": [s.value for s in StockSort],
                "description": (
                    "Rank results by this column: market_cap for size, "
                    "revenue_growth / eps_growth for growth, pe for cheap-on-earnings, "
                    "fcf_yield for cheap-on-cash. Omit for an A-Z browse."
                ),
            },
            "direction": {
                "type": "string",
                "enum": [d.value for d in SortDirection],
                "description": "desc for top/highest, asc for lowest/cheapest. Needs a sort.",
            },
            "in_sp500": {
                "type": "boolean",
                "description": "Set true to restrict to S&P 500 members.",
            },
            "in_nasdaq100": {
                "type": "boolean",
                "description": "Set true to restrict to Nasdaq-100 members.",
            },
            "limit": {
                "type": "integer",
                "description": f"How many rows to return (max {_MAX_SCREEN_ROWS}).",
            },
        },
        "required": [],
    },
)

_MARKET_SENTIMENT_SPEC = ToolSpec(
    name="get_market_sentiment",
    description=(
        "Get the current overall US market sentiment: the VIX (CBOE volatility index, "
        "the 'fear gauge') and the CNN Fear & Greed score (0-100). Use for questions "
        "about the market's mood, risk appetite, or how fearful/greedy the market is. "
        "Takes no arguments."
    ),
    input_schema={"type": "object", "properties": {}, "required": []},
)


class SearchStocksTool(Tool):
    spec = _SEARCH_STOCKS_SPEC

    def __init__(self, search: SearchStocks) -> None:
        self._search = search

    def run(self, arguments: dict) -> ToolResult:
        # The model's arguments are untrusted — a stray value degrades to its default, never raises.
        limit = _positive_int_or_none(arguments.get("limit")) or _MAX_SCREEN_ROWS
        page = self._search.execute(
            query=_string_or_none(arguments.get("query")),
            sectors=_string_tuple(arguments.get("sectors")),
            market_cap_tiers=_enum_tuple(MarketCapTier, arguments.get("market_cap_tiers")),
            sort=_enum_or_none(StockSort, arguments.get("sort")),
            direction=_enum_or_none(SortDirection, arguments.get("direction"))
            or SortDirection.DESC,
            in_sp500=_bool_or_none(arguments.get("in_sp500")),
            in_nasdaq100=_bool_or_none(arguments.get("in_nasdaq100")),
            limit=min(limit, _MAX_SCREEN_ROWS),
        )
        if not page.results:
            return ToolMessage("No stocks in the universe matched that screen.")
        return StockScreenResult(
            total=page.total,
            results=tuple(
                StockScreenRow(
                    ticker=row.ticker,
                    name=row.name,
                    sector=row.sector,
                    market_cap=row.market_cap,
                    pe_ratio=row.pe_ratio,
                    revenue_growth_yoy=row.revenue_growth_yoy,
                )
                for row in page.results
            ),
        )


class MarketSentimentTool(Tool):
    spec = _MARKET_SENTIMENT_SPEC

    def __init__(self, sentiment: GetMarketSentiment) -> None:
        self._sentiment = sentiment

    def run(self, arguments: dict) -> ToolResult:
        try:
            sentiment = self._sentiment.execute()
        except (StockNotFound, StockDataUnavailable) as exc:
            return ToolMessage(f"Market sentiment is unavailable right now: {exc}")
        vix = fear_greed = None
        if sentiment.vix is not None:
            v = sentiment.vix
            vix = VixReading(value=v.value, change=v.change, regime=v.regime, as_of=v.as_of)
        if sentiment.fear_greed is not None:
            fg = sentiment.fear_greed
            fear_greed = FearGreedReading(score=fg.score, label=fg.label, cnn_rating=fg.rating)
        if vix is None and fear_greed is None:
            return ToolMessage("No market-sentiment sources were available.")
        return MarketSentimentResult(vix=vix, fear_greed=fear_greed)


# Coercion helpers: a stray model value degrades to "unset", never raises.


def _string_or_none(value) -> str | None:
    return value.strip() or None if isinstance(value, str) else None


def _string_tuple(value) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(text for item in value if (text := str(item).strip()))


def _enum_tuple(enum_cls, value) -> tuple:
    out = []
    for item in _string_tuple(value):
        member = _enum_or_none(enum_cls, item)
        if member is not None and member not in out:
            out.append(member)
    return tuple(out)


def _enum_or_none(enum_cls, value):
    if not isinstance(value, str):
        return None
    try:
        return enum_cls(value)
    except ValueError:
        return None


def _bool_or_none(value) -> bool | None:
    return value if isinstance(value, bool) else None


def _positive_int_or_none(value) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value > 0 else None


__all__ = ("SearchStocksTool", "MarketSentimentTool")
