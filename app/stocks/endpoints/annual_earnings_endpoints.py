"""HTTP API for reading a stock's per-year (annual) earnings timeline.

``GET /stocks/{symbol}/earnings/annual`` — the read endpoint for the annual-earnings slice:
a stock's recent reported fiscal years plus its upcoming (estimated) ones, in chronological
order, served from the DB cache over yfinance. Controller + presenter + wiring, the
composition-root way, sitting in ``app/stocks/endpoints/`` beside the cron entrypoint
(``cron_annual_earnings_endpoints``) so all of the slice's HTTP lives in one place.

Wiring mirrors the quarterly read path: the process-singleton live provider is memoized with
``@lru_cache`` while the DB cache is built per request (it needs the request session). A
persistent DB cache (filled lazily on a miss, refreshed out of band by the cron endpoint)
sits in front of Yahoo so the endpoint rarely calls it — Yahoo rate-limits, so the fewer live
hits the better. yfinance needs no credential, so the endpoint is always wired; a cold cache
on a host Yahoo blocks just yields an empty timeline (best-effort).
"""

from functools import lru_cache

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.orm import Session

from app.db import get_db
from app.stocks.adapters.db_cached_annual_earnings_adapter import (
    DbCachedAnnualEarningsProvider,
)
from app.stocks.adapters.yfinance_annual_earnings_adapter import (
    YfinanceAnnualEarningsProvider,
)
from app.stocks.earnings.annual.db_repository import SqlAnnualEarningsRepository
from app.stocks.earnings.annual.entities import (
    AnnualEarnings,
    AnnualEarningsTimeline,
)
from app.stocks.earnings.annual.ports import AnnualEarningsProvider
from app.stocks.earnings.annual.schemas import (
    AnnualEarningsResponse,
    AnnualEarningsYearResponse,
)
from app.stocks.earnings.annual.use_cases import GetAnnualEarnings
from app.stocks.exceptions import StockDataUnavailable, StockNotFound

router = APIRouter(tags=["annual-earnings"])


@lru_cache(maxsize=1)
def _yfinance_annual_earnings_provider() -> AnnualEarningsProvider:
    # One process-singleton live provider (no key, no connection pool to share); the DB cache
    # that wraps it is built per request, since it needs the request session.
    return YfinanceAnnualEarningsProvider()


def get_annual_earnings_provider(
    db: Session = Depends(get_db),
) -> AnnualEarningsProvider:
    # A persistent DB cache (refreshed out of band by the annual-earnings cron endpoint +
    # lazily on a miss) sits in front of Yahoo so the endpoint rarely calls it, and it serves
    # stored rows without a live round-trip. yfinance needs no key, so this is always wired.
    return DbCachedAnnualEarningsProvider(
        _yfinance_annual_earnings_provider(), SqlAnnualEarningsRepository(db)
    )


def get_annual_earnings_use_case(
    provider: AnnualEarningsProvider = Depends(get_annual_earnings_provider),
) -> GetAnnualEarnings:
    return GetAnnualEarnings(provider)


def _present_year(year: AnnualEarnings) -> AnnualEarningsYearResponse:
    return AnnualEarningsYearResponse(
        fiscal_year=year.fiscal_year,
        period_end=year.period_end,
        eps_actual=year.eps_actual,
        eps_estimate=year.eps_estimate,
        revenue_actual=year.revenue_actual,
        revenue_estimate=year.revenue_estimate,
        net_income=year.net_income,
        eps_actual_consensus=year.eps_actual_consensus,
        is_reported=year.is_reported,
    )


def _present(timeline: AnnualEarningsTimeline) -> AnnualEarningsResponse:
    """Presenter: annual-earnings timeline entity -> HTTP response DTO."""
    return AnnualEarningsResponse(
        symbol=timeline.symbol,
        count=len(timeline.years),
        reported_count=len(timeline.past),
        upcoming_count=len(timeline.future),
        years=[_present_year(y) for y in timeline.years],
    )


@router.get("/stocks/{symbol}/earnings/annual", response_model=AnnualEarningsResponse)
def get_annual_earnings_endpoint(
    symbol: str,
    response: Response,
    use_case: GetAnnualEarnings = Depends(get_annual_earnings_use_case),
) -> AnnualEarningsResponse:
    try:
        timeline = use_case.execute(symbol)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except StockNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except StockDataUnavailable as exc:
        raise HTTPException(502, str(exc)) from exc
    # Reported years move slowly (and are served from the DB cache), so cache briefly: a burst
    # of viewers collapses onto one response.
    response.headers["Cache-Control"] = "public, max-age=300"
    return _present(timeline)
