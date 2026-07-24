"""The yields slice's composition root — framework-free. Both sources are keyless
(Treasury.gov + FRED), so the providers are always-constructable process singletons
and there is no missing-key gate."""

from functools import lru_cache

from app.adapters.fred.yield_history_adapter_impl import YieldHistoryAdapterImpl
from app.adapters.treasury.yield_curve_adapter_impl import YieldCurveAdapterImpl
from app.domains.macro.yields.interfaces import YieldCurveAdapter, YieldHistoryAdapter
from app.domains.macro.yields.use_cases import GetYieldCurve, GetYieldHistory


@lru_cache(maxsize=1)
def get_yield_curve_provider() -> YieldCurveAdapter:
    # Keyless (Treasury.gov), so no 503 gate — unlike the Alpaca price feed.
    return YieldCurveAdapterImpl()


@lru_cache(maxsize=1)
def get_yield_history_provider() -> YieldHistoryAdapter:
    # Keyless (FRED), so no 503 gate.
    return YieldHistoryAdapterImpl()


def build_get_yield_curve() -> GetYieldCurve:
    return GetYieldCurve(get_yield_curve_provider())


def build_get_yield_history() -> GetYieldHistory:
    return GetYieldHistory(get_yield_history_provider())
