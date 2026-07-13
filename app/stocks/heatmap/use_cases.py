"""Application use case for the heat-map slice.

``GetStockHeatMap`` assembles the market treemap: read the screened universe for the chosen
index (structure, tile size **and** the trailing-window returns — all straight off the
``stocks`` anchor in one DB read) and fetch every member's live day-change in one batched call
(the day tile's colour), then fold them into the nested :class:`HeatMap`. Pure orchestration
over two ports — the universe read (``StockSearchRepository``, reused from the universe slice)
and the batched quote feed (``BulkQuoteProvider``) — so it runs offline against hand-written
fakes and knows nothing of SQLAlchemy, Alpaca, or HTTP.

Two data legs, two stances:
- The **universe read is primary** — it's the map's skeleton *and* its timeframe colours. The
  trailing windows (1W…1Y, YTD) ride on each search result, materialized on the anchor by the
  performance sync, so the read path no longer recomputes a year of daily bars for the whole
  index (that live leg — its heaviest — moved to the ``sync-stock-performance`` cron). No
  members (a not-yet-synced index) yields an *empty* map, a valid 200; a member the perf sync
  hasn't reached yet simply carries blank timeframe windows (its day tile still renders).
- The **day-change quotes are best-effort** — they only colour the default (today) tile. A
  live-feed failure is swallowed so the map still renders (structure, sizes and the trailing
  windows all come from the DB) rather than 502-ing over a transient Alpaca hiccup: a quote
  failure just leaves the day tiles uncoloured. A missing API key is still a hard 503 in the
  wiring (no keys → no day colour), consistent with every price view.
"""

from __future__ import annotations

import logging

from app.stocks.exceptions import StockDataUnavailable
from app.stocks.heatmap.entities import HeatMap, HeatMapRow, HeatMapScope
from app.stocks.ports import BulkQuoteProvider
from app.stocks.universe.entities import (
    SortDirection,
    StockSearchCriteria,
    StockSort,
)
from app.stocks.universe.repository import StockSearchRepository

logger = logging.getLogger(__name__)


class GetStockHeatMap:
    """Build the sector→industry→stock heat map for one index (``GET /market/heatmap``)."""

    # Page ceiling for the universe read — comfortably above the S&P 500's ~500 members (and
    # far above the Nasdaq-100's ~100), so the whole index lands in one read. A treemap of
    # more tiles than this would be unreadable anyway.
    _MAX_TILES = 600

    def __init__(
        self,
        repository: StockSearchRepository,
        quotes: BulkQuoteProvider,
    ) -> None:
        self._repository = repository
        self._quotes = quotes

    def execute(self, scope: HeatMapScope) -> HeatMap:
        """Read the index's screened members (with their stored trailing performance), colour
        them with the live day-change, and fold into the treemap.

        The universe read is primary — it carries the structure, tile sizes *and* the trailing
        windows, so an unsynced index is an empty map (a valid 200). The batched quote call is
        best-effort: a hard feed failure is caught and the map is built without the day colour
        rather than failing — a gray-but-correct board beats a 502.
        """
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
        """The whole chosen index, biggest cap first — the board's natural reading order (the
        entity re-sorts by group, but a cap-desc read keeps a truncated board's tail sensible).
        Filters on the one index flag; every other axis is left open."""
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
        """Each member's day-change percent, keyed by ticker — best-effort.

        One batched quote call. A hard feed failure is swallowed to an empty map (every tile
        uncoloured) so the board still renders; a symbol simply missing from the feed is already
        absent (the provider's per-symbol best-effort). ``change_percent`` may itself be ``None``
        (no previous close), which the entity treats the same as a missing quote."""
        symbols = tuple(tickers)
        if not symbols:
            return {}
        try:
            quotes = self._quotes.get_quotes(symbols)
        except StockDataUnavailable:
            logger.warning("heat map: live quotes unavailable; rendering uncoloured board")
            return {}
        return {symbol: quote.change_percent for symbol, quote in quotes.items()}
