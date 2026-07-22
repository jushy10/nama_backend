from dataclasses import asdict, dataclass

from app.stocks.ai.agent.entities import ToolSpec
from app.stocks.ai.agent.interfaces import Tool
from app.stocks.exceptions import StockDataUnavailable, StockNotFound
from app.stocks.market.sentiment.use_cases import GetMarketSentiment
from app.stocks.catalog.universe.entities import (
    MarketCapTier,
    SortDirection,
    StockSearchResult,
    StockSort,
)
from app.stocks.catalog.universe.use_cases import SearchStocks

# The cap on rows a single screen returns to the model — enough context to compare a handful of
# names without flooding the prompt (and the token bill) with a whole page.
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
    def __init__(self, search: SearchStocks) -> None:
        self._search = search

    @property
    def spec(self) -> ToolSpec:
        return _SEARCH_STOCKS_SPEC

    def run(self, arguments: dict) -> str:
        page = self._search.execute(**asdict(_SearchArgs.from_model(arguments)))
        if not page.results:
            return "No stocks in the universe matched that screen."
        rows = "\n".join(_format_row(row) for row in page.results)
        return f"{page.total} match(es); showing {len(page.results)}:\n{rows}"


class MarketSentimentTool(Tool):
    def __init__(self, sentiment: GetMarketSentiment) -> None:
        self._sentiment = sentiment

    @property
    def spec(self) -> ToolSpec:
        return _MARKET_SENTIMENT_SPEC

    def run(self, arguments: dict) -> str:
        try:
            sentiment = self._sentiment.execute()
        except (StockNotFound, StockDataUnavailable) as exc:
            return f"Market sentiment is unavailable right now: {exc}"
        parts: list[str] = []
        if sentiment.vix is not None:
            vix = sentiment.vix
            change = "" if vix.change is None else f" ({vix.change:+.2f})"
            parts.append(
                f"VIX: {vix.value:.2f}{change}, regime '{vix.regime}' (as of {vix.as_of})."
            )
        if sentiment.fear_greed is not None:
            fg = sentiment.fear_greed
            parts.append(
                f"Fear & Greed: {fg.score:.0f}/100 — '{fg.label}' (CNN rating: {fg.rating})."
            )
        return (
            " ".join(parts) if parts else "No market-sentiment sources were available."
        )


@dataclass(frozen=True)
class _SearchArgs:
    """The search_stocks tool's arguments, coerced from the model's raw (untrusted) input.

    Field names mirror SearchStocks.execute's keyword arguments, so run() can splat
    asdict(...) straight into it. A stray model value degrades to its default, never raises.
    """

    query: str | None = None
    sectors: tuple[str, ...] = ()
    market_cap_tiers: tuple[MarketCapTier, ...] = ()
    sort: StockSort | None = None
    direction: SortDirection = SortDirection.DESC
    in_sp500: bool | None = None
    in_nasdaq100: bool | None = None
    limit: int = _MAX_SCREEN_ROWS

    @classmethod
    def from_model(cls, raw: dict) -> "_SearchArgs":
        limit = _positive_int_or_none(raw.get("limit")) or _MAX_SCREEN_ROWS
        return cls(
            query=_string_or_none(raw.get("query")),
            sectors=_string_tuple(raw.get("sectors")),
            market_cap_tiers=_enum_tuple(MarketCapTier, raw.get("market_cap_tiers")),
            sort=_enum_or_none(StockSort, raw.get("sort")),
            direction=_enum_or_none(SortDirection, raw.get("direction"))
            or SortDirection.DESC,
            in_sp500=_bool_or_none(raw.get("in_sp500")),
            in_nasdaq100=_bool_or_none(raw.get("in_nasdaq100")),
            limit=min(limit, _MAX_SCREEN_ROWS),
        )


def _format_row(row: StockSearchResult) -> str:
    bits: list[str] = [f"- {row.ticker}"]
    if row.name:
        bits.append(f"({row.name})")
    facts: list[str] = []
    if row.sector:
        facts.append(row.sector)
    if row.market_cap is not None:
        facts.append(f"mktcap {_human_usd(row.market_cap)}")
    if row.pe_ratio is not None:
        facts.append(f"P/E {row.pe_ratio:.1f}")
    if row.revenue_growth_yoy is not None:
        facts.append(f"rev growth {row.revenue_growth_yoy:+.1f}%")
    if facts:
        bits.append("— " + ", ".join(facts))
    return " ".join(bits)


def _human_usd(value: float) -> str:
    if value >= 1_000_000_000_000:
        return f"${value / 1_000_000_000_000:.1f}T"
    if value >= 1_000_000_000:
        return f"${value / 1_000_000_000:.1f}B"
    if value >= 1_000_000:
        return f"${value / 1_000_000:.0f}M"
    return f"${value:.0f}"


# --- Defensive argument coercion (a stray model value degrades to "unset", never raises) ------


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
