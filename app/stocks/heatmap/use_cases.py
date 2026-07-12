"""Application use case for the heat-map slice.

``GetStockHeatMap`` assembles the market treemap: read the screened universe for the chosen
index (structure + tile size, straight off the ``stocks`` anchor), fetch every member's live
day-change in one batched call (the day tile's colour) and their trailing-window returns in a
handful more (the timeframe selector's colours), and fold them into the nested
:class:`HeatMap`. Pure orchestration over three ports — the universe read
(``StockSearchRepository``, reused from the universe slice), the batched quote feed
(``BulkQuoteProvider``) and the batched performance feed (``BulkPerformanceProvider``) — so it
runs offline against hand-written fakes and knows nothing of SQLAlchemy, Alpaca, or HTTP.

Three data legs, two stances:
- The **universe read is primary** — it's the map's skeleton. No members (a not-yet-synced
  index) yields an *empty* map, which is a valid 200, not an error.
- The **quotes and trailing performance are best-effort** — they only colour tiles. A live-feed
  failure on either is swallowed so the map still renders (the structure and sizes come from the
  DB) rather than 502-ing over a transient Alpaca hiccup: a quote failure leaves the day tiles
  uncoloured, a performance failure leaves the longer timeframes blank. A missing API key is
  still a hard 503 in the wiring (no keys → no colour at all), consistent with every price view.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor

from app.stocks.entities import StockPerformance
from app.stocks.exceptions import StockDataUnavailable
from app.stocks.heatmap.entities import HeatMap, HeatMapRow, HeatMapScope
from app.stocks.ports import BulkPerformanceProvider, BulkQuoteProvider
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
        performance: BulkPerformanceProvider,
    ) -> None:
        self._repository = repository
        self._quotes = quotes
        self._performance = performance

    def execute(self, scope: HeatMapScope) -> HeatMap:
        """Read the index's screened members, colour them with live day-change and trailing
        performance, and fold into the treemap.

        The universe read is primary (an unsynced index → an empty map, a valid 200). Both the
        batched quote call and the batched performance call are best-effort: a hard feed failure
        on either is caught and the map is built without those colours rather than failing — the
        structure and tile sizes come from the DB, so a gray-but-correct board still beats a 502.
        """
        page = self._repository.search(self._criteria(scope))
        rows = tuple(
            HeatMapRow(
                ticker=r.ticker,
                name=r.name,
                sector=r.sector,
                industry=r.industry,
                market_cap=r.market_cap,
            )
            for r in page.results
            if r.market_cap is not None
        )
        tickers = tuple(row.ticker for row in rows)
        change_by_ticker, performance_by_ticker = self._colours(tickers)
        return HeatMap.build(scope, rows, change_by_ticker, performance_by_ticker)

    def _colours(
        self, tickers
    ) -> tuple[dict[str, float | None], dict[str, StockPerformance]]:
        """The board's two colour legs — day-change and trailing performance — fetched
        **concurrently**.

        They're independent Alpaca reads (no shared DB session — the universe read already
        completed on the calling thread), and the trailing-performance leg is far the heavier
        (a year of daily bars for the whole index) while the quote leg is a handful of light
        snapshots, so running them in parallel makes a cache-cold board cost ``max`` of the two
        rather than their sum. Both helpers swallow their own feed failures to an empty map, so
        neither future raises — a plain fan-out over ``concurrent.futures``, the same I/O
        orchestration ``GetStockInfo`` uses, with no framework or vendor leaking into the core."""
        if not tickers:
            return {}, {}
        with ThreadPoolExecutor(max_workers=2) as pool:
            change_future = pool.submit(self._change_by_ticker, tickers)
            performance_future = pool.submit(self._performance_by_ticker, tickers)
            return change_future.result(), performance_future.result()

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

    def _performance_by_ticker(self, tickers) -> dict[str, StockPerformance]:
        """Each member's trailing-window returns, keyed by ticker — best-effort.

        One handful of batched daily-bars calls (chunked in the provider). A hard feed failure is
        swallowed to an empty map (every tile's longer timeframes blank) so the day-move board
        still renders; a symbol simply missing history is already absent (the provider's per-symbol
        best-effort). Any individual window may itself be ``None`` (not enough history), which the
        entity treats as a blank tile for that timeframe."""
        symbols = tuple(tickers)
        if not symbols:
            return {}
        try:
            return self._performance.get_bulk_performance(symbols)
        except StockDataUnavailable:
            logger.warning(
                "heat map: trailing performance unavailable; timeframe windows left blank"
            )
            return {}
