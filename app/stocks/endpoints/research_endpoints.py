import os
from functools import lru_cache

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.db import get_db
from app.rate_limit import limiter
from app.stocks.adapters.bedrock.research_model_adapter import BedrockConversationModel
from app.stocks.adapters.cnn_fear_greed_adapter import CnnFearGreedProvider
from app.stocks.adapters.fred_vix_adapter import FredVixProvider
from app.stocks.agent.entities import ResearchResult
from app.stocks.agent.ports import ConversationModel
from app.stocks.agent.schemas import (
    AgentStepResponse,
    ResearchRequest,
    ResearchResponse,
)
from app.stocks.agent.tools import MarketSentimentTool, SearchStocksTool
from app.stocks.agent.use_cases import RunResearch
from app.stocks.exceptions import StockDataUnavailable, StockNotFound
from app.stocks.sentiment.use_cases import GetMarketSentiment
from app.stocks.universe.db_repository import SqlStockSearchRepository
from app.stocks.universe.use_cases import SearchStocks

router = APIRouter(tags=["stocks"])

# The research read makes several metered Bedrock calls per request (one per loop step), so it
# carries the same tight per-IP limit as the AI analysis reads — sized for a human asking
# occasional questions, not a scripted loop. Env-tunable without a deploy.
_AI_RESEARCH_RATE_LIMIT = os.environ.get("AI_RESEARCH_RATE_LIMIT", "10/minute")

# Authored by the service, not the model: the research read is informational only.
_RESEARCH_DISCLAIMER = (
    "AI-generated for informational and educational purposes only — not financial advice. "
    "Markets carry risk; do your own research before investing."
)


@lru_cache(maxsize=1)
def get_conversation_model() -> ConversationModel:
    # The agent's model is its primary data, so it's required — but, like the analysis
    # adapters, there's no secret to gate on: Bedrock authenticates through the process's AWS
    # credentials (the ECS task role in production). Region + model id are config with sane
    # defaults (the id may be a cross-region inference profile), env-overridable so a deploy can
    # swap models without a code change. A missing 'bedrock' extra surfaces as a clean 503.
    region = os.environ.get("BEDROCK_REGION", "us-east-1")
    model_id = os.environ.get("BEDROCK_RESEARCH_MODEL_ID")
    try:
        if model_id:
            return BedrockConversationModel(model_id=model_id, region=region)
        return BedrockConversationModel(region=region)
    except ImportError as exc:
        raise HTTPException(
            503, "AI research is not configured (install the 'bedrock' extra)."
        ) from exc


@lru_cache(maxsize=1)
def get_market_sentiment_use_case() -> GetMarketSentiment:
    # Keyless live sources (FRED + CNN), so no key gate — the same singletons the
    # /market/sentiment endpoint wires, reused here as the agent's sentiment tool.
    return GetMarketSentiment(FredVixProvider(), CnnFearGreedProvider())


def get_run_research(
    model: ConversationModel = Depends(get_conversation_model),
    sentiment: GetMarketSentiment = Depends(get_market_sentiment_use_case),
    db: Session = Depends(get_db),
) -> RunResearch:
    # Build the agent's tools from the app's own read use cases: the universe screen (a pure DB
    # read, bound to this request's session) and the live market-sentiment read. Adding a tool
    # is one more entry in this list plus its Tool subclass in app/stocks/agent/tools.py.
    tools = [
        SearchStocksTool(SearchStocks(SqlStockSearchRepository(db))),
        MarketSentimentTool(sentiment),
    ]
    return RunResearch(model, tools)


def _present(result: ResearchResult) -> ResearchResponse:
    return ResearchResponse(
        question=result.question,
        answer=result.answer,
        steps=[
            AgentStepResponse(
                tool=step.tool,
                arguments=step.arguments,
                output=step.output,
                is_error=step.is_error,
            )
            for step in result.steps
        ],
        disclaimer=_RESEARCH_DISCLAIMER,
        model=result.model,
        generated_at=result.generated_at,
    )


@router.post("/research", response_model=ResearchResponse)
@limiter.limit(_AI_RESEARCH_RATE_LIMIT)
def run_research_endpoint(
    request: Request,
    body: ResearchRequest,
    use_case: RunResearch = Depends(get_run_research),
) -> ResearchResponse:
    try:
        result = use_case.execute(body.question)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except StockNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except StockDataUnavailable as exc:
        raise HTTPException(502, str(exc)) from exc
    return _present(result)
