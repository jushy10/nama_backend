import httpx
import pytest

from app.evals.adapters.answer_under_test_adapter_impl import AnswerUnderTestAdapterImpl
from app.evals.exceptions import SubjectUnavailable


def _adapter(handler, *, answer_field="answer", path="/research") -> AnswerUnderTestAdapterImpl:
    client = httpx.Client(
        transport=httpx.MockTransport(handler), base_url="http://test"
    )
    return AnswerUnderTestAdapterImpl(path=path, answer_field=answer_field, client=client)


def test_posts_the_question_and_returns_the_answer_field():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json

        seen["url"] = str(request.url)
        seen["body"] = _json.loads(request.content)
        return httpx.Response(200, json={"answer": "NVDA is larger.", "model": "m"})

    out = _adapter(handler).answer("compare NVDA and AMD")
    assert out == "NVDA is larger."
    assert seen["url"].endswith("/research")
    assert seen["body"] == {"question": "compare NVDA and AMD"}


def test_a_non_2xx_response_becomes_subject_unavailable():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, json={"detail": "bedrock down"})

    with pytest.raises(SubjectUnavailable):
        _adapter(handler).answer("q")


def test_a_missing_answer_field_becomes_subject_unavailable():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"not_answer": "oops"})

    with pytest.raises(SubjectUnavailable):
        _adapter(handler).answer("q")


def test_a_blank_answer_becomes_subject_unavailable():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"answer": "   "})

    with pytest.raises(SubjectUnavailable):
        _adapter(handler).answer("q")


def test_a_custom_answer_field_is_honored():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"text": "hello"})

    assert _adapter(handler, answer_field="text").answer("q") == "hello"
