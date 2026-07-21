from datetime import date, datetime, timezone

import pytest

from app.stocks.adapters.bedrock.bedrock_fundamentals_analysis_adapter import (
    BedrockFundamentalsAnalysisAdapter,
)
from app.stocks.ai.analysis.entities import Confidence, FundamentalsVerdict
from app.stocks.entities import (
    AnalystEstimates,
    KeyMetrics,
    Stock,
)
from app.stocks.exceptions import StockDataUnavailable
from app.stocks.company.ticker.entities import PeHistoryStats, ValuationSignal
from app.stocks.catalog.universe.entities import IndustryValuation


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
        verdict="strong",
        confidence="high",
        summary="Profitable and growing, at a reasonable price.",
        findings=["Keeps a big share of sales as profit", ""],  # blank should be dropped
    )
    payload.update(input_overrides)
    return _StubMessage(
        [_StubBlock("tool_use", name="submit_fundamentals_findings", input=payload)]
    )


# --- Fixtures for the fundamentals context -----------------------------------------------------


def _a_stock(**overrides) -> Stock:
    base = dict(
        symbol="AAPL", name="Apple Inc.", exchange="NASDAQ", price=300.0,
        open=298.0, high=301.0, low=295.0, previous_close=296.0,
        volume=1_000_000, bid=299.0, ask=301.0,
        as_of=datetime(2026, 6, 18, tzinfo=timezone.utc),
        market_cap=3_000_000_000_000.0, dividend_per_share=1.0, dividend_yield=0.42,
        metrics=KeyMetrics(
            pe=28.5, pb=45.2, ps=7.1, ev_to_ebitda=18.0, eps=6.1,
            fcf_per_share=6.43, ocf_per_share=8.0,
            gross_margin=44.0, operating_margin=30.0, net_margin=25.0, roe=147.4,
            current_ratio=0.9, debt_to_equity=1.5,
            eps_growth_yoy=12.0, revenue_growth_yoy=8.0, fcf_growth_yoy=15.0, beta=1.2,
        ),
        analyst_estimates=AnalystEstimates(
            fiscal_year=2026, period_end=date(2026, 9, 30),
            eps_avg=10.0, revenue_avg=420_000_000_000.0,
            fiscal_year_fy2=2027, eps_avg_fy2=11.5, revenue_avg_fy2=455_000_000_000.0,
        ),
    )
    base.update(overrides)
    return Stock(**base)


def _a_benchmark() -> IndustryValuation:
    return IndustryValuation.from_pe_ratios(
        "semiconductors", (10.0, 20.0, 30.0, 40.0, 50.0)
    )


def test_parses_tool_call_into_entity():
    client = _StubClient(_tool_message())
    provider = BedrockFundamentalsAnalysisAdapter(client=client, model_id="test-model")

    analysis = provider.analyze(_a_stock(), _a_benchmark())

    assert analysis.symbol == "AAPL"
    assert analysis.verdict is FundamentalsVerdict.STRONG
    assert analysis.confidence is Confidence.HIGH
    assert analysis.summary.startswith("Profitable and growing")
    assert analysis.findings == ("Keeps a big share of sales as profit",)  # blank dropped
    assert analysis.model == "test-model"
    # The model was actually pinned to the forced tool, with our model id.
    assert client.calls[0]["tool_choice"] == {
        "type": "tool",
        "name": "submit_fundamentals_findings",
    }
    assert client.calls[0]["model"] == "test-model"


def test_stamps_a_generated_at():
    client = _StubClient(_tool_message())
    before = datetime.now(timezone.utc)
    analysis = BedrockFundamentalsAnalysisAdapter(client=client).analyze(_a_stock())
    assert analysis.generated_at >= before


def test_renders_fundamentals_into_the_prompt():
    client = _StubClient(_tool_message())
    BedrockFundamentalsAnalysisAdapter(client=client).analyze(_a_stock(), _a_benchmark())

    prompt = client.calls[0]["messages"][0]["content"]
    assert "Fundamentals for AAPL" in prompt
    assert "P/E (trailing): 28.50" in prompt
    assert "EV/EBITDA (trailing): 18.00" in prompt  # the capital-structure-neutral multiple
    assert "Net margin %: 25.00" in prompt
    assert "Forward P/E (consensus): 30.00" in prompt  # price 300 / FY1 eps 10
    # The cash-flow yields are priced on the snapshot quote (the ticker card's reads).
    assert "Price/FCF (trailing): 46.66" in prompt  # 300 / 6.43
    assert "FCF yield %: 2.14" in prompt  # 6.43 / 300 * 100
    assert "OCF yield % (pre-capex): 2.67" in prompt  # 8.0 / 300 * 100
    assert "FCF/share growth YoY %: 15.00" in prompt
    # The industry benchmark rides along.
    assert "Industry valuation benchmark" in prompt
    assert "Industry: semiconductors" in prompt
    assert "Median P/E: 30.00" in prompt


def test_renders_the_pe_history_signal_into_the_prompt():
    client = _StubClient(_tool_message())
    stats = PeHistoryStats(
        current_pe=18.0, median_pe=24.0, p25_pe=20.0, p75_pe=30.0,
        min_pe=12.0, max_pe=40.0, current_percentile=15.0,
        discount_to_median_percent=-25.0, signal=ValuationSignal.CHEAP, sample_size=16,
    )
    BedrockFundamentalsAnalysisAdapter(client=client).analyze(
        _a_stock(), _a_benchmark(), stats
    )
    prompt = client.calls[0]["messages"][0]["content"]
    assert "Valuation vs its own history" in prompt
    assert "Current trailing P/E: 18.00" in prompt
    assert "Typical (median) P/E: 24.00" in prompt
    assert "Signal: cheap" in prompt


def test_omits_the_pe_history_block_when_absent():
    client = _StubClient(_tool_message())
    BedrockFundamentalsAnalysisAdapter(client=client).analyze(_a_stock(), _a_benchmark())
    assert "Valuation vs its own history" not in client.calls[0]["messages"][0]["content"]


def test_omits_absent_blocks_from_the_prompt():
    # No metrics, no estimates, no benchmark -> a short prompt: the header + whatever base facts
    # are present (price), nothing to reason over beyond that.
    client = _StubClient(_tool_message())
    bare = _a_stock(metrics=None, analyst_estimates=None, market_cap=None,
                    dividend_per_share=None, dividend_yield=None)
    BedrockFundamentalsAnalysisAdapter(client=client).analyze(bare, None)

    prompt = client.calls[0]["messages"][0]["content"]
    assert "Fundamentals for AAPL" in prompt
    assert "Net margin" not in prompt
    assert "Industry valuation benchmark" not in prompt


def test_raises_when_the_model_does_not_call_the_tool():
    client = _StubClient(_StubMessage([_StubBlock("text")]))  # no tool_use block
    with pytest.raises(StockDataUnavailable):
        BedrockFundamentalsAnalysisAdapter(client=client).analyze(_a_stock(), _a_benchmark())


def test_maps_a_client_error_to_a_domain_error():
    with pytest.raises(StockDataUnavailable):
        BedrockFundamentalsAnalysisAdapter(client=_BoomClient()).analyze(_a_stock())


def test_rejects_an_offschema_verdict():
    client = _StubClient(_tool_message(verdict="very_strong"))  # not in the enum
    with pytest.raises(StockDataUnavailable):
        BedrockFundamentalsAnalysisAdapter(client=client).analyze(_a_stock())


def test_drops_string_findings_instead_of_char_splitting():
    client = _StubClient(_tool_message(findings="not a list"))
    analysis = BedrockFundamentalsAnalysisAdapter(client=client).analyze(_a_stock())
    assert analysis.findings == ()  # a bare string yields no findings, not characters
