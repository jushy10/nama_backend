import os

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.db import get_db
from app.rate_limit import client_ip, limiter
from app.domains.research.agent import wiring
from app.domains.research.agent.api_schemas import ResearchRequest, ResearchResponse
from app.endpoints.wiring import research_generation_quota

router = APIRouter(tags=["stocks"])

# Each request makes several metered Bedrock calls — tight per-IP limit, env-tunable.
_AI_RESEARCH_RATE_LIMIT = os.environ.get("AI_RESEARCH_RATE_LIMIT", "10/minute")


def get_run_research(db: Session = Depends(get_db)) -> wiring.RunResearchUseCase:
    # Shim over the framework-free wiring: Depends gives the db lifecycle + the
    # dependency_overrides test seam. The quota is built here so env config stays
    # at this edge.
    return wiring.build_run_research(db, quota=research_generation_quota(db))


@router.post("/agents/research", response_model=ResearchResponse)
@limiter.limit(_AI_RESEARCH_RATE_LIMIT)
def run_research_endpoint(
    request: Request,
    body: ResearchRequest,
    use_case: wiring.RunResearchUseCase = Depends(get_run_research),
) -> ResearchResponse:
    # Domain errors raised below (incl. QuotaExceeded -> 429) are translated by the
    # central handlers.
    return ResearchResponse.from_result(
        use_case.run(body.question, client_id=client_ip(request))
    )
