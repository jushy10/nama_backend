from functools import lru_cache

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.orm import Session

from app.db import get_db
from app.stocks.adapters.db.db_cached_institutional_ownership_adapter_impl import (
    InstitutionalOwnershipAdapterImpl as DbCachedInstitutionalOwnershipAdapterImpl,
)
from app.stocks.adapters.yfinance.institutional_ownership_adapter_impl import (
    InstitutionalOwnershipAdapterImpl as YfinanceInstitutionalOwnershipAdapterImpl,
)
from app.stocks.exceptions import StockDataUnavailable, StockNotFound
from app.stocks.company.institutional_ownership.institutional_ownership_repository_adapter_impl import (
    InstitutionalOwnershipRepositoryAdapterImpl,
)
from app.stocks.company.institutional_ownership.entities import (
    InstitutionalHolder,
    InstitutionalOwnership,
)
from app.stocks.company.institutional_ownership.interfaces import InstitutionalOwnershipAdapter
from app.stocks.company.institutional_ownership.schemas import (
    HolderFlowResponse,
    InstitutionalHolderResponse,
    InstitutionalOwnershipResponse,
    OwnershipBreakdownResponse,
)
from app.stocks.company.institutional_ownership.use_cases import GetInstitutionalOwnership

router = APIRouter(tags=["institutional-ownership"])


@lru_cache(maxsize=1)
def _yfinance_institutional_provider() -> InstitutionalOwnershipAdapter:
    # One process-singleton live provider (no key, no connection pool to share); the DB cache that
    # wraps it is built per request, since it needs the request session.
    return YfinanceInstitutionalOwnershipAdapterImpl()


def get_institutional_ownership_provider(
    db: Session = Depends(get_db),
) -> InstitutionalOwnershipAdapter:
    # A persistent DB cache (refreshed out of band by the cron endpoint + lazily on a miss) sits in
    # front of Yahoo so the endpoint rarely calls it. yfinance needs no key, so this is always wired.
    return DbCachedInstitutionalOwnershipAdapterImpl(
        _yfinance_institutional_provider(),
        InstitutionalOwnershipRepositoryAdapterImpl(db),
    )


def get_institutional_ownership_use_case(
    provider: InstitutionalOwnershipAdapter = Depends(
        get_institutional_ownership_provider
    ),
) -> GetInstitutionalOwnership:
    return GetInstitutionalOwnership(provider)


def _present_holder(holder: InstitutionalHolder) -> InstitutionalHolderResponse:
    return InstitutionalHolderResponse(
        holder=holder.holder,
        holder_type=holder.holder_type,
        date_reported=holder.date_reported,
        shares=holder.shares,
        value=holder.value,
        pct_held=holder.pct_held,
        pct_change=holder.pct_change,
        is_buyer=holder.is_buyer,
        is_seller=holder.is_seller,
        share_change=holder.share_change,
        value_change=holder.value_change,
    )


def _present(ownership: InstitutionalOwnership) -> InstitutionalOwnershipResponse:
    breakdown = ownership.breakdown
    flow = ownership.flow
    return InstitutionalOwnershipResponse(
        symbol=ownership.symbol,
        count=len(ownership.holders),
        latest_report_date=ownership.latest_report_date,
        breakdown=(
            OwnershipBreakdownResponse(
                institutions_pct_held=breakdown.institutions_pct_held,
                insiders_pct_held=breakdown.insiders_pct_held,
                institutions_float_pct_held=breakdown.institutions_float_pct_held,
                institutions_count=breakdown.institutions_count,
            )
            if breakdown is not None
            else None
        ),
        flow=HolderFlowResponse(
            buyers_count=flow.buyers_count,
            sellers_count=flow.sellers_count,
            shares_bought=flow.shares_bought,
            shares_sold=flow.shares_sold,
            value_bought=flow.value_bought,
            value_sold=flow.value_sold,
            net_share_change=flow.net_share_change,
            net_value_change=flow.net_value_change,
        ),
        holders=[_present_holder(h) for h in ownership.holders],
    )


@router.get(
    "/stocks/ticker/{ticker}/institutional-ownership",
    response_model=InstitutionalOwnershipResponse,
)
def get_institutional_ownership_endpoint(
    ticker: str,
    response: Response,
    use_case: GetInstitutionalOwnership = Depends(
        get_institutional_ownership_use_case
    ),
) -> InstitutionalOwnershipResponse:
    try:
        ownership = use_case.execute(ticker)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except StockNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except StockDataUnavailable as exc:
        raise HTTPException(502, str(exc)) from exc
    # Served from the DB cache and refreshed out of band, so cache briefly: a burst of viewers
    # collapses onto one response.
    response.headers["Cache-Control"] = "public, max-age=300"
    return _present(ownership)
