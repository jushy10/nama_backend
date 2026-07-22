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


def get_run_research(db: Session = Depends(get_db)) -> wiring.RunResearchUsecase:
    # The FastAPI-facing shim over the framework-free composition root: Depends gives the
    # per-request db lifecycle and the tests' dependency_overrides seam; the wiring layer
    # keeps all construction knowledge.
    return wiring.build_run_research(db)


@router.post("/agents/research", response_model=ResearchResponse)
@limiter.limit(_AI_RESEARCH_RATE_LIMIT)
def run_research_endpoint(
    request: Request,
    body: ResearchRequest,
    use_case: wiring.RunResearchUsecase = Depends(get_run_research),
) -> ResearchResponse:
    # Domain errors raised below are translated by the central handlers.
    return ResearchResponse.from_result(use_case.execute(body.question))
