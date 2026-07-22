from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.orm import Session

from app.db import get_db
from app.domains.research.brief.market_brief_repository_adapter_impl import MarketBriefRepositoryAdapterImpl
from app.domains.research.brief.entities import MarketBrief
from app.domains.research.brief.schemas import MarketBriefResponse, MarketBriefSectionResponse
from app.domains.research.brief.use_cases import GetDailyBrief

router = APIRouter(tags=["market-brief"])

# Attached at the edge (not model-authored): the brief is descriptive, general information.
_DISCLAIMER = (
    "This market brief is AI-generated from recent market data for general information only "
    "and is not financial advice."
)


def get_daily_brief_use_case(db: Session = Depends(get_db)) -> GetDailyBrief:
    # Pure DB read over the brief store — no vendor, no key, no model.
    return GetDailyBrief(MarketBriefRepositoryAdapterImpl(db))


def _present(brief: MarketBrief) -> MarketBriefResponse:
    return MarketBriefResponse(
        date=brief.brief_date,
        generated_at=brief.generated_at,
        tone=brief.tone.value,
        summary=brief.summary,
        sections=[
            MarketBriefSectionResponse(heading=s.heading, body=s.body)
            for s in brief.sections
        ],
        model=brief.model or None,
        disclaimer=_DISCLAIMER,
    )


def _serve(brief: MarketBrief | None, response: Response) -> MarketBriefResponse:
    if brief is None:
        raise HTTPException(404, "No market brief is available yet.")
    # A dated brief never changes once written; cache generously so a crawler/viewer burst
    # collapses onto one response.
    response.headers["Cache-Control"] = "public, max-age=900"
    return _present(brief)


@router.get("/market/brief", response_model=MarketBriefResponse)
def get_latest_brief_endpoint(
    response: Response,
    use_case: GetDailyBrief = Depends(get_daily_brief_use_case),
) -> MarketBriefResponse:
    return _serve(use_case.execute(None), response)


@router.get("/market/brief/{brief_date}", response_model=MarketBriefResponse)
def get_dated_brief_endpoint(
    brief_date: str,
    response: Response,
    use_case: GetDailyBrief = Depends(get_daily_brief_use_case),
) -> MarketBriefResponse:
    try:
        parsed = date.fromisoformat(brief_date)
    except ValueError as exc:
        raise HTTPException(400, f"'{brief_date}' is not a valid date (YYYY-MM-DD).") from exc
    return _serve(use_case.execute(parsed), response)
