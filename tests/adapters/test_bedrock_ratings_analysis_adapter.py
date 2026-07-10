"""Tests for the Bedrock ratings-analysis adapter.

Offline: a stub client (matching the Anthropic SDK's ``message.content`` → blocks with
``.type/.name/.input`` shape) is injected through the constructor seam, so the real adapter's
prompt-building and parse/translate logic runs with no ``anthropic`` package and no network.
Mirrors ``tests/adapters/test_bedrock_etf_analysis_adapter.py``, but over the analyst-coverage
context (recommendation consensus + top credible firms) instead of an ``EtfDetail``.
"""

from datetime import date

import pytest

from app.stocks.adapters.bedrock.ratings_analysis_adapter import (
    BedrockRatingsAnalysisProvider,
)
from app.stocks.entities import Confidence, RatingsVerdict
from app.stocks.exceptions import StockDataUnavailable
from app.stocks.recommendations.entities import (
    AnalystPriceTargets,
    AnalystRecommendations,
    FirmRating,
    RecommendationTrend,
)


# --- Stub Bedrock client (same shape as the other adapters') -----------------------------------


class _StubBlock:
    def __init__(self, type, name=None, input=None):
        self.type = type
        self.name = name
        self.input = input


class _StubMessage:
    def __init__(self, content):
        self.content = content


class _StubMessages:
    def __init__(self, message, recorder):
        self._message = message
        self._recorder = recorder

    def create(self, **kwargs):
        self._recorder.append(kwargs)
        return self._message


class _StubClient:
    def __init__(self, message):
        self.calls: list[dict] = []
        self.messages = _StubMessages(message, self.calls)


class _BoomMessages:
    def create(self, **kwargs):
        raise RuntimeError("bedrock exploded")


class _BoomClient:
    messages = _BoomMessages()


def _tool_message(**input_overrides) -> _StubMessage:
    payload = dict(
        verdict="bullish",
        confidence="high",
        summary="Analysts are overwhelmingly positive on this stock.",
        findings=["Nearly all analysts rate it a buy", ""],  # the blank entry should be dropped
    )
    payload.update(input_overrides)
    return _StubMessage(
        [_StubBlock("tool_use", name="submit_ratings_findings", input=payload)]
    )


# --- Fixtures for the coverage context ---------------------------------------------------------


def _recommendations() -> AnalystRecommendations:
    return AnalystRecommendations(
        "NVDA",
        (
            RecommendationTrend(
                date(2026, 6, 1), strong_buy=10, buy=48, hold=2, sell=1, strong_sell=0
            ),
            RecommendationTrend(
                date(2026, 5, 1), strong_buy=9, buy=46, hold=3, sell=1, strong_sell=0
            ),
        ),
        AnalystPriceTargets(mean=301.62, high=500.0, low=180.0, median=300.0),
    )


def _top_firms() -> tuple[FirmRating, ...]:
    return (
        FirmRating(
            firm="RBC Capital",
            rank=1,
            rating="Outperform",
            action="main",
            target=270.0,
            published_at=date(2026, 5, 21),
        ),
        FirmRating(
            firm="Evercore ISI Group",
            rank=2,
            rating="Outperform",
            action="main",
            target=413.0,
            published_at=date(2026, 5, 21),
        ),
    )


# --- Tests -------------------------------------------------------------------------------------


def test_parses_tool_call_into_entity():
    client = _StubClient(_tool_message())
    provider = BedrockRatingsAnalysisProvider(client=client, model_id="test-model")

    analysis = provider.analyze("nvda", _recommendations(), _top_firms())

    assert analysis.symbol == "NVDA"  # normalized/upper-cased by the adapter
    assert analysis.verdict is RatingsVerdict.BULLISH
    assert analysis.confidence is Confidence.HIGH
    assert analysis.summary.startswith("Analysts are overwhelmingly")
    assert analysis.findings == ("Nearly all analysts rate it a buy",)  # blank dropped
    assert analysis.model == "test-model"
    # The model was actually pinned to the forced tool, with our model id.
    assert client.calls[0]["tool_choice"] == {
        "type": "tool",
        "name": "submit_ratings_findings",
    }
    assert client.calls[0]["model"] == "test-model"


def test_renders_coverage_into_the_prompt():
    client = _StubClient(_tool_message())
    BedrockRatingsAnalysisProvider(client=client).analyze(
        "NVDA", _recommendations(), _top_firms()
    )

    prompt = client.calls[0]["messages"][0]["content"]
    assert "Analyst coverage for NVDA" in prompt
    assert "Strong Buy 10, Buy 48, Hold 2, Sell 1, Strong Sell 0 (61 analysts)" in prompt
    assert "mean $301.62" in prompt
    assert "high $500.00" in prompt
    assert "RBC Capital" in prompt
    assert "target $270.00" in prompt


def test_omits_absent_blocks_from_the_prompt():
    # No consensus and no top firms -> a short prompt with just the header, nothing to reason
    # over beyond the label (the use case only reaches the model when there IS something).
    client = _StubClient(_tool_message())
    BedrockRatingsAnalysisProvider(client=client).analyze("NVDA", None, ())

    prompt = client.calls[0]["messages"][0]["content"]
    assert "Analyst coverage for NVDA" in prompt
    assert "ratings split" not in prompt
    assert "price target" not in prompt
    assert "covering firms" not in prompt


def test_raises_when_the_model_does_not_call_the_tool():
    client = _StubClient(_StubMessage([_StubBlock("text")]))  # no tool_use block
    with pytest.raises(StockDataUnavailable):
        BedrockRatingsAnalysisProvider(client=client).analyze(
            "NVDA", _recommendations(), _top_firms()
        )


def test_maps_a_client_error_to_a_domain_error():
    with pytest.raises(StockDataUnavailable):
        BedrockRatingsAnalysisProvider(client=_BoomClient()).analyze(
            "NVDA", _recommendations(), ()
        )


def test_rejects_an_offschema_verdict():
    client = _StubClient(_tool_message(verdict="very_bullish"))  # not in the enum
    with pytest.raises(StockDataUnavailable):
        BedrockRatingsAnalysisProvider(client=client).analyze("NVDA", _recommendations(), ())


def test_drops_string_findings_instead_of_char_splitting():
    client = _StubClient(_tool_message(findings="not a list"))
    analysis = BedrockRatingsAnalysisProvider(client=client).analyze(
        "NVDA", _recommendations(), ()
    )
    assert analysis.findings == ()  # a bare string yields no findings, not characters
