from fastapi import APIRouter, Depends

from app.adapters.alpaca.price_adapter_impl import PriceAdapterImpl
from app.domains.markets.boards import wiring
from app.domains.markets.boards.api_schemas import SectorBoardResponse
from app.domains.markets.boards.use_cases import GetMarketOverview, GetSectorPerformance
from app.endpoints.wiring import get_provider

router = APIRouter(tags=["market"])


def get_sector_performance(
    # Depends shim over the slice's wiring. The Alpaca provider implements
    # SectorPerformanceAdapter as well (each sector through its proxy ETF snapshot);
    # it's the shared singleton owned by endpoints/wiring.py, so this shim inherits
    # its missing-keys 503 gate. analysis_endpoints reuses the shim to wire the
    # sector AI read off the same board.
    provider: PriceAdapterImpl = Depends(get_provider),
) -> GetSectorPerformance:
    return wiring.build_get_sector_performance(provider)


def get_market_overview(
    # The Alpaca provider implements MarketOverviewAdapter too, reading the S&P
    # 500 and Nasdaq through their proxy ETFs (SPY / QQQ) — same as the sectors.
    # analysis_endpoints reuses this shim to wire the market-summary AI read.
    provider: PriceAdapterImpl = Depends(get_provider),
) -> GetMarketOverview:
    return wiring.build_get_market_overview(provider)


@router.get("/sectors", response_model=SectorBoardResponse)
def get_sectors_endpoint(
    use_case: GetSectorPerformance = Depends(get_sector_performance),
) -> SectorBoardResponse:
    # Domain errors (StockNotFound → 404, StockDataUnavailable → 502) are translated
    # by the central handlers in endpoints/error_handlers.py.
    return SectorBoardResponse.from_sectors(use_case.run())
