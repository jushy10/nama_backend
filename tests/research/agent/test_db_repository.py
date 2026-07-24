import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db import Base
from app.domains.research.agent.models import AgentRecipeRecord
from app.domains.research.agent.db_repository import DbAgentRecipeRepository


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        yield db


def _seed(session, **over) -> AgentRecipeRecord:
    record = AgentRecipeRecord(
        id=uuid.uuid4(),
        name="research",
        system_prompt="You are a research agent.",
        tool_names=["search_stocks", "get_market_sentiment"],
        max_steps=6,
        model_id="us.anthropic.claude-haiku-4-5-20251001-v1:0",
        updated_at=datetime.now(timezone.utc),
    )
    for key, value in over.items():
        setattr(record, key, value)
    session.add(record)
    session.commit()
    return record


def test_get_maps_the_row_to_the_recipe_entity(session):
    _seed(session, model_id="us.anthropic.claude-haiku-4-5-20251001-v1:0")
    recipe = DbAgentRecipeRepository(session).get("research")
    assert recipe is not None
    assert recipe.name == "research"
    assert recipe.system_prompt == "You are a research agent."
    assert recipe.tool_names == ("search_stocks", "get_market_sentiment")
    assert recipe.max_steps == 6
    assert recipe.model_id == "us.anthropic.claude-haiku-4-5-20251001-v1:0"


def test_get_returns_none_for_an_unknown_agent(session):
    _seed(session)
    assert DbAgentRecipeRepository(session).get("portfolio") is None


def test_tool_names_round_trip_as_a_tuple(session):
    _seed(session, tool_names=["search_stocks"])
    recipe = DbAgentRecipeRepository(session).get("research")
    assert recipe.tool_names == ("search_stocks",)
