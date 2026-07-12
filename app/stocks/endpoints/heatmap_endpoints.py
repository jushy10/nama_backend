"""HTTP API for the market heat map.

``GET /market/heatmap`` — a Finviz-style treemap of an index (default the S&P 500): every stock
a tile sized by market cap, coloured by the day's price change, grouped sector → industry →
stock. Controller + presenter + wiring, the composition-root way, sitting in
``app/stocks/endpoints/`` beside the other read endpoints.

Wiring reuses the shared factories from ``wiring.py``: the universe read is a pure
request-scoped DB read over the shared ``stocks`` anchor (no key), and the batched quote feed is
the same Alpaca singleton every other price view uses (so a missing key is the usual 503). The
map's structure and tile sizes come from the DB; the colours are best-effort live quotes, so a
transient feed hiccup yields an uncoloured-but-correct board rather than a 502.
"""

import os
from functools import lru_cache

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.orm import Session

from app.db import get_db
from app.stocks.caching_bulk_performance_provider import CachingBulkPerformanceProvider
from app.stocks.entities import StockPerformance
from app.stocks.exceptions import StockDataUnavailable
from app.stocks.heatmap.entities import HeatMap, HeatMapScope
from app.stocks.heatmap.schemas import (
    HeatMapIndustryResponse,
    HeatMapResponse,
    HeatMapSectorResponse,
    HeatMapStockResponse,
)
from app.stocks.heatmap.use_cases import GetStockHeatMap
from app.stocks.ports import BulkPerformanceProvider
from app.stocks.schemas import StockPerformanceResponse
from app.stocks.universe.db_repository import SqlStockSearchRepository
from app.stocks.wiring import get_provider

router = APIRouter(tags=["heatmap"])


@lru_cache(maxsize=1)
def get_heatmap_performance_provider() -> BulkPerformanceProvider:
    # The trailing-window leg is the board's heaviest read (a year of daily bars for a whole
    # index), so it's served through a TTL cache in front of the Alpaca singleton — a singleton
    # itself, so the cache persists across requests and a burst of viewers collapses onto one
    # fetch per window. The window is env-tunable (HEATMAP_PERFORMANCE_CACHE_TTL_SECONDS); the
    # trailing figures are stable over hours, so the default is comfortably long. Wrapping the
    # same get_provider() singleton keeps the missing-keys 503 gate.
    ttl = float(
        os.environ.get(
            "HEATMAP_PERFORMANCE_CACHE_TTL_SECONDS",
            CachingBulkPerformanceProvider._DEFAULT_TTL_SECONDS,
        )
    )
    return CachingBulkPerformanceProvider(get_provider(), ttl_seconds=ttl)


def get_heatmap_use_case(
    db: Session = Depends(get_db),
    provider=Depends(get_provider),
    performance: BulkPerformanceProvider = Depends(get_heatmap_performance_provider),
) -> GetStockHeatMap:
    # The universe read is a request-scoped DB read over the shared anchor (no vendor, no key).
    # The Alpaca singleton supplies the live day-change board (BulkQuoteProvider); the batched
    # trailing-window returns come through a TTL cache in front of that same singleton, so it
    # inherits the missing-keys 503 gate while sparing the heavy bars fetch on repeat boards.
    return GetStockHeatMap(SqlStockSearchRepository(db), provider, performance)


def _present_performance(
    perf: StockPerformance | None,
) -> StockPerformanceResponse | None:
    """Presenter: the shared trailing-window value object -> its HTTP DTO (``null`` when a
    tile carried no history)."""
    if perf is None:
        return None
    return StockPerformanceResponse(
        one_week=perf.one_week,
        one_month=perf.one_month,
        three_month=perf.three_month,
        six_month=perf.six_month,
        ytd=perf.ytd,
        one_year=perf.one_year,
    )


def _present(heatmap: HeatMap) -> HeatMapResponse:
    """Presenter: the heat-map entity tree -> HTTP response DTO."""
    return HeatMapResponse(
        scope=heatmap.scope.value,
        count=heatmap.cell_count,
        sectors=[
            HeatMapSectorResponse(
                sector=sector.sector,
                market_cap=sector.market_cap,
                industries=[
                    HeatMapIndustryResponse(
                        industry=industry.industry,
                        market_cap=industry.market_cap,
                        stocks=[
                            HeatMapStockResponse(
                                ticker=cell.ticker,
                                name=cell.name,
                                market_cap=cell.market_cap,
                                change_percent=cell.change_percent,
                                performance=_present_performance(cell.performance),
                            )
                            for cell in industry.cells
                        ],
                    )
                    for industry in sector.industries
                ],
            )
            for sector in heatmap.sectors
        ],
    )


@router.get("/market/heatmap", response_model=HeatMapResponse)
def get_heatmap_endpoint(
    response: Response,
    index: str = Query(
        HeatMapScope.SP500.value,
        description=(
            "Which index to map: 'sp500' (default) or 'nasdaq100'. Members are read off the "
            "in_sp500 / in_nasdaq100 flags on the stocks anchor."
        ),
    ),
    use_case: GetStockHeatMap = Depends(get_heatmap_use_case),
) -> HeatMapResponse:
    try:
        scope = HeatMapScope(index)
    except ValueError as exc:
        raise HTTPException(
            400, f"Unknown index '{index}'. Use 'sp500' or 'nasdaq100'."
        ) from exc
    try:
        heatmap = use_case.execute(scope)
    except StockDataUnavailable as exc:
        # The use case swallows a live-quote failure (best-effort colour), so this only fires
        # on an unexpected data-layer error — still mapped to the slice's 502 for uniformity.
        raise HTTPException(502, str(exc)) from exc
    # A live board that only drifts through the session, backing a homepage widget hit by every
    # visitor: cache briefly so a burst of viewers collapses onto one read without going stale.
    response.headers["Cache-Control"] = "public, max-age=60"
    return _present(heatmap)
