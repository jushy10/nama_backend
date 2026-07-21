from fastapi import APIRouter, Depends, HTTPException

from app.stocks.adapters.alpaca_adapter import AlpacaStockDataProvider
from app.stocks.entities import StockPerformance
from app.stocks.exceptions import StockDataUnavailable, StockNotFound
from app.stocks.market.boards.entities import SectorPerformance
from app.stocks.market.boards.schemas import (
    SectorBoardResponse,
    SectorPerformanceResponse,
)
from app.stocks.market.boards.use_cases import GetMarketOverview, GetSectorPerformance
from app.stocks.schemas import StockPerformanceResponse
from app.stocks.wiring import get_provider

router = APIRouter(tags=["market"])


def get_sector_performance(
    # The Alpaca provider implements SectorPerformanceProvider as well, reading
    # each sector through its proxy ETF snapshot.
    provider: AlpacaStockDataProvider = Depends(get_provider),
) -> GetSectorPerformance:
    return GetSectorPerformance(provider)


def get_market_overview(
    # The Alpaca provider implements MarketOverviewProvider too, reading the S&P
    # 500 and Nasdaq through their proxy ETFs (SPY / QQQ) — same as the sectors.
    provider: AlpacaStockDataProvider = Depends(get_provider),
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
