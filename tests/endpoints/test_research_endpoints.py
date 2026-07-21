"""Tests for the AI research endpoint (POST /research).

Offline: a fake RunResearch is injected through dependency_overrides + FastAPI's TestClient, so
this checks only the controller + presenter — the response shape (answer + the tool-call step
transcript + the service-authored disclaimer), input validation, and the error mapping —
without touching Bedrock or the database.
"""

from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.stocks.agent.entities import AgentStep, ResearchResult
from app.stocks.endpoints import research_endpoints as endpoints
from app.stocks.exceptions import StockDataUnavailable


class _FakeUseCase:
    """Stands in for RunResearch; returns a canned result or raises."""

    def __init__(self, *, result=None, error=None) -> None:
        self._result = result
        self._error = error
        self.questions: list[str] = []

    def execute(self, question: str) -> ResearchResult:
        self.questions.append(question)
        if self._error is not None:
            raise self._error
        return self._result


def _client(fake: _FakeUseCase) -> TestClient:
    app = FastAPI()
    app.include_router(endpoints.router)
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
    resp = _client(fake).post("/research", json={"question": "compare NVDA and AMD"})
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
    # Passes pydantic's min_length, but the use case rejects the blank -> ValueError -> 400.
    fake = _FakeUseCase(error=ValueError("A research question must not be empty."))
    resp = _client(fake).post("/research", json={"question": "   "})
    assert resp.status_code == 400


def test_an_empty_question_is_rejected_by_validation():
    resp = _client(_FakeUseCase(result=_a_result())).post("/research", json={"question": ""})
    assert resp.status_code == 422


def test_a_model_failure_maps_to_502():
    fake = _FakeUseCase(error=StockDataUnavailable("research", "bedrock down"))
    resp = _client(fake).post("/research", json={"question": "how is the market?"})
    assert resp.status_code == 502
