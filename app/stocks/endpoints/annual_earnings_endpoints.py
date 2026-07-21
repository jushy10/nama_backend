from functools import lru_cache

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.orm import Session

from app.db import get_db
from app.stocks.adapters.db.db_cached_annual_earnings_adapter_impl import (
    AnnualEarningsAdapterImpl as DbCachedAnnualEarningsAdapterImpl,
)
from app.stocks.adapters.yfinance.annual_earnings_adapter_impl import (
    AnnualEarningsAdapterImpl as YfinanceAnnualEarningsAdapterImpl,
)
from app.stocks.company.earnings.annual.annual_earnings_repository_adapter_impl import AnnualEarningsRepositoryAdapterImpl
from app.stocks.company.earnings.annual.entities import (
    AnnualEarnings,
    AnnualEarningsTimeline,
)
from app.stocks.company.earnings.annual.interfaces import AnnualEarningsAdapter
from app.stocks.company.earnings.annual.schemas import (
    AnnualEarningsResponse,
    AnnualEarningsYearResponse,
)
from app.stocks.company.earnings.annual.use_cases import GetAnnualEarnings
from app.stocks.exceptions import StockDataUnavailable, StockNotFound

router = APIRouter(tags=["annual-earnings"])


@lru_cache(maxsize=1)
def _yfinance_annual_earnings_provider() -> AnnualEarningsAdapter:
    # One process-singleton live provider (no key, no connection pool to share); the DB cache
    # that wraps it is built per request, since it needs the request session.
    return YfinanceAnnualEarningsAdapterImpl()


def get_annual_earnings_provider(
    db: Session = Depends(get_db),
) -> AnnualEarningsAdapter:
    # A persistent DB cache (refreshed out of band by the annual-earnings cron endpoint +
    # lazily on a miss) sits in front of Yahoo so the endpoint rarely calls it, and it serves
    # stored rows without a live round-trip. yfinance needs no key, so this is always wired.
    return DbCachedAnnualEarningsAdapterImpl(
        _yfinance_annual_earnings_provider(), AnnualEarningsRepositoryAdapterImpl(db)
    )


def get_annual_earnings_use_case(
    provider: AnnualEarningsAdapter = Depends(get_annual_earnings_provider),
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
        fcf_per_share=year.fcf_per_share,
        ocf_per_share=year.ocf_per_share,
        is_reported=year.is_reported,
    )


def _present(timeline: AnnualEarningsTimeline) -> AnnualEarningsResponse:
    return AnnualEarningsResponse(
        symbol=timeline.symbol,
        count=len(timeline.years),
        reported_count=len(timeline.past),
        upcoming_count=len(timeline.future),
        revenue_growth_yoy=timeline.latest_revenue_growth_yoy,
        eps_growth_yoy=timeline.latest_eps_growth_yoy,
        fcf_growth_yoy=timeline.latest_fcf_growth_yoy,
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
