"""HTTP API for the market heat map.

``GET /market/heatmap`` — a Finviz-style treemap of an index (default the S&P 500): every stock
a tile sized by market cap, coloured by the day's price change, grouped sector → industry →
stock. Controller + presenter + wiring, the composition-root way, sitting in
``app/stocks/endpoints/`` beside the other read endpoints.

Wiring reuses the composition root's factories from ``router.py``: the universe read is a pure
request-scoped DB read over the shared ``stocks`` anchor (no key), and the batched quote feed is
the same Alpaca singleton every other price view uses (so a missing key is the usual 503). The
map's structure and tile sizes come from the DB; the colours are best-effort live quotes, so a
transient feed hiccup yields an uncoloured-but-correct board rather than a 502.
"""

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.orm import Session

from app.db import get_db
from app.stocks.exceptions import StockDataUnavailable
from app.stocks.heatmap.entities import HeatMap, HeatMapScope
from app.stocks.heatmap.schemas import (
    HeatMapIndustryResponse,
    HeatMapResponse,
    HeatMapSectorResponse,
    HeatMapStockResponse,
)
from app.stocks.heatmap.use_cases import GetStockHeatMap
from app.stocks.router import get_provider
from app.stocks.universe.db_repository import SqlStockSearchRepository

router = APIRouter(tags=["heatmap"])


def get_heatmap_use_case(
    db: Session = Depends(get_db),
    provider=Depends(get_provider),
) -> GetStockHeatMap:
    # The universe read is a request-scoped DB read over the shared anchor (no vendor, no key).
    # The Alpaca singleton (a BulkQuoteProvider) supplies the live day-change board — the same
    # instance every price view uses, so it inherits the missing-keys 503 gate.
    return GetStockHeatMap(SqlStockSearchRepository(db), provider)


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
