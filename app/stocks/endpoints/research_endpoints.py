import os

from fastapi import APIRouter, Depends, Request

from app.rate_limit import limiter
from app.stocks.ai.agent.schemas import ResearchRequest, ResearchResponse
from app.stocks.ai.agent.use_cases import RunResearch
from app.stocks.ai.agent.wiring import get_run_research

router = APIRouter(tags=["stocks"])

# The research read makes several metered Bedrock calls per request (one per loop step), so it
# carries the same tight per-IP limit as the AI analysis reads — sized for a human asking
# occasional questions, not a scripted loop. Env-tunable without a deploy.
_AI_RESEARCH_RATE_LIMIT = os.environ.get("AI_RESEARCH_RATE_LIMIT", "10/minute")


@router.post("/agents/research", response_model=ResearchResponse)
@limiter.limit(_AI_RESEARCH_RATE_LIMIT)
def run_research_endpoint(
    request: Request,
    body: ResearchRequest,
    use_case: RunResearch = Depends(get_run_research),
) -> ResearchResponse:
    # No construction and no error handling here by design: the composition root
    # (ai/agent/wiring.py) builds the use case, the use case raises domain errors, and the
    # central handlers (endpoints/error_handlers.py) translate them to HTTP.
    return ResearchResponse.from_result(use_case.execute(body.question))
