import os

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.db import get_db
from app.rate_limit import limiter
from app.domains.research.agent import wiring
from app.domains.research.agent.api_schemas import ResearchRequest, ResearchResponse

router = APIRouter(tags=["stocks"])

# Each request makes several metered Bedrock calls — tight per-IP limit, env-tunable.
_AI_RESEARCH_RATE_LIMIT = os.environ.get("AI_RESEARCH_RATE_LIMIT", "10/minute")


@router.post("/agents/research", response_model=ResearchResponse)
@limiter.limit(_AI_RESEARCH_RATE_LIMIT)
def run_research_endpoint(
    request: Request,
    body: ResearchRequest,
    db: Session = Depends(get_db),
) -> ResearchResponse:
    # The endpoint calls the wiring layer, which builds and returns the use case; domain
    # errors are translated by the central handlers. Depends stays only for the DB session,
    # whose per-request open/close the framework owns.
    use_case = wiring.build_run_research(db)
    return ResearchResponse.from_result(use_case.execute(body.question))
