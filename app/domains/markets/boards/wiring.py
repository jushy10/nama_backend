"""The boards slice's composition root — framework-free. Both board ports are
implemented by the shared Alpaca price-feed singleton, which is owned by
app/endpoints/wiring.py (get_provider, with its missing-keys 503 gate) — so the
builders take the provider as a parameter instead of constructing it here."""

from app.domains.markets.boards.interfaces import (
    MarketOverviewAdapter,
    SectorPerformanceAdapter,
)
from app.domains.markets.boards.use_cases import GetMarketOverview, GetSectorPerformance


def build_get_sector_performance(
    provider: SectorPerformanceAdapter,
) -> GetSectorPerformance:
    return GetSectorPerformance(provider)


def build_get_market_overview(provider: MarketOverviewAdapter) -> GetMarketOverview:
    return GetMarketOverview(provider)
