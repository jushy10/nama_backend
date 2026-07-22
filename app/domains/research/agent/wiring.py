"""The agent slice's composition root — Depends() calls these factories; the endpoint
only receives the finished use case."""

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

# Recipe row backing /agents/research — the DB is the source of truth for agent config.
_RESEARCH_AGENT = "research"


@lru_cache(maxsize=4)
def get_conversation_model(model_id: str | None = None) -> ConversationModelAdapter:
    # No secret to gate on — Bedrock authenticates via the process's AWS credentials (the
    # ECS task role in prod). Cached per model id so recipes on different models coexist.
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
    # Keyless live sources (FRED + CNN) — the same singletons /market/sentiment wires.
    return GetMarketSentiment(VixAdapterImpl(), FearGreedAdapterImpl())


def _tool_registry(db: Session) -> dict[str, Tool]:
    # Every tool the app offers, by the name recipe rows use. Adding a tool = its Tool
    # subclass in tools.py + one entry here; a recipe opts in by listing the name.
    sentiment = get_market_sentiment_use_case()
    return {
        "search_stocks": SearchStocksTool(SearchStocks(StockSearchRepositoryAdapterImpl(db))),
        "get_market_sentiment": MarketSentimentTool(sentiment),
    }


def get_run_research(db: Session = Depends(get_db)) -> RunResearch:
    # No code fallback: a missing recipe row is a deployment problem (migrations not run),
    # surfaced as AgentNotConfigured -> 503.
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
