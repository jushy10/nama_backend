"""The charts slice's composition root — framework-free. Every chart read rides
one CandleAdapter — the per-symbol market-routing price provider (US → Alpaca,
CA-suffixed → Yahoo), which is owned by app/endpoints/wiring.py
(get_price_provider, riding the Alpaca missing-keys 503 gate) — so the builders
take the provider as a parameter instead of constructing it here."""

from app.domains.pricing.charts.interfaces import CandleAdapter
from app.domains.pricing.charts.use_cases import (
    GetStockCandles,
    GetStockEma,
    GetStockIndicators,
    GetStockSupportLevels,
    GetStockTrend,
)


def build_get_stock_candles(provider: CandleAdapter) -> GetStockCandles:
    return GetStockCandles(provider)


def build_get_stock_ema(provider: CandleAdapter) -> GetStockEma:
    return GetStockEma(provider)


def build_get_stock_support_levels(provider: CandleAdapter) -> GetStockSupportLevels:
    return GetStockSupportLevels(provider)


def build_get_stock_trend(provider: CandleAdapter) -> GetStockTrend:
    return GetStockTrend(provider)


def build_get_stock_indicators(provider: CandleAdapter) -> GetStockIndicators:
    return GetStockIndicators(provider)
