from __future__ import annotations

import logging

from app.domains.shared.exceptions import StockDataUnavailable
from app.domains.markets.heatmap.entities import HeatMap, HeatMapRow, HeatMapScope
from app.domains.shared.interfaces import BulkQuoteAdapter
from app.domains.listings.universe.entities import (
    SortDirection,
    StockSearchCriteria,
    StockSort,
)
from app.domains.listings.universe.interfaces import StockSearchRepositoryAdapter

logger = logging.getLogger(__name__)


class GetStockHeatMap:
    # Page ceiling for the universe read — comfortably above the S&P 500's ~500 members (and
    # far above the Nasdaq-100's ~100), so the whole index lands in one read. A treemap of
    # more tiles than this would be unreadable anyway.
    _MAX_TILES = 600

    def __init__(
        self,
        repository: StockSearchRepositoryAdapter,
        quotes: BulkQuoteAdapter,
    ) -> None:
        self._repository = repository
        self._quotes = quotes

    def run(self, scope: HeatMapScope) -> HeatMap:
        page = self._repository.search(self._criteria(scope))
        results = tuple(r for r in page.results if r.market_cap is not None)
        rows = tuple(
            HeatMapRow(
                ticker=r.ticker,
                name=r.name,
                sector=r.sector,
                industry=r.industry,
                market_cap=r.market_cap,
            )
            for r in results
        )
        # Trailing windows come straight off the search rows (materialized on the anchor by the
        # performance sync); only rows the sync has reached carry a block.
        performance_by_ticker = {
            r.ticker: r.performance for r in results if r.performance is not None
        }
        tickers = tuple(row.ticker for row in rows)
        change_by_ticker = self._change_by_ticker(tickers)
        return HeatMap.build(scope, rows, change_by_ticker, performance_by_ticker)

    def _criteria(self, scope: HeatMapScope) -> StockSearchCriteria:
        return StockSearchCriteria(
            query=None,
            sectors=(),
            industries=(),
            in_sp500=scope is HeatMapScope.SP500 or None,
            in_nasdaq100=scope is HeatMapScope.NASDAQ100 or None,
            market_cap_tiers=(),
            sort=StockSort.MARKET_CAP,
            direction=SortDirection.DESC,
            limit=self._MAX_TILES,
            offset=0,
        )

    def _change_by_ticker(self, tickers) -> dict[str, float | None]:
        symbols = tuple(tickers)
        if not symbols:
            return {}
        try:
            quotes = self._quotes.get_quotes(symbols)
        except StockDataUnavailable:
            logger.warning("heat map: live quotes unavailable; rendering uncoloured board")
            return {}
        return {symbol: quote.change_percent for symbol, quote in quotes.items()}
