import logging
import os
from functools import lru_cache

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.db import get_db
from app.rate_limit import limiter
from app.stocks.adapters.bedrock.conversation_model_adapter_impl import ConversationModelAdapterImpl
from app.stocks.adapters.cnn.fear_greed_adapter_impl import FearGreedAdapterImpl
from app.stocks.adapters.fred.vix_adapter_impl import VixAdapterImpl
from app.stocks.ai.agent.errors import AgentNotConfigured
from app.stocks.ai.agent.interfaces import ConversationModelAdapter, Tool
from app.stocks.ai.agent.repository_adapter_impl import AgentRecipeRepositoryAdapterImpl
from app.stocks.ai.agent.schemas import ResearchRequest, ResearchResponse
from app.stocks.ai.agent.tools import MarketSentimentTool, SearchStocksTool
from app.stocks.ai.agent.use_cases import RunResearch
from app.stocks.market.sentiment.use_cases import GetMarketSentiment
from app.stocks.catalog.universe.repository_adapter_impl import StockSearchRepositoryAdapterImpl
from app.stocks.catalog.universe.use_cases import SearchStocks

logger = logging.getLogger(__name__)

router = APIRouter(tags=["stocks"])

# The recipe row the /research endpoint runs on. The DB is the single source of truth for an
# agent's prompt / tools / step budget — a new agent or a prompt change ships as a migration.
_RESEARCH_AGENT = "research"

# The research read makes several metered Bedrock calls per request (one per loop step), so it
# carries the same tight per-IP limit as the AI analysis reads — sized for a human asking
# occasional questions, not a scripted loop. Env-tunable without a deploy.
_AI_RESEARCH_RATE_LIMIT = os.environ.get("AI_RESEARCH_RATE_LIMIT", "10/minute")

@lru_cache(maxsize=4)
def get_conversation_model(model_id: str | None = None) -> ConversationModelAdapter:
    # The agent's model is its primary data, so it's required — but, like the analysis
    # adapters, there's no secret to gate on: Bedrock authenticates through the process's AWS
    # credentials (the ECS task role in production). The id comes from the recipe row (DB wins),
    # falling back to the BEDROCK_RESEARCH_MODEL_ID env override, else the adapter's default.
    # Cached per model id so two recipes on different models each keep their own client. A
    # missing 'bedrock' extra surfaces as a clean 503.
    region = os.environ.get("BEDROCK_REGION", "us-east-1")
    try:
        if model_id:
            return ConversationModelAdapterImpl(model_id=model_id, region=region)
        return ConversationModelAdapterImpl(region=region)
    except ImportError as exc:
        raise AgentNotConfigured(
            "AI research is not configured (install the 'bedrock' extra)."
        ) from exc


@lru_cache(maxsize=1)
def get_market_sentiment_use_case() -> GetMarketSentiment:
    # Keyless live sources (FRED + CNN), so no key gate — the same singletons the
    # /market/sentiment endpoint wires, reused here as the agent's sentiment tool.
    return GetMarketSentiment(VixAdapterImpl(), FearGreedAdapterImpl())


def _tool_registry(db: Session) -> dict[str, Tool]:
    # The code side of a recipe: every tool the app can offer, by the name recipe rows use.
    # Adding a tool = its Tool subclass in app/stocks/ai/agent/tools.py + one entry here; a
    # recipe then opts in by listing the name.
    sentiment = get_market_sentiment_use_case()
    return {
        "search_stocks": SearchStocksTool(SearchStocks(StockSearchRepositoryAdapterImpl(db))),
        "get_market_sentiment": MarketSentimentTool(sentiment),
    }


def get_run_research(db: Session = Depends(get_db)) -> RunResearch:
    # The recipe row is the agent's configuration — prompt, tool names, step budget, model.
    # No code fallback: a missing row is a deployment problem (migrations not run), not a
    # runtime condition to paper over, so it surfaces as a 503.
    recipe = AgentRecipeRepositoryAdapterImpl(db).get(_RESEARCH_AGENT)
    if recipe is None:
        raise AgentNotConfigured(
            "AI research is not configured "
            f"(missing '{_RESEARCH_AGENT}' agent recipe — run migrations)."
        )
    registry = _tool_registry(db)
    unknown = [name for name in recipe.tool_names if name not in registry]
    if unknown:
        logger.warning(
            "agent recipe '%s' references unknown tools: %s", recipe.name, unknown
        )
    tools = [registry[name] for name in recipe.tool_names if name in registry]
    model = get_conversation_model(
        recipe.model_id or os.environ.get("BEDROCK_RESEARCH_MODEL_ID")
    )
    return RunResearch(
        model, tools, max_steps=recipe.max_steps, system_prompt=recipe.system_prompt
    )


@router.post("/agents/research", response_model=ResearchResponse)
@limiter.limit(_AI_RESEARCH_RATE_LIMIT)
def run_research_endpoint(
    request: Request,
    body: ResearchRequest,
    use_case: RunResearch = Depends(get_run_research),
) -> ResearchResponse:
    # No error handling here by design: the use case raises domain errors and the central
    # handlers (endpoints/error_handlers.py) translate them to HTTP.
    return ResearchResponse.from_result(use_case.execute(body.question))
