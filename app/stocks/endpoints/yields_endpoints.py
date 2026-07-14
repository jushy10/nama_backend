"""HTTP API for the US Treasury yield curve.

``GET /market/yield-curve`` — the current par-yield curve across every maturity
(the snapshot), and ``GET /market/yield-history`` — the 2Y/10Y yields over time
(the two-line series whose crossover marks an inversion). Controller + presenter
+ wiring, the composition-root way, sitting in ``app/stocks/endpoints/`` beside
the other market reads. Both are live-per-request off keyless sources (Treasury
for the curve, FRED for the history), so there's no table, no cron, and — like
the ``/sectors`` board — the wiring factories are local, keyless, and un-gated.
"""

from functools import lru_cache

from fastapi import APIRouter, Depends, HTTPException, Query

from app.stocks.adapters.fred_yield_history_adapter import FredYieldHistoryProvider
from app.stocks.adapters.treasury_yield_curve_adapter import TreasuryYieldCurveProvider
from app.stocks.exceptions import StockDataUnavailable, StockNotFound
from app.stocks.yields.entities import YieldCurve, YieldHistory
from app.stocks.yields.ports import YieldCurveProvider, YieldHistoryProvider
from app.stocks.yields.schemas import (
    YieldCurveResponse,
    YieldHistoryResponse,
    YieldObservationResponse,
    YieldSeriesResponse,
    YieldTenorResponse,
)
from app.stocks.yields.use_cases import GetYieldCurve, GetYieldHistory

router = APIRouter(tags=["market"])


@lru_cache(maxsize=1)
def get_yield_curve_provider() -> YieldCurveProvider:
    # Keyless (Treasury.gov), so no 503 gate — unlike the Alpaca price feed.
    return TreasuryYieldCurveProvider()


@lru_cache(maxsize=1)
def get_yield_history_provider() -> YieldHistoryProvider:
    # Keyless (FRED), so no 503 gate.
    return FredYieldHistoryProvider()


def get_yield_curve(
    provider: YieldCurveProvider = Depends(get_yield_curve_provider),
) -> GetYieldCurve:
    return GetYieldCurve(provider)


def get_yield_history(
    provider: YieldHistoryProvider = Depends(get_yield_history_provider),
) -> GetYieldHistory:
    return GetYieldHistory(provider)


def _present_curve(curve: YieldCurve) -> YieldCurveResponse:
    """Presenter: the curve entity -> HTTP response DTO."""
    return YieldCurveResponse(
        as_of=curve.as_of,
        two_year=curve.two_year,
        ten_year=curve.ten_year,
        spread_2s10s=curve.spread_2s10s,
        is_inverted=curve.is_inverted,
        count=len(curve.tenors),
        tenors=[
            YieldTenorResponse(label=t.label, months=t.months, rate=t.rate)
            for t in curve.tenors
        ],
    )


def _present_history(history: YieldHistory) -> YieldHistoryResponse:
    """Presenter: the history entity -> HTTP response DTO."""
    return YieldHistoryResponse(
        latest_spread=history.latest_spread,
        is_inverted=history.is_inverted,
        series=[
            YieldSeriesResponse(
                label=s.label,
                observations=[
                    YieldObservationResponse(date=o.on, rate=o.rate)
                    for o in s.observations
                ],
            )
            for s in history.series
        ],
        spread=[
            YieldObservationResponse(date=o.on, rate=o.rate) for o in history.spread
        ],
    )


@router.get("/market/yield-curve", response_model=YieldCurveResponse)
def get_yield_curve_endpoint(
    use_case: GetYieldCurve = Depends(get_yield_curve),
) -> YieldCurveResponse:
    try:
        curve = use_case.execute()
    except StockNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except StockDataUnavailable as exc:
        raise HTTPException(502, str(exc)) from exc
    return _present_curve(curve)


@router.get("/market/yield-history", response_model=YieldHistoryResponse)
def get_yield_history_endpoint(
    lookback_days: int = Query(
        1095, ge=1, le=10950, description="Trailing window in days (default ~3y)."
    ),
    use_case: GetYieldHistory = Depends(get_yield_history),
) -> YieldHistoryResponse:
    try:
        history = use_case.execute(lookback_days)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except StockNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except StockDataUnavailable as exc:
        raise HTTPException(502, str(exc)) from exc
    return _present_history(history)
