from fastapi import APIRouter, Depends, HTTPException, Query

from app.domains.macro.yields import wiring
from app.domains.macro.yields.api_schemas import YieldCurveResponse, YieldHistoryResponse
from app.domains.macro.yields.use_cases import GetYieldCurve, GetYieldHistory

router = APIRouter(tags=["market"])


def get_get_yield_curve() -> GetYieldCurve:
    # Depends shim over the slice's wiring — exists for the dependency_overrides
    # test seam, nothing more (both sources are keyless, so no 503 gate).
    return wiring.build_get_yield_curve()


def get_get_yield_history() -> GetYieldHistory:
    return wiring.build_get_yield_history()


@router.get("/market/yield-curve", response_model=YieldCurveResponse)
def get_yield_curve_endpoint(
    use_case: GetYieldCurve = Depends(get_get_yield_curve),
) -> YieldCurveResponse:
    # Domain errors (StockNotFound → 404, StockDataUnavailable → 502) are translated
    # by the central handlers in endpoints/error_handlers.py.
    return YieldCurveResponse.from_curve(use_case.run())


@router.get("/market/yield-history", response_model=YieldHistoryResponse)
def get_yield_history_endpoint(
    lookback_days: int = Query(
        1095, ge=1, le=10950, description="Trailing window in days (default ~3y)."
    ),
    use_case: GetYieldHistory = Depends(get_get_yield_history),
) -> YieldHistoryResponse:
    try:
        history = use_case.run(lookback_days)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return YieldHistoryResponse.from_history(history)
