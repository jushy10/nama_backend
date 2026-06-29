"""Interface Adapter: AI investment analysis via Claude on Amazon Bedrock.

The only module that knows Bedrock (and the Anthropic SDK) exists. It takes the
data the use case already gathered — the price snapshot, trailing performance,
the valuation/health metrics, and the recent earnings beat history — renders it
into a compact prompt, and asks Claude Opus 4.8 for a balanced buy/hold/sell
read. Swap models or vendors and only this file changes.

Two deliberate choices keep it robust and on-pattern:

* **Auth is the runtime's job, not ours.** Bedrock authenticates through the
  process's AWS credentials (in production, the ECS task role), so — unlike
  every other vendor in this slice — there is no API key to read or pass. The
  IAM policy on the task role is what grants access.
* **Structured output via a forced tool call.** Rather than parse free text we
  hand Claude one ``submit_analysis`` tool and require it, so the model returns
  the analysis as validated JSON arguments that map straight onto the
  ``InvestmentAnalysis`` entity — no brittle prose parsing.

The Anthropic SDK is imported lazily inside ``__init__`` so the app (and the
offline test suite, which injects a fake or a stub client) imports cleanly
without the ``bedrock`` extra installed. Any Bedrock/SDK failure is translated
to ``StockDataUnavailable`` — the one error this port documents.

Operational note: model access must be enabled for the Anthropic model in the
target region, and the model id may need to be a cross-region inference profile
(e.g. ``us.anthropic.claude-opus-4-8``). Both are deploy-time config, surfaced
through the constructor / env (see ``router.get_analysis_provider``).

Docs: https://docs.anthropic.com/en/api/claude-on-amazon-bedrock
"""

from datetime import datetime, timezone

from app.stocks.entities import (
    Confidence,
    EarningsHistory,
    InvestmentAnalysis,
    Recommendation,
    Stock,
)
from app.stocks.exceptions import StockDataUnavailable
from app.stocks.ports import InvestmentAnalysisProvider

# A single forced tool is how the model is pinned to structured output: Claude
# must call submit_analysis, so the response comes back as validated JSON
# arguments instead of prose. The schema mirrors the InvestmentAnalysis entity,
# minus the fields the adapter stamps itself (symbol, model, generated_at).
_ANALYSIS_TOOL = {
    "name": "submit_analysis",
    "description": (
        "Record a balanced buy/hold/sell analysis of the stock, grounded only in "
        "the figures provided in the prompt."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "recommendation": {
                "type": "string",
                "enum": [r.value for r in Recommendation],
                "description": "The headline call, weighing the data on balance.",
            },
            "confidence": {
                "type": "string",
                "enum": [c.value for c in Confidence],
                "description": "How strongly the data supports the recommendation.",
            },
            "thesis": {
                "type": "string",
                "description": (
                    "2-4 sentences of reasoning that weigh the bull and bear cases "
                    "and justify the recommendation."
                ),
            },
            "strengths": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Up to 4 concise bull-case points, each tied to a figure.",
            },
            "risks": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Up to 4 concise bear-case points, each tied to a figure.",
            },
        },
        "required": ["recommendation", "confidence", "thesis", "strengths", "risks"],
    },
}

_SYSTEM_PROMPT = (
    "You are an equity research assistant. Given a snapshot of one stock's market "
    "data, valuation and profitability metrics, and recent earnings, produce a "
    "balanced, evidence-based read on whether it looks like a buy, hold, or sell.\n"
    "Ground every statement ONLY in the figures provided — do not use outside "
    "knowledge, recent news, or prices you may recall, and never invent numbers. "
    "If the data is thin, say so and lower your confidence. Weigh both the bull "
    "and bear cases honestly. This is general information, not personalized "
    "financial advice. Respond by calling the submit_analysis tool."
)


class BedrockAnalysisProvider(InvestmentAnalysisProvider):
    """Generates an ``InvestmentAnalysis`` with Claude Opus 4.8 on Amazon Bedrock.

    ``model_id`` and ``region`` are deploy-time config (the model id may be a
    cross-region inference profile). ``client`` is an injection seam: pass a
    ready-made client (e.g. a test stub) to bypass the Anthropic SDK entirely;
    otherwise the Bedrock client is built lazily and authenticates through the
    process's AWS credentials.
    """

    _DEFAULT_MODEL_ID = "us.anthropic.claude-opus-4-8"
    _DEFAULT_REGION = "us-east-1"
    _MAX_TOKENS = 2000

    def __init__(
        self,
        *,
        model_id: str = _DEFAULT_MODEL_ID,
        region: str = _DEFAULT_REGION,
        client=None,
    ) -> None:
        self._model_id = model_id
        if client is not None:
            self._client = client
            return
        # Imported here, not at module load: the SDK is an optional heavyweight
        # dependency (it pulls boto3), and neither the app's other endpoints nor
        # the offline tests need it present. A missing extra raises ImportError,
        # which the wiring (router.get_analysis_provider) turns into a 503.
        from anthropic import AnthropicBedrock

        self._client = AnthropicBedrock(aws_region=region)

    def analyze(
        self, stock: Stock, earnings: EarningsHistory | None = None
    ) -> InvestmentAnalysis:
        prompt = _render_prompt(stock, earnings)
        try:
            message = self._client.messages.create(
                model=self._model_id,
                max_tokens=self._MAX_TOKENS,
                system=_SYSTEM_PROMPT,
                tools=[_ANALYSIS_TOOL],
                tool_choice={"type": "tool", "name": "submit_analysis"},
                messages=[{"role": "user", "content": prompt}],
            )
        except Exception as exc:  # SDK/botocore raise a family of errors; map them all
            raise StockDataUnavailable(
                stock.symbol, f"analysis model call failed: {exc}"
            ) from exc
        payload = _tool_payload(message)
        if payload is None:
            raise StockDataUnavailable(
                stock.symbol, "analysis model returned no structured result"
            )
        return _to_entity(stock.symbol, payload, self._model_id)


def _tool_payload(message) -> dict | None:
    """Pull the submit_analysis arguments out of the model's tool call, if any."""
    for block in getattr(message, "content", None) or []:
        if (
            getattr(block, "type", None) == "tool_use"
            and getattr(block, "name", None) == "submit_analysis"
        ):
            inputs = getattr(block, "input", None)
            if isinstance(inputs, dict):
                return inputs
    return None


def _to_entity(symbol: str, payload: dict, model_id: str) -> InvestmentAnalysis:
    """Map the validated tool arguments onto the domain entity.

    The forced-tool schema constrains the shape, but a defensive guard keeps an
    off-schema result (e.g. an unknown enum value) from leaking out as something
    other than this port's documented ``StockDataUnavailable``.
    """
    try:
        recommendation = Recommendation(payload["recommendation"])
        confidence = Confidence(payload["confidence"])
        thesis = str(payload["thesis"]).strip()
    except (KeyError, ValueError) as exc:
        raise StockDataUnavailable(
            symbol, f"analysis model returned an unexpected result: {exc}"
        ) from exc
    return InvestmentAnalysis(
        symbol=symbol,
        recommendation=recommendation,
        confidence=confidence,
        thesis=thesis,
        strengths=_string_tuple(payload.get("strengths")),
        risks=_string_tuple(payload.get("risks")),
        model=model_id,
        generated_at=datetime.now(timezone.utc),
    )


def _string_tuple(value) -> tuple[str, ...]:
    """Coerce the model's list field into a tuple of non-empty, stripped strings."""
    if not isinstance(value, list):
        return ()
    return tuple(text for item in value if (text := str(item).strip()))


def _render_prompt(stock: Stock, earnings: EarningsHistory | None) -> str:
    """Render the gathered data into a compact, labelled block for the model.

    Only fields that are present are included, so the model is never handed a
    ``None`` to reason about — thin coverage simply yields a shorter prompt.
    """
    metrics = stock.metrics
    perf = stock.performance
    fields: list[tuple[str, object]] = [
        ("Name", stock.name),
        ("Exchange", stock.exchange),
        ("Price", stock.price),
        ("Day change %", stock.change_percent),
        ("Previous close", stock.previous_close),
        ("Market cap (USD)", stock.market_cap),
        ("Dividend yield %", stock.dividend_yield),
    ]
    if stock.all_time_high is not None:
        fields.append(("All-time high", stock.all_time_high.price))
    fields.append(("Drawdown from high %", stock.drawdown_from_high))
    if metrics is not None:
        fields += [
            ("P/E (trailing)", metrics.pe),
            ("PEG (trailing)", metrics.peg),
            ("P/B", metrics.pb),
            ("P/S", metrics.ps),
            ("EPS (trailing)", metrics.eps),
            ("EPS growth YoY %", metrics.eps_growth_yoy),
            ("Revenue growth YoY %", metrics.revenue_growth_yoy),
            ("Gross margin %", metrics.gross_margin),
            ("Operating margin %", metrics.operating_margin),
            ("Net margin %", metrics.net_margin),
            ("Current ratio", metrics.current_ratio),
            ("Debt/equity", metrics.debt_to_equity),
            ("Beta", metrics.beta),
            ("52-week high", metrics.week_52_high),
            ("52-week low", metrics.week_52_low),
        ]
    if perf is not None:
        fields += [
            ("Return 1w %", perf.one_week),
            ("Return 1m %", perf.one_month),
            ("Return 3m %", perf.three_month),
            ("Return 6m %", perf.six_month),
            ("Return YTD %", perf.ytd),
            ("Return 1y %", perf.one_year),
        ]
    lines = [f"Stock: {stock.symbol}"]
    lines += [f"- {label}: {_num(value)}" for label, value in fields if value is not None]
    earnings_block = _render_earnings(earnings)
    if earnings_block:
        lines.append("")
        lines.append(earnings_block)
    return "\n".join(lines)


def _render_earnings(earnings: EarningsHistory | None) -> str:
    """Render the recent beat history as a short labelled block (or '' if none)."""
    if earnings is None or not earnings.quarters:
        return ""
    lines = ["Recent earnings (newest quarter first):"]
    if earnings.beat_rate is not None:
        lines.append(
            f"- Beat rate: {earnings.beat_rate}% "
            f"({earnings.beats}/{earnings.scored} quarters met or beat estimate)"
        )
    for q in earnings.quarters:
        parts: list[str] = []
        if q.period is not None:
            parts.append(str(q.period))
        elif q.fiscal_year is not None and q.fiscal_quarter is not None:
            parts.append(f"FY{q.fiscal_year} Q{q.fiscal_quarter}")
        if q.actual is not None:
            parts.append(f"EPS actual {q.actual}")
        if q.estimate is not None:
            parts.append(f"est {q.estimate}")
        if q.surprise_percent is not None:
            parts.append(f"surprise {q.surprise_percent}%")
        if q.revenue_actual is not None:
            parts.append(f"revenue {q.revenue_actual:,.0f}")
        if parts:
            lines.append("- " + ", ".join(parts))
    return "\n".join(lines)


def _num(value: object) -> str:
    """Format a numeric field readably; pass non-numbers through unchanged."""
    if isinstance(value, bool):  # bool is an int subclass — keep it as-is
        return str(value)
    if isinstance(value, float):
        return f"{value:,.2f}"
    return str(value)
