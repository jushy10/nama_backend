"""The heatmap slice's composition root — framework-free. The universe read is a
request-scoped DB read over the shared anchor (no vendor, no key), so the builder
constructs the db-backed search repository from the Session itself. The live
day-change board (BulkQuoteAdapter) is the shared Alpaca price-feed singleton,
owned by app/endpoints/wiring.py (get_provider, with its missing-keys 503 gate) —
so the builder takes it as a parameter instead of constructing it here."""

from sqlalchemy.orm import Session

from app.domains.listings.universe.repository_adapter_impl import (
    StockSearchRepositoryAdapterImpl,
)
from app.domains.markets.heatmap.use_cases import GetStockHeatMap
from app.domains.shared.interfaces import BulkQuoteAdapter


def build_get_stock_heat_map(db: Session, quotes: BulkQuoteAdapter) -> GetStockHeatMap:
    return GetStockHeatMap(StockSearchRepositoryAdapterImpl(db), quotes)
