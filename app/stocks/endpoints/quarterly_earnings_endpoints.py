from functools import lru_cache

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.orm import Session

from app.db import get_db
from app.stocks.adapters.db_cached_quarterly_earnings_adapter import (
    DbCachedQuarterlyEarningsProvider,
)
from app.stocks.adapters.yfinance_quarterly_earnings_adapter import (
    YfinanceQuarterlyEarningsProvider,
)
from app.stocks.earnings.quarterly.db_repository import SqlQuarterlyEarningsRepository
from app.stocks.earnings.quarterly.entities import (
    QuarterlyEarnings,
    QuarterlyEarningsTimeline,
)
from app.stocks.earnings.quarterly.ports import QuarterlyEarningsProvider
from app.stocks.earnings.quarterly.schemas import (
    QuarterlyEarningsQuarterResponse,
    QuarterlyEarningsResponse,
)
from app.stocks.earnings.quarterly.use_cases import GetQuarterlyEarnings
from app.stocks.exceptions import StockDataUnavailable, StockNotFound

router = APIRouter(tags=["quarterly-earnings"])


@lru_cache(maxsize=1)
def _yfinance_quarterly_earnings_provider() -> QuarterlyEarningsProvider:
    # One process-singleton live provider (no key, no connection pool to share); the DB
    # cache that wraps it is built per request, since it needs the request session.
    return YfinanceQuarterlyEarningsProvider()


def get_quarterly_earnings_provider(
    db: Session = Depends(get_db),
) -> QuarterlyEarningsProvider:
    # A persistent DB cache (refreshed out of band by the quarterly-earnings cron endpoint
    # + lazily on a miss) sits in front of Yahoo so the endpoint rarely calls it, and it
    # serves stored rows without a live round-trip. yfinance needs no key, so this is
    # always wired.
    return DbCachedQuarterlyEarningsProvider(
        _yfinance_quarterly_earnings_provider(), SqlQuarterlyEarningsRepository(db)
    )


def get_quarterly_earnings_use_case(
    provider: QuarterlyEarningsProvider = Depends(get_quarterly_earnings_provider),
) -> GetQuarterlyEarnings:
    return GetQuarterlyEarnings(provider)


def _present_quarter(quarter: QuarterlyEarnings) -> QuarterlyEarningsQuarterResponse:
    return QuarterlyEarningsQuarterResponse(
        fiscal_year=quarter.fiscal_year,
        fiscal_quarter=quarter.fiscal_quarter,
        period_end=quarter.period_end,
        report_date=quarter.report_date,
        eps_actual=quarter.eps_actual,
        eps_estimate=quarter.eps_estimate,
        eps_surprise=quarter.eps_surprise,
        eps_surprise_percent=quarter.eps_surprise_percent,
        revenue_estimate=quarter.revenue_estimate,
        revenue_actual=quarter.revenue_actual,
        report_session=quarter.report_session.value,
        beat=quarter.beat,
        is_reported=quarter.is_reported,
    )


def _present(timeline: QuarterlyEarningsTimeline) -> QuarterlyEarningsResponse:
    return QuarterlyEarningsResponse(
        symbol=timeline.symbol,
        count=len(timeline.quarters),
        reported_count=len(timeline.past),
        upcoming_count=len(timeline.future),
        quarters=[_present_quarter(q) for q in timeline.quarters],
    )


@router.get(
    "/stocks/{symbol}/earnings/quarterly", response_model=QuarterlyEarningsResponse
)
def get_quarterly_earnings_endpoint(
    symbol: str,
    response: Response,
    use_case: GetQuarterlyEarnings = Depends(get_quarterly_earnings_use_case),
) -> QuarterlyEarningsResponse:
    try:
        timeline = use_case.execute(symbol)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except StockNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except StockDataUnavailable as exc:
        raise HTTPException(502, str(exc)) from exc
    # Reported quarters and firmed-up report dates move slowly (and are served from the
    # DB cache), so cache briefly: a burst of viewers collapses onto one response.
    response.headers["Cache-Control"] = "public, max-age=300"
    return _present(timeline)
