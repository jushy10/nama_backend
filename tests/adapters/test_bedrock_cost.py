"""Tests for the shared Bedrock cost helper.

Pure and offline: the cost math is a plain function, and the log line is captured
via a handler attached directly to the module's own logger — not pytest's ``caplog``,
which the Alembic-driven migration tests can disable (the project's log-assertion
note). The handler capture also forces ``logger.disabled = False`` in case an earlier
test left it disabled.
"""

import logging

import pytest

from app.stocks.adapters.bedrock import cost
from app.stocks.adapters.bedrock.cost import (
    CostAccumulator,
    estimate_cost_usd,
    log_model_cost,
)


# --- Fakes mirroring the Anthropic SDK message.usage shape -------------------------


class _Usage:
    def __init__(self, input_tokens, output_tokens):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


class _Message:
    def __init__(self, usage=None):
        self.usage = usage


def _capture_logs(fn):
    """Run ``fn`` with a handler attached to the cost logger; return the records."""
    records: list[logging.LogRecord] = []
    handler = logging.Handler()
    handler.emit = records.append  # type: ignore[method-assign]
    prev_level, prev_disabled = cost.logger.level, cost.logger.disabled
    cost.logger.addHandler(handler)
    cost.logger.setLevel(logging.INFO)
    cost.logger.disabled = False
    try:
        fn()
    finally:
        cost.logger.removeHandler(handler)
        cost.logger.setLevel(prev_level)
        cost.logger.disabled = prev_disabled
    return records


# --- estimate_cost_usd (the pricing math) ------------------------------------------


def test_estimate_cost_haiku():
    # Haiku 4.5 (the default tier): $1/MTok in, $5/MTok out.
    cost_usd = estimate_cost_usd(
        "us.anthropic.claude-haiku-4-5-20251001-v1:0", 1_000_000, 200_000
    )
    assert cost_usd == pytest.approx(1.0 + 1.0)  # 1M*$1 + 0.2M*$5


def test_estimate_cost_sonnet():
    # Prod has run Sonnet: $3/MTok in, $15/MTok out.
    cost_usd = estimate_cost_usd(
        "us.anthropic.claude-sonnet-4-6-v1:0", 1_000_000, 1_000_000
    )
    assert cost_usd == pytest.approx(3.0 + 15.0)


def test_estimate_cost_opus():
    # Opus 4.x tier: $5/MTok in, $25/MTok out.
    cost_usd = estimate_cost_usd("us.anthropic.claude-opus-4-8-v1:0", 2_000_000, 0)
    assert cost_usd == pytest.approx(10.0)


def test_estimate_cost_unknown_model_is_none():
    assert estimate_cost_usd("some.unlisted.model", 1_000, 1_000) is None


# --- CostAccumulator (one aggregated line per endpoint call) -----------------------


def test_accumulator_sums_calls_into_one_line():
    # Two model calls (a bullet-recovery retry) → a single line summing their usage,
    # with model_calls=2 so the retry is visible.
    acc = CostAccumulator()
    acc.add(_Message(_Usage(1000, 300)))
    acc.add(_Message(_Usage(1200, 400)))
    records = _capture_logs(
        lambda: acc.log(
            label="stock analysis",
            model_id="us.anthropic.claude-haiku-4-5-20251001-v1:0",
            key="AAPL",
        )
    )
    assert len(records) == 1
    msg = records[0].getMessage()
    assert "stock analysis cost" in msg
    assert "model_calls=2" in msg
    assert "input_tokens=2200" in msg  # 1000 + 1200
    assert "output_tokens=700" in msg  # 300 + 400
    assert "est_cost=$" in msg
    assert "(AAPL)" in msg


def test_accumulator_skips_usageless_calls():
    # A stub response (no usage) contributes nothing and isn't counted.
    acc = CostAccumulator()
    acc.add(_Message(_Usage(1000, 300)))
    acc.add(_Message(usage=None))
    records = _capture_logs(
        lambda: acc.log(label="stock analysis", model_id="x", key="AAPL")
    )
    assert len(records) == 1
    assert "model_calls=1" in records[0].getMessage()


def test_accumulator_no_calls_is_silent():
    # No priced call seen (all stubs, or a first-call failure) → no line at all.
    acc = CostAccumulator()
    acc.add(_Message(usage=None))
    records = _capture_logs(
        lambda: acc.log(label="stock analysis", model_id="x", key="AAPL")
    )
    assert records == []


# --- log_model_cost (the single-call shortcut, e.g. the ratings analyser) ----------


def test_log_model_cost_emits_single_call_line():
    records = _capture_logs(
        lambda: log_model_cost(
            label="ratings analysis",
            model_id="us.anthropic.claude-haiku-4-5-20251001-v1:0",
            message=_Message(_Usage(1000, 500)),
            key="AAPL",
        )
    )
    assert len(records) == 1
    msg = records[0].getMessage()
    assert "ratings analysis cost" in msg
    assert "model_calls=1" in msg
    assert "input_tokens=1000" in msg
    assert "output_tokens=500" in msg
    assert "est_cost=$" in msg
    assert "(AAPL)" in msg


def test_log_model_cost_unknown_model_logs_unknown_cost():
    records = _capture_logs(
        lambda: log_model_cost(
            label="ratings analysis",
            model_id="some.unlisted.model",
            message=_Message(_Usage(1000, 500)),
        )
    )
    assert len(records) == 1
    assert "est_cost=unknown" in records[0].getMessage()


def test_log_model_cost_no_usage_is_silent():
    # A stub/fake client (the offline adapter tests) carries no usage — stay silent.
    records = _capture_logs(
        lambda: log_model_cost(
            label="ratings analysis", model_id="x", message=_Message(usage=None)
        )
    )
    assert records == []
