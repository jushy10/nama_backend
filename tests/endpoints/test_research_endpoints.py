import uuid
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db import Base
from app.domains.research.agent.entities import AgentStep, ResearchResult
from app.domains.research.agent.errors import AgentNotConfigured, EmptyQuestion, UnknownAgentTool
from app.domains.research.agent.models import AgentRecipeRecord
from app.domains.research.agent import wiring
from app.endpoints import research_endpoints as endpoints
from app.endpoints.error_handlers import register_error_handlers
from app.domains.shared.exceptions import QuotaExceeded, StockDataUnavailable


class _FakeUseCase:
    def __init__(self, *, result=None, error=None) -> None:
        self._result = result
        self._error = error
        self.questions: list[str] = []

    def run(self, question: str, client_id: str | None = None) -> ResearchResult:
        self.questions.append(question)
        if self._error is not None:
            raise self._error
        return self._result


def _client(fake: _FakeUseCase) -> TestClient:
    app = FastAPI()
    app.include_router(endpoints.router)
    register_error_handlers(app)  # the endpoint has no try/except; the handlers translate
    # Overriding the shim replaces the whole construction chain (db session included).
    app.dependency_overrides[endpoints.get_run_research] = lambda: fake
    return TestClient(app)


def _a_result(**over) -> ResearchResult:
    base = dict(
        question="compare NVDA and AMD",
        answer="NVDA is larger and growing faster.",
        model="fake-model",
        generated_at=datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc),
        steps=(
            AgentStep(
                tool="search_stocks",
                arguments={"query": "NVDA"},
                output="NVDA — $3.4T",
                is_error=False,
            ),
        ),
    )
    base.update(over)
    return ResearchResult(**base)


def test_returns_the_answer_steps_and_disclaimer():
    fake = _FakeUseCase(result=_a_result())
    resp = _client(fake).post("/agents/research", json={"question": "compare NVDA and AMD"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["answer"] == "NVDA is larger and growing faster."
    assert body["model"] == "fake-model"
    assert body["steps"] == [
        {
            "tool": "search_stocks",
            "arguments": {"query": "NVDA"},
            "output": "NVDA — $3.4T",
            "is_error": False,
        }
    ]
    # The disclaimer is attached at the edge, not authored by the model.
    assert "not financial advice" in body["disclaimer"].lower()
    assert fake.questions == ["compare NVDA and AMD"]


def test_a_whitespace_question_maps_to_400():
    # Passes pydantic's min_length, but the use case rejects the blank -> EmptyQuestion -> 400.
    fake = _FakeUseCase(error=EmptyQuestion())
    resp = _client(fake).post("/agents/research", json={"question": "   "})
    assert resp.status_code == 400


def test_an_empty_question_is_rejected_by_validation():
    resp = _client(_FakeUseCase(result=_a_result())).post("/agents/research", json={"question": ""})
    assert resp.status_code == 422


def test_a_model_failure_maps_to_502():
    fake = _FakeUseCase(error=StockDataUnavailable("research", "bedrock down"))
    resp = _client(fake).post("/agents/research", json={"question": "how is the market?"})
    assert resp.status_code == 502


# --- The recipe wiring: the DB row is the agent's configuration, with no code fallback ------


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def _seed_recipe(db, **over) -> None:
    record = AgentRecipeRecord(
        id=uuid.uuid4(),
        name="research",
        system_prompt="Answer with the tools.",
        tool_names=["search_stocks", "get_market_sentiment"],
        max_steps=4,
        model_id="us.anthropic.claude-haiku-4-5-20251001-v1:0",
        updated_at=datetime.now(timezone.utc),
    )
    for key, value in over.items():
        setattr(record, key, value)
    db.add(record)
    db.commit()


class _FakeModel:
    pass


def test_a_missing_recipe_row_raises_agent_not_configured(db):
    # AgentNotConfigured maps to 503 via the central error handlers.
    with pytest.raises(AgentNotConfigured, match="recipe"):
        wiring.build_run_research(db=db)


def test_the_recipe_row_configures_the_agent(db, monkeypatch):
    _seed_recipe(db, tool_names=["search_stocks"], max_steps=2)
    seen: list[str | None] = []

    def fake_model(model_id=None):
        seen.append(model_id)
        return _FakeModel()

    monkeypatch.setattr(wiring, "get_conversation_model", fake_model)
    use_case = wiring.build_run_research(db=db)
    assert list(use_case._tools) == ["search_stocks"]  # only the recipe's tools are offered
    assert use_case._agent_name == "research"  # prompt/steps are fetched from the repo at execute time
    assert use_case._recipe_repo.get("research").system_prompt == "Answer with the tools."
    assert seen == ["us.anthropic.claude-haiku-4-5-20251001-v1:0"]  # the row's required model id


def test_the_recipe_model_id_is_used(db, monkeypatch):
    _seed_recipe(db, model_id="us.anthropic.claude-sonnet-5-v1:0")
    seen: list[str | None] = []
    monkeypatch.setattr(
        wiring,
        "get_conversation_model",
        lambda model_id=None: seen.append(model_id) or _FakeModel(),
    )
    wiring.build_run_research(db=db)
    assert seen == ["us.anthropic.claude-sonnet-5-v1:0"]


def test_an_unknown_tool_name_fails_loud(db, monkeypatch):
    # A recipe naming a tool the registry lacks is a misconfiguration -> 503, not a
    # silently thinner agent.
    _seed_recipe(db, tool_names=["search_stocks", "not_a_tool"])
    monkeypatch.setattr(wiring, "get_conversation_model", lambda model_id=None: _FakeModel())
    with pytest.raises(UnknownAgentTool, match="not_a_tool"):
        wiring.build_run_research(db=db)


def test_a_spent_daily_quota_is_a_429():
    fake = _FakeUseCase(error=QuotaExceeded())
    resp = _client(fake).post("/agents/research", json={"question": "compare NVDA and AMD"})
    assert resp.status_code == 429
    assert "limit" in resp.json()["detail"].lower()
