"""The agent slice's composition root — the endpoint calls build_run_research(db) and
receives the finished use case; all construction knowledge lives here."""

import os
from functools import lru_cache

from sqlalchemy.orm import Session

from app.adapters.bedrock.conversation_model_adapter_impl import (
    ConversationModelAdapterImpl,
)
from app.adapters.cnn.fear_greed_adapter_impl import FearGreedAdapterImpl
from app.adapters.fred.vix_adapter_impl import VixAdapterImpl
from app.domains.research.agent.errors import (
    BedrockNotInstalled,
    MissingAgentRecipe,
    UnknownAgentTool,
)
from app.domains.research.agent.interfaces import ConversationModelAdapter, Tool
from app.domains.research.agent.repository_adapter_impl import AgentRecipeRepositoryAdapterImpl
from app.domains.research.agent.tools import MarketSentimentTool, SearchStocksTool
from app.domains.research.agent.use_cases import RunResearchUsecase
from app.domains.macro.sentiment.use_cases import GetMarketSentiment
from app.domains.listings.universe.repository_adapter_impl import (
    StockSearchRepositoryAdapterImpl,
)
from app.domains.listings.universe.use_cases import SearchStocks

# Recipe row backing /agents/research — the DB is the source of truth for agent config.
_RESEARCH_AGENT = "research"


@lru_cache(maxsize=4)
def get_conversation_model(model_id: str) -> ConversationModelAdapter:
    # Bedrock auth rides the process's AWS credentials (ECS task role); cached per model id.
    region = os.environ.get("BEDROCK_REGION", "us-east-1")
    try:
        return ConversationModelAdapterImpl(model_id=model_id, region=region)
    except ImportError as exc:
        raise BedrockNotInstalled() from exc


@lru_cache(maxsize=1)
def get_market_sentiment_use_case() -> GetMarketSentiment:
    # Keyless live sources (FRED + CNN) — the same singletons /market/sentiment wires.
    return GetMarketSentiment(VixAdapterImpl(), FearGreedAdapterImpl())


def _tool_registry(db: Session) -> dict[str, Tool]:
    # Adding a tool = its Tool subclass in tools.py + one entry here; recipes opt in by name.
    sentiment = get_market_sentiment_use_case()
    return {
        "search_stocks": SearchStocksTool(SearchStocks(StockSearchRepositoryAdapterImpl(db))),
        "get_market_sentiment": MarketSentimentTool(sentiment),
    }


def build_run_research(db: Session) -> RunResearchUsecase:
    # A missing recipe row is a deployment problem (migrations not run) -> 503.
    # Wiring reads the recipe for what it builds (tools, model); the use case re-reads for prompt/steps.
    repo = AgentRecipeRepositoryAdapterImpl(db)
    recipe = repo.get(_RESEARCH_AGENT)
    if recipe is None:
        raise MissingAgentRecipe(_RESEARCH_AGENT)
    registry = _tool_registry(db)
    try:
        tools = [registry[name] for name in recipe.tool_names]
    except KeyError as exc:
        raise UnknownAgentTool(exc.args[0]) from exc
    # The recipe's model_id is required (NOT NULL) — no env or code fallback chain.
    model = get_conversation_model(recipe.model_id)
    return RunResearchUsecase(model, tools, repo, _RESEARCH_AGENT)
