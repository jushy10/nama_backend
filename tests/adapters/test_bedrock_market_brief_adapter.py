"""Tests for the Bedrock market-brief adapter.

Offline: a stub client (matching the Anthropic SDK's ``message.content`` → blocks with
``.type/.name/.input`` shape) is injected through the constructor seam, so the real adapter's
prompt-building, retry, and parse/translate logic runs with no ``anthropic`` package and no
network. Mirrors the other Bedrock adapter tests, over the assembled market snapshot.
"""

from datetime import date

import pytest

from app.stocks.adapters.bedrock.market_brief_adapter import BedrockMarketBriefProvider
from app.stocks.brief.entities import (
    BriefIndexMove,
    BriefMover,
    BriefSectorMove,
    BriefTone,
    MarketBriefContext,
)
from app.stocks.exceptions import StockDataUnavailable


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
    """Returns each queued message in turn (so the retry path can be exercised), repeating
    the last one once exhausted."""

    def __init__(self, messages, recorder):
        self._messages = list(messages)
        self._recorder = recorder

    def create(self, **kwargs):
        self._recorder.append(kwargs)
        if len(self._messages) > 1:
            return self._messages.pop(0)
        return self._messages[0]


class _StubClient:
    def __init__(self, *messages):
        self.calls: list[dict] = []
        self.messages = _StubMessages(messages, self.calls)


class _BoomMessages:
    def create(self, **kwargs):
        raise RuntimeError("bedrock exploded")


class _BoomClient:
    messages = _BoomMessages()


def _tool_message(**input_overrides) -> _StubMessage:
    payload = dict(
        summary="The market rose broadly today, led by tech.",
        tone="risk_on",
        sections=[
            {"heading": "Overview", "body": "Stocks climbed across the board."},
            {"heading": "Sectors", "body": "Technology led; energy lagged."},
            {"heading": "", "body": "dropped — no heading"},  # incomplete → dropped
        ],
    )
    payload.update(input_overrides)
    return _StubMessage(
        [_StubBlock("tool_use", name="submit_market_brief", input=payload)]
    )


def _context() -> MarketBriefContext:
    return MarketBriefContext(
        indexes=(
            BriefIndexMove("S&P 500", "SPY", 0.8, 1.2, 3.0, 18.0),
            BriefIndexMove("Nasdaq", "QQQ", 1.1, 2.0, None, 25.0),
        ),
        sectors=(
            BriefSectorMove("Technology", "XLK", 1.5),
            BriefSectorMove("Energy", "XLE", -0.9),
        ),
        gainers=(BriefMover("NVDA", "NVIDIA", "technology", 6.2),),
        losers=(BriefMover("XOM", "Exxon", "energy", -3.1),),
        advancers=340,
        decliners=150,
        quoted=500,
    )


_D = date(2026, 7, 14)


# --- Tests -------------------------------------------------------------------------------------


def test_parses_tool_call_into_entity():
    client = _StubClient(_tool_message())
    provider = BedrockMarketBriefProvider(client=client, model_id="test-model")

    brief = provider.generate(_context(), _D)

    assert brief.brief_date == _D
    assert brief.tone is BriefTone.RISK_ON
    assert brief.summary.startswith("The market rose")
    # The incomplete (heading-less) section is dropped; the two complete ones survive in order.
    assert [s.heading for s in brief.sections] == ["Overview", "Sectors"]
    assert brief.model == "test-model"
    assert brief.is_complete
    assert client.calls[0]["tool_choice"] == {
        "type": "tool",
        "name": "submit_market_brief",
    }


def test_renders_context_into_the_prompt():
    client = _StubClient(_tool_message())
    BedrockMarketBriefProvider(client=client).generate(_context(), _D)

    prompt = client.calls[0]["messages"][0]["content"]
    assert "S&P 500 (SPY)" in prompt
    assert "today 0.80%" in prompt
    assert "past year 18.00%" in prompt
    assert "Technology: today 1.50%" in prompt
    assert "340 stocks up vs 150 down" in prompt
    assert "NVIDIA (NVDA, technology): 6.20%" in prompt
    assert "Exxon (XOM, energy): -3.10%" in prompt


def test_retries_when_sections_come_back_empty():
    # First call: a summary with no sections (an incomplete Haiku result); second call recovers.
    empty = _tool_message(sections=[])
    good = _tool_message()
    client = _StubClient(empty, good)

    brief = BedrockMarketBriefProvider(client=client).generate(_context(), _D)

    assert len(brief.sections) == 2  # recovered on the retry
    assert len(client.calls) == 2  # one retry fired


def test_raises_when_the_model_never_calls_the_tool():
    client = _StubClient(_StubMessage([_StubBlock("text")]))  # no tool_use block
    with pytest.raises(StockDataUnavailable):
        BedrockMarketBriefProvider(client=client).generate(_context(), _D)


def test_maps_a_client_error_to_a_domain_error():
    with pytest.raises(StockDataUnavailable):
        BedrockMarketBriefProvider(client=_BoomClient()).generate(_context(), _D)


def test_rejects_an_offschema_tone():
    client = _StubClient(_tool_message(tone="euphoric"))  # not in the enum
    with pytest.raises(StockDataUnavailable):
        BedrockMarketBriefProvider(client=client).generate(_context(), _D)
