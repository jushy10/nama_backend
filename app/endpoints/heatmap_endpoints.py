from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.orm import Session

from app.db import get_db
from app.domains.markets.heatmap import wiring
from app.domains.markets.heatmap.api_schemas import HeatMapResponse
from app.domains.markets.heatmap.entities import HeatMapScope
from app.domains.markets.heatmap.use_cases import GetStockHeatMap
from app.endpoints.wiring import get_provider

router = APIRouter(tags=["heatmap"])


def get_stock_heat_map(
    db: Session = Depends(get_db),
    provider=Depends(get_provider),
) -> GetStockHeatMap:
    # Depends shim over the slice's wiring. The universe read is a request-scoped DB read
    # over the shared anchor (no vendor, no key) — structure, tile sizes and the
    # trailing-window colours all come off it in one query. The Alpaca singleton supplies
    # only the live day-change board (BulkQuoteAdapter), inheriting its missing-keys 503 gate.
    return wiring.build_get_stock_heat_map(db, provider)


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
    use_case: GetStockHeatMap = Depends(get_stock_heat_map),
) -> HeatMapResponse:
    try:
        scope = HeatMapScope(index)
    except ValueError as exc:
        raise HTTPException(
            400, f"Unknown index '{index}'. Use 'sp500' or 'nasdaq100'."
        ) from exc
    # The use case swallows a live-quote failure (best-effort colour), so a
    # StockDataUnavailable only escapes on an unexpected data-layer error — translated
    # to 502 by the central handlers in endpoints/error_handlers.py.
    heatmap = use_case.run(scope)
    # A live board that only drifts through the session, backing a homepage widget hit by every
    # visitor: cache briefly so a burst of viewers collapses onto one read without going stale.
    response.headers["Cache-Control"] = "public, max-age=60"
    return HeatMapResponse.from_heat_map(heatmap)
