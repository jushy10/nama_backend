"""Interface Adapter: route a per-symbol price read to the right market's feed.

Alpaca serves US equities in real time; it carries no Canadian (TSX/TSXV) data. So a per-symbol
price view — the ticker card and the charts — dispatches by the symbol's market: a Canadian
suffix (``.TO`` / ``.V`` / ``.NE`` / ``.CN``, Yahoo's convention, the same form the universe
screen stores) routes to the Yahoo feed, everything else to Alpaca.

This is a composition adapter, not a vendor one: it knows *no* vendor, only the two ports it
delegates to (injected), so it stays swappable and testable like everything else. It implements
the per-symbol price ports at once (quote / candles / performance / all-time-high / full
snapshot) because the card, the chart, and the analysis-context use cases inject one provider
that plays several of those roles — the same way the Alpaca singleton does. The batched board/bulk feeds (sectors, market, heat-map quotes)
are US-only and keep using the Alpaca provider directly; only the per-symbol reads route.
"""

from __future__ import annotations

from datetime import datetime

from app.stocks.charts.ports import CandleProvider
from app.stocks.entities import (
    AllTimeHigh,
    CandleSeries,
    Quote,
    Stock,
    StockPerformance,
    Timeframe,
)
from app.stocks.ports import (
    AllTimeHighProvider,
    StockDataProvider,
    StockPerformanceProvider,
    StockQuoteProvider,
)

# Yahoo's suffixes for the Canadian venues — the form the universe screen stores (SHOP.TO). A
# symbol carrying one of these routes to the Yahoo feed; a bare US symbol stays on Alpaca.
CANADIAN_SUFFIXES = (".TO", ".V", ".NE", ".CN")


def is_canadian(symbol: str) -> bool:
    """Whether ``symbol`` is a Canadian listing (by Yahoo suffix). Case-insensitive; a blank or
    non-string symbol is not Canadian (it routes to the US default)."""
    if not isinstance(symbol, str):
        return False
    upper = symbol.upper()
    return any(upper.endswith(suffix) for suffix in CANADIAN_SUFFIXES)


class MarketRoutingPriceProvider(
    StockDataProvider,
    StockQuoteProvider,
    StockPerformanceProvider,
    AllTimeHighProvider,
    CandleProvider,
):
    """Dispatches each per-symbol price read to the US (Alpaca) or CA (Yahoo) feed by suffix.

    ``us`` and ``ca`` each implement all five per-symbol price ports; this picks one per call.
    A US symbol behaves exactly as before (straight to ``us``), so routing is transparent to
    the existing US path; a Canadian-suffixed symbol goes to ``ca``. Implementing
    ``AllTimeHighProvider`` too matters: the analysis context reads the injected provider as
    one, so a router missing it would silently drop the all-time high for *US* symbols as well.
    """

    def __init__(self, *, us, ca) -> None:
        self._us = us
        self._ca = ca

    def _for(self, symbol: str):
        return self._ca if is_canadian(symbol) else self._us

    def get_stock(self, symbol: str) -> Stock:
        return self._for(symbol).get_stock(symbol)

    def get_quote(self, symbol: str) -> Quote:
        return self._for(symbol).get_quote(symbol)

    def get_performance(self, symbol: str) -> StockPerformance:
        return self._for(symbol).get_performance(symbol)

    def get_all_time_high(self, symbol: str) -> AllTimeHigh:
        return self._for(symbol).get_all_time_high(symbol)

    def get_candles(
        self,
        symbol: str,
        timeframe: Timeframe,
        *,
        start: datetime | None,
        end: datetime | None,
    ) -> CandleSeries:
        return self._for(symbol).get_candles(symbol, timeframe, start=start, end=end)
