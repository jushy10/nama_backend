"""Shared helper: estimate and log an analysis endpoint's Bedrock model spend.

Every Bedrock analyser in this folder makes one — or, on a bullet-recovery retry, a
few — forced-tool calls to Claude, each returning a message that carries token usage.
A ``CostAccumulator`` folds those calls' usage together across a single ``analyze()`` so
the adapter logs **one** cost line per endpoint request (not one per model call), turning
the running total into an approximate dollar cost from published on-demand pricing. The
line lands at info in CloudWatch beside the timing lines the use cases already emit.
(``log_model_cost`` is the single-call shortcut for an analyser that never retries.)

It is a shared *utility*, not an adapter — it implements no port and knows no vendor beyond
the shape of the Anthropic SDK's ``usage`` object — so the "an adapter never imports another
adapter" rule stays intact (the same standing as ``yfinance_session`` / ``yfinance_currency``,
which several adapters share).

Best-effort by construction: a stub/fake client with no ``usage`` (the offline tests), or a
model id not in the price table, degrades to a partial line — or silence — and nothing here
ever raises, so a cost line that can't be formed never sinks an analysis.
"""

import logging

logger = logging.getLogger(__name__)

# On-demand price per 1M tokens as (input, output), keyed by a substring of the
# Bedrock inference-profile model id (e.g. "us.anthropic.claude-haiku-4-5-...").
# First match wins, so keep the list most-specific-first. Bedrock's on-demand rates
# mirror the first-party Anthropic per-token pricing for these models; figures are
# current as of 2026-07. This is a best-effort estimate for observability, not
# billing — update a rate here if a model's price changes, or add a row for a model
# a deploy points BEDROCK_*_ANALYSIS_MODEL_ID at that isn't listed yet.
_PRICE_PER_MTOK: tuple[tuple[str, float, float], ...] = (
    ("haiku-4-5", 1.00, 5.00),  # the default tier for every analyser
    ("haiku-3-5", 0.80, 4.00),
    ("sonnet-4", 3.00, 15.00),  # Sonnet 4 / 4.5 / 4.6 (prod has run Sonnet)
    ("sonnet-5", 3.00, 15.00),
    ("opus-4", 5.00, 25.00),  # Opus 4.5–4.8 tier
)


def _prices_for(model_id: str) -> tuple[float, float] | None:
    lowered = model_id.lower()
    for needle, input_price, output_price in _PRICE_PER_MTOK:
        if needle in lowered:
            return input_price, output_price
    return None


def estimate_cost_usd(
    model_id: str, input_tokens: int, output_tokens: int
) -> float | None:
    """Approximate the USD cost of a request from its token usage, or ``None`` when
    the model id isn't in the price table (an unrecognised model — the caller then
    logs the token counts without a dollar figure rather than a wrong one)."""
    prices = _prices_for(model_id)
    if prices is None:
        return None
    input_price, output_price = prices
    return input_tokens / 1_000_000 * input_price + output_tokens / 1_000_000 * output_price


def _usage_of(message) -> tuple[int, int] | None:
    """Pull ``(input_tokens, output_tokens)`` off a model response, or ``None`` when it
    carries no usage (a test stub / fake client)."""
    usage = getattr(message, "usage", None)
    input_tokens = getattr(usage, "input_tokens", None)
    output_tokens = getattr(usage, "output_tokens", None)
    if input_tokens is None and output_tokens is None:
        return None
    return input_tokens or 0, output_tokens or 0


def _emit(
    *, label: str, model_id: str, calls: int, input_tokens: int, output_tokens: int, key: str
) -> None:
    """Format and log one aggregated cost line at info. Silent when no priced call was
    seen (``calls == 0`` — e.g. offline stubs)."""
    if calls == 0:
        return
    suffix = f" ({key})" if key else ""
    cost = estimate_cost_usd(model_id, input_tokens, output_tokens)
    cost_str = "unknown" if cost is None else f"${cost:.6f}"
    logger.info(
        "%s cost: model_calls=%d input_tokens=%d output_tokens=%d est_cost=%s "
        "(model=%s)%s",
        label,
        calls,
        input_tokens,
        output_tokens,
        cost_str,
        model_id,
        suffix,
    )


class CostAccumulator:
    """Sums token usage across the model calls of one endpoint request so an adapter
    whose ``analyze()`` may retry logs a **single** aggregated cost line, not one per
    call. Create one per ``analyze()``, ``add()`` each response, and ``log()`` once — in
    a ``finally``, so a mid-retry failure still records what was spent."""

    def __init__(self) -> None:
        self.calls = 0
        self.input_tokens = 0
        self.output_tokens = 0

    def add(self, message) -> None:
        """Fold one model response's usage into the running total. Best-effort: a
        message without usage (a stub client) contributes nothing."""
        tokens = _usage_of(message)
        if tokens is None:
            return
        self.calls += 1
        self.input_tokens += tokens[0]
        self.output_tokens += tokens[1]

    def log(self, *, label: str, model_id: str, key: str = "") -> None:
        """Emit the single aggregated cost line for this request, or stay silent if no
        priced call was seen."""
        _emit(
            label=label,
            model_id=model_id,
            calls=self.calls,
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            key=key,
        )


def log_model_cost(*, label: str, model_id: str, message, key: str = "") -> None:
    """Log the cost of a single model call — the shortcut for an analyser that makes
    exactly one (so per-call and per-endpoint coincide). Best-effort/silent like the
    accumulator: a message without usage logs nothing."""
    tokens = _usage_of(message)
    if tokens is None:
        return
    _emit(
        label=label,
        model_id=model_id,
        calls=1,
        input_tokens=tokens[0],
        output_tokens=tokens[1],
        key=key,
    )
