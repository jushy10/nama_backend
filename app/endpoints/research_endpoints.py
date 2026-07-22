import os

from fastapi import APIRouter, Depends, Request

from app.rate_limit import limiter
from app.domains.research.agent.schemas import ResearchRequest, ResearchResponse
from app.domains.research.agent.use_cases import RunResearch
from app.domains.research.agent.wiring import get_run_research

router = APIRouter(tags=["stocks"])

# Each request makes several metered Bedrock calls — tight per-IP limit, env-tunable.
_AI_RESEARCH_RATE_LIMIT = os.environ.get("AI_RESEARCH_RATE_LIMIT", "10/minute")


@router.post("/agents/research", response_model=ResearchResponse)
@limiter.limit(_AI_RESEARCH_RATE_LIMIT)
def run_research_endpoint(
    request: Request,
    body: ResearchRequest,
    use_case: RunResearch = Depends(get_run_research),
) -> ResearchResponse:
    # Wiring builds the use case; domain errors are translated by the central handlers.
    return ResearchResponse.from_result(use_case.execute(body.question))
