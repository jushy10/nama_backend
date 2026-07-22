from fastapi import APIRouter, Depends, HTTPException

from app.adapters.alpaca.price_adapter_impl import PriceAdapterImpl
from app.domains.shared.entities import StockPerformance
from app.domains.shared.exceptions import StockDataUnavailable, StockNotFound
from app.domains.markets.boards.entities import SectorPerformance
from app.domains.markets.boards.schemas import (
    SectorBoardResponse,
    SectorPerformanceResponse,
)
from app.domains.markets.boards.use_cases import GetMarketOverview, GetSectorPerformance
from app.domains.shared.schemas import StockPerformanceResponse
from app.endpoints.wiring import get_provider

router = APIRouter(tags=["market"])


def get_sector_performance(
    # The Alpaca provider implements SectorPerformanceAdapter as well, reading
    # each sector through its proxy ETF snapshot.
    provider: PriceAdapterImpl = Depends(get_provider),
) -> GetSectorPerformance:
    return GetSectorPerformance(provider)


def get_market_overview(
    # The Alpaca provider implements MarketOverviewAdapter too, reading the S&P
    # 500 and Nasdaq through their proxy ETFs (SPY / QQQ) — same as the sectors.
    provider: PriceAdapterImpl = Depends(get_provider),
) -> GetMarketOverview:
    return GetMarketOverview(provider)


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


def _present_sectors(sectors: list[SectorPerformance]) -> SectorBoardResponse:
    return SectorBoardResponse(
        count=len(sectors),
        sectors=[
            SectorPerformanceResponse(
                sector=s.sector,
                symbol=s.symbol,
                price=s.price,
                change=s.change,
                change_percent=s.change_percent,
                previous_close=s.previous_close,
                as_of=s.as_of,
                performance=_present_performance(s.performance),
            )
            for s in sectors
        ],
    )


@router.get("/sectors", response_model=SectorBoardResponse)
def get_sectors_endpoint(
    use_case: GetSectorPerformance = Depends(get_sector_performance),
) -> SectorBoardResponse:
    try:
        sectors = use_case.execute()
    except StockNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except StockDataUnavailable as exc:
        raise HTTPException(502, str(exc)) from exc
    return _present_sectors(sectors)
