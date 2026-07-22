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
    prices = _prices_for(model_id)
    if prices is None:
        return None
    input_price, output_price = prices
    return input_tokens / 1_000_000 * input_price + output_tokens / 1_000_000 * output_price


def _usage_of(message) -> tuple[int, int] | None:
    usage = getattr(message, "usage", None)
    input_tokens = getattr(usage, "input_tokens", None)
    output_tokens = getattr(usage, "output_tokens", None)
    if input_tokens is None and output_tokens is None:
        return None
    return input_tokens or 0, output_tokens or 0


def _emit_line(
    *,
    label: str,
    calls: int,
    input_tokens: int,
    output_tokens: int,
    cost_str: str,
    model_str: str,
    key: str,
) -> None:
    if calls == 0:
        return
    suffix = f" ({key})" if key else ""
    logger.info(
        "%s cost: model_calls=%d input_tokens=%d output_tokens=%d est_cost=%s "
        "(model=%s)%s",
        label,
        calls,
        input_tokens,
        output_tokens,
        cost_str,
        model_str,
        suffix,
    )


class CostAccumulator:
    def __init__(self) -> None:
        # raw model id passed to add() (or None → priced at log()'s primary) -> [calls, in, out]
        self._buckets: dict[str | None, list[int]] = {}

    @property
    def calls(self) -> int:
        return sum(b[0] for b in self._buckets.values())

    def add(self, message, model_id: str | None = None) -> None:
        tokens = _usage_of(message)
        if tokens is None:
            return
        bucket = self._buckets.setdefault(model_id, [0, 0, 0])
        bucket[0] += 1
        bucket[1] += tokens[0]
        bucket[2] += tokens[1]

    def log(self, *, label: str, model_id: str, key: str = "") -> None:
        if not self._buckets:
            return
        total = 0.0
        priced = True
        models: list[str] = []
        input_tokens = output_tokens = 0
        for raw, (_calls, tok_in, tok_out) in self._buckets.items():
            effective = raw if raw is not None else model_id
            input_tokens += tok_in
            output_tokens += tok_out
            if effective not in models:
                models.append(effective)
            piece = estimate_cost_usd(effective, tok_in, tok_out)
            if piece is None:
                priced = False
            else:
                total += piece
        cost_str = f"${total:.6f}" if priced else "unknown"
        _emit_line(
            label=label,
            calls=self.calls,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_str=cost_str,
            model_str=",".join(models),
            key=key,
        )


def log_model_cost(*, label: str, model_id: str, message, key: str = "") -> None:
    tokens = _usage_of(message)
    if tokens is None:
        return
    cost = estimate_cost_usd(model_id, tokens[0], tokens[1])
    _emit_line(
        label=label,
        calls=1,
        input_tokens=tokens[0],
        output_tokens=tokens[1],
        cost_str="unknown" if cost is None else f"${cost:.6f}",
        model_str=model_id,
        key=key,
    )
