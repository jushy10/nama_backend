from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.orm import Session

from app.db import get_db
from app.stocks.entities import StockPerformance
from app.stocks.exceptions import StockDataUnavailable
from app.stocks.market.heatmap.entities import HeatMap, HeatMapScope
from app.stocks.market.heatmap.schemas import (
    HeatMapIndustryResponse,
    HeatMapResponse,
    HeatMapSectorResponse,
    HeatMapStockResponse,
)
from app.stocks.market.heatmap.use_cases import GetStockHeatMap
from app.stocks.schemas import StockPerformanceResponse
from app.stocks.catalog.universe.db_repository import SqlStockSearchRepository
from app.stocks.wiring import get_provider

router = APIRouter(tags=["heatmap"])


def get_heatmap_use_case(
    db: Session = Depends(get_db),
    provider=Depends(get_provider),
) -> GetStockHeatMap:
    # The universe read is a request-scoped DB read over the shared anchor (no vendor, no key) —
    # structure, tile sizes and the trailing-window colours all come off it in one query. The
    # Alpaca singleton supplies only the live day-change board (BulkQuoteProvider), inheriting
    # its missing-keys 503 gate.
    return GetStockHeatMap(SqlStockSearchRepository(db), provider)


def _present_performance(
    perf: StockPerformance | None,
) -> StockPerformanceResponse | None:
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
