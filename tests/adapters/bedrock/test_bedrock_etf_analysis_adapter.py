from datetime import datetime, timezone

import pytest

from app.stocks.adapters.bedrock.bedrock_etf_analysis_adapter import BedrockEtfAnalysisProvider
from app.stocks.ai.analysis.entities import Confidence, Recommendation
from app.stocks.entities import Quote, StockPerformance
from app.stocks.catalog.etfs.entities import (
    EtfDetail,
    EtfHolding,
    EtfProfile,
    EtfSearchResult,
    EtfSectorWeight,
)
from app.stocks.exceptions import StockDataUnavailable


# --- Stub Bedrock client (same shape as the stock adapter's) -----------------------------------


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


class _SeqStubMessages:
    def __init__(self, messages, recorder):
        self._messages = list(messages)
        self._recorder = recorder

    def create(self, **kwargs):
        self._recorder.append(kwargs)
        return self._messages[min(len(self._recorder) - 1, len(self._messages) - 1)]


class _SeqStubClient:
    def __init__(self, messages):
        self.calls: list[dict] = []
        self.messages = _SeqStubMessages(messages, self.calls)


def _tool_message(**input_overrides) -> _StubMessage:
    payload = dict(
        recommendation="buy",
        confidence="high",
        thesis="A cheap, broad way to own the whole market.",
        strengths=["Very low yearly cost", ""],  # the blank entry should be dropped
        risks=["Heavy in a handful of tech names"],
    )
    payload.update(input_overrides)
    return _StubMessage([_StubBlock("tool_use", name="submit_analysis", input=payload)])


def _bullets_message(**input_overrides) -> _StubMessage:
    # The lighter recovery tool the retry path forces — only the two bullet lists.
    payload = dict(
        strengths=["Very low yearly cost", ""],  # the blank entry should be dropped
        risks=["Heavy in a handful of tech names"],
    )
    payload.update(input_overrides)
    return _StubMessage([_StubBlock("tool_use", name="submit_bullets", input=payload)])


# --- Fixtures for the detail snapshot ----------------------------------------------------------


def _quote(symbol="VOO", price=685.28, previous_close=682.07) -> Quote:
    return Quote(
        symbol=symbol,
        price=price,
        previous_close=previous_close,
        bid=None,
        ask=None,
        as_of=datetime(2026, 7, 6, 20, 0, tzinfo=timezone.utc),
    )


def _profile() -> EtfProfile:
    return EtfProfile(
        fund_family="Vanguard",
        nav=685.28,
        dividend_yield=1.03,
        three_year_return=20.41,
        five_year_return=13.01,
        description="The fund employs an indexing investment approach.",
        top_holdings=(
            EtfHolding(ticker="NVDA", name="NVIDIA Corp", weight=7.89),
            EtfHolding(ticker="AAPL", name="Apple Inc", weight=6.12),
        ),
        sector_weightings=(EtfSectorWeight(sector="technology", weight=39.13),),
    )


def _detail(*, profile=None, performance=None) -> EtfDetail:
    facts = EtfSearchResult(
        ticker="VOO",
        name="Vanguard S&P 500 ETF",
        exchange="NYSE",
        net_assets=1_701_513_003_008.0,
        expense_ratio=0.03,
        category="large_blend",
    )
    return EtfDetail.assemble(
        "VOO",
        _quote(),
        facts,
        profile if profile is not None else _profile(),
        include=frozenset({"performance"}),
        performance=performance,
    )


def _a_performance() -> StockPerformance:
    return StockPerformance(
        one_week=0.5,
        one_month=1.2,
        three_month=3.4,
        six_month=6.5,
        ytd=8.9,
        one_year=12.3,
    )


def test_parses_tool_call_into_entity():
    client = _StubClient(_tool_message())
    provider = BedrockEtfAnalysisProvider(client=client, model_id="test-model")

    analysis = provider.analyze(_detail())

    assert analysis.symbol == "VOO"
    assert analysis.recommendation is Recommendation.BUY
    assert analysis.confidence is Confidence.HIGH
    assert analysis.strengths == ("Very low yearly cost",)  # blank entry dropped
    assert analysis.risks == ("Heavy in a handful of tech names",)
    assert analysis.model == "test-model"
    # The model was actually pinned to the forced tool, with our model id.
    assert client.calls[0]["tool_choice"] == {"type": "tool", "name": "submit_analysis"}
    assert client.calls[0]["model"] == "test-model"


def test_renders_the_fund_facts_into_the_prompt():
    client = _StubClient(_tool_message())
    BedrockEtfAnalysisProvider(client=client).analyze(
        _detail(performance=_a_performance())
    )

    prompt = client.calls[0]["messages"][0]["content"]
    assert "ETF: VOO" in prompt
    assert "Name: Vanguard S&P 500 ETF" in prompt
    assert "Category (fund type): large_blend" in prompt
    assert "Expense ratio % (yearly cost): 0.03" in prompt
    assert "Net assets (AUM, USD): 1,701,513,003,008.00" in prompt
    assert "Dividend yield %: 1.03" in prompt
    # The trailing windows (Alpaca) and the long-horizon returns (Yahoo) both render.
    assert "Return 1y %: 12.30" in prompt
    assert "Return 3y % (annualized): 20.41" in prompt
    assert "Return 5y % (annualized): 13.01" in prompt


def test_renders_holdings_sectors_and_description_blocks():
    client = _StubClient(_tool_message())
    BedrockEtfAnalysisProvider(client=client).analyze(_detail())

    prompt = client.calls[0]["messages"][0]["content"]
    assert "Top holdings" in prompt
    assert "NVIDIA Corp: 7.89%" in prompt
    assert "Apple Inc: 6.12%" in prompt
    assert "Sector weightings" in prompt
    assert "technology: 39.13%" in prompt
    assert "Fund description:" in prompt
    assert "indexing investment approach" in prompt


def test_omits_absent_blocks_from_the_prompt():
    # An unenriched fund (empty profile, no performance) -> a short prompt with no holdings/sector/
    # description sections and no return figures, but still the quote + stored facts.
    client = _StubClient(_tool_message())
    BedrockEtfAnalysisProvider(client=client).analyze(
        _detail(profile=EtfProfile.empty(), performance=None)
    )

    prompt = client.calls[0]["messages"][0]["content"]
    assert "Top holdings" not in prompt
    assert "Sector weightings" not in prompt
    assert "Fund description" not in prompt
    assert "Return 3y" not in prompt
    assert "Category (fund type): large_blend" in prompt  # stored facts still render


def test_raises_when_the_model_does_not_call_the_tool():
    client = _StubClient(_StubMessage([_StubBlock("text")]))  # no tool_use block
    with pytest.raises(StockDataUnavailable):
        BedrockEtfAnalysisProvider(client=client).analyze(_detail())


def test_maps_a_client_error_to_a_domain_error():
    with pytest.raises(StockDataUnavailable):
        BedrockEtfAnalysisProvider(client=_BoomClient()).analyze(_detail())


def test_rejects_an_offschema_enum_value():
    client = _StubClient(_tool_message(recommendation="mega_buy"))  # not in the enum
    with pytest.raises(StockDataUnavailable):
        BedrockEtfAnalysisProvider(client=client).analyze(_detail())


def test_retries_once_when_bullets_come_back_empty():
    # The fast Haiku tier sometimes packs everything into the thesis and returns
    # empty strengths/risks; the adapter retries with the lighter bullets-only tool
    # and merges the recovered lists in (before the result-cache could freeze the
    # empty one) — a fraction of the tokens of re-running the whole analysis.
    empty = _tool_message(strengths=[], risks=[])
    bullets = _bullets_message()  # the targeted recovery call
    client = _SeqStubClient([empty, bullets])

    analysis = BedrockEtfAnalysisProvider(client=client).analyze(_detail())

    assert len(client.calls) == 2  # retried exactly once
    # the recovery is the lighter, bullets-only forced tool, not the full analysis
    assert client.calls[1]["tool_choice"] == {"type": "tool", "name": "submit_bullets"}
    assert analysis.strengths == ("Very low yearly cost",)
    assert analysis.risks == ("Heavy in a handful of tech names",)


def test_incomplete_retry_escalates_to_the_recovery_model():
    # When recovery_model_id is set, the single retry runs on that (more capable) model
    # rather than re-calling the fast primary that just dropped the bullets — the first
    # (full) pass stays on the primary.
    empty = _tool_message(strengths=[], risks=[])
    bullets = _bullets_message()
    client = _SeqStubClient([empty, bullets])
    recovery = "us.anthropic.claude-sonnet-4-6-v1:0"

    BedrockEtfAnalysisProvider(client=client, recovery_model_id=recovery).analyze(
        _detail()
    )

    assert len(client.calls) == 2
    assert client.calls[0]["model"] == BedrockEtfAnalysisProvider._DEFAULT_MODEL_ID
    assert client.calls[1]["model"] == recovery  # the retry escalated


def test_escalated_recovery_failure_is_best_effort():
    # If the escalated recovery call fails (e.g. the model isn't entitled), the failure
    # is swallowed and the first pass's (incomplete) read is still served — escalation
    # can never sink an otherwise-usable analysis.
    class _EmptyThenBoom:
        def __init__(self):
            self.calls: list[dict] = []
            self.messages = self

        def create(self, **kwargs):
            self.calls.append(kwargs)
            if len(self.calls) == 1:
                return _tool_message(strengths=[], risks=[])
            raise RuntimeError("recovery model not entitled")

    client = _EmptyThenBoom()
    analysis = BedrockEtfAnalysisProvider(
        client=client, recovery_model_id="us.anthropic.claude-sonnet-4-6-v1:0"
    ).analyze(_detail())

    assert len(client.calls) == 2  # attempted the escalated recovery once
    assert analysis.strengths == ()  # served the incomplete read, not a 502
    assert analysis.risks == ()
