"""The agent slice's composition root.

Builds the research agent from its parts — recipe row, tool registry, model adapter —
so the endpoint module stays a pure controller. FastAPI's Depends calls these factories;
the endpoint only receives the finished use case.
"""

import logging
import os
from functools import lru_cache

from fastapi import Depends
from sqlalchemy.orm import Session

from app.db import get_db
from app.adapters.bedrock.conversation_model_adapter_impl import (
    ConversationModelAdapterImpl,
)
from app.adapters.cnn.fear_greed_adapter_impl import FearGreedAdapterImpl
from app.adapters.fred.vix_adapter_impl import VixAdapterImpl
from app.domains.research.agent.errors import AgentNotConfigured
from app.domains.research.agent.interfaces import ConversationModelAdapter, Tool
from app.domains.research.agent.repository_adapter_impl import AgentRecipeRepositoryAdapterImpl
from app.domains.research.agent.tools import MarketSentimentTool, SearchStocksTool
from app.domains.research.agent.use_cases import RunResearch
from app.domains.macro.sentiment.use_cases import GetMarketSentiment
from app.domains.listings.universe.repository_adapter_impl import (
    StockSearchRepositoryAdapterImpl,
)
from app.domains.listings.universe.use_cases import SearchStocks

logger = logging.getLogger(__name__)

# The recipe row the /agents/research endpoint runs on. The DB is the single source of truth
# for an agent's prompt / tools / step budget — a new agent or a prompt change ships as a
# migration.
_RESEARCH_AGENT = "research"


@lru_cache(maxsize=4)
def get_conversation_model(model_id: str | None = None) -> ConversationModelAdapter:
    # The agent's model is its primary data, so it's required — but, like the analysis
    # adapters, there's no secret to gate on: Bedrock authenticates through the process's AWS
    # credentials (the ECS task role in production). The id comes from the recipe row (DB wins),
    # falling back to the BEDROCK_RESEARCH_MODEL_ID env override, else the adapter's default.
    # Cached per model id so two recipes on different models each keep their own client.
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
    # Adding a tool = its Tool subclass in app/domains/research/agent/tools.py + one entry here; a
    # recipe then opts in by listing the name.
    sentiment = get_market_sentiment_use_case()
    return {
        "search_stocks": SearchStocksTool(SearchStocks(StockSearchRepositoryAdapterImpl(db))),
        "get_market_sentiment": MarketSentimentTool(sentiment),
    }


def get_run_research(db: Session = Depends(get_db)) -> RunResearch:
    # The recipe row is the agent's configuration — prompt, tool names, step budget, model.
    # No code fallback: a missing row is a deployment problem (migrations not run), not a
    # runtime condition to paper over, so it raises AgentNotConfigured (-> 503).
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
