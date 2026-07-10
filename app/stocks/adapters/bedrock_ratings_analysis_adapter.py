"""Interface Adapter: AI analyst-coverage read via Claude on Amazon Bedrock.

The analyst-ratings sibling of ``bedrock_earnings_analysis_provider.py`` (the
earnings read) and ``bedrock_analysis_provider.py`` (the full buy/hold/sell
read). The only module — alongside its earnings/stock/ETF/sector/market cousins
— that knows Bedrock (and the Anthropic SDK) exists. It takes the recommendation
consensus and the most credible covering firms the use case gathered, renders
them into a compact prompt, and asks Claude for a plain-language read of what
Wall Street thinks: how bullish or cautious the coverage is, how much analysts
agree, and where they see the price going. Swap models or vendors and only this
file changes.

The same two choices that keep the earnings adapter robust apply here:

* **Auth is the runtime's job, not ours.** Bedrock authenticates through the
  process's AWS credentials (in production, the ECS task role), so there is no
  API key to read or pass — only ``model_id`` and ``region``.
* **Structured output via a forced tool call.** Claude must call
  ``submit_ratings_findings``, so the model returns validated JSON arguments
  that map straight onto the ``RatingsAnalysis`` entity — no prose parsing.

The prompt carries the *real* figures (the buy/hold/sell split, the consensus
target, the top firms' stances) and the model writes only plain prose over them
— it never authors a number that reaches the card. The Anthropic SDK is imported
lazily inside ``__init__`` so the app (and the offline test suite, which injects
a stub client) imports cleanly without the ``bedrock`` extra. Any Bedrock/SDK
failure is translated to ``StockDataUnavailable`` — the one error this port
documents.

Docs: https://docs.anthropic.com/en/api/claude-on-amazon-bedrock
"""

from datetime import datetime, timezone

from app.stocks.entities import Confidence, RatingsAnalysis, RatingsVerdict
from app.stocks.exceptions import StockDataUnavailable
from app.stocks.ports import RatingsAnalysisProvider
from app.stocks.recommendations.entities import AnalystRecommendations, FirmRating

# A single forced tool pins the model to structured output: Claude must call
# submit_ratings_findings, so the response comes back as validated JSON arguments
# instead of prose. The schema mirrors the RatingsAnalysis entity, minus the
# fields the adapter stamps itself (symbol, model, generated_at).
_ANALYSIS_TOOL = {
    "name": "submit_ratings_findings",
    "description": (
        "Record a plain, everyday-language read of what Wall Street analysts think of a "
        "stock — how bullish or cautious the coverage is, how much they agree, and what "
        "stands out — grounded only in the figures in the prompt."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "verdict": {
                "type": "string",
                "enum": [v.value for v in RatingsVerdict],
                "description": (
                    "The overall read of the analyst coverage: 'bullish' when analysts "
                    "clearly lean positive (mostly Buy ratings, price targets above the "
                    "current price, upgrades), 'cautious' when they lean negative or are "
                    "turning more negative (Holds/Sells, downgrades, targets being cut), "
                    "'mixed' when they're split or sending conflicting signals."
                ),
            },
            "confidence": {
                "type": "string",
                "enum": [c.value for c in Confidence],
                "description": (
                    "How firmly to hold that verdict given the data: 'high' when many "
                    "analysts agree and the signals line up, 'low' when coverage is thin "
                    "or the signals conflict, 'medium' otherwise."
                ),
            },
            "summary": {
                "type": "string",
                "description": (
                    "2-4 short sentences in plain, everyday language: what analysts think "
                    "of this stock right now — how positive or cautious they are, how much "
                    "they agree, and roughly where they see the price going — as if to a "
                    "friend who doesn't follow markets. No jargon."
                ),
            },
            "findings": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "2-4 short, plain-language takeaways a reader should remember — one "
                    "clear point each (e.g. how lopsided the buy/hold/sell split is, how "
                    "wide the price-target range is, or whether the most respected firms "
                    "are more or less positive than the crowd). No jargon, no invented "
                    "numbers."
                ),
            },
        },
        "required": ["verdict", "confidence", "summary", "findings"],
    },
}

_SYSTEM_PROMPT = (
    "You are a friendly investing assistant explaining what Wall Street analysts think of a "
    "stock to an everyday person with no finance background. You are given the current "
    "buy/hold/sell split across the analysts who cover it, how that split shifted from last "
    "month, the consensus 12-month price target (and its range), and the current stance of "
    "the most credible research firms covering it. From only those figures, give a clear, "
    "balanced read of how bullish or cautious the coverage is and what stands out.\n"
    "Explain in plain words whether analysts mostly say buy, hold, or sell, whether they "
    "agree or are split, whether their view is improving or souring, and how far above or "
    "below today's price their target sits. Coverage that clearly leans buy with rising "
    "targets is 'bullish'; coverage full of holds and sells or being cut is 'cautious'; a "
    "split or conflicting picture is 'mixed'. Call it out when the most credible firms "
    "disagree with the crowd.\n"
    "Write in plain, warm, everyday language — short sentences, no jargon (say 'price "
    "target', not 'PT'; 'most analysts rate it a buy', not 'consensus overweight'). Ground "
    "every statement ONLY in the figures provided — do not use outside knowledge, recent "
    "news, or numbers you may recall, and never invent figures. Be honest that analysts are "
    "often wrong and ratings can lag the price. This is general information, not personal "
    "financial advice. Respond by calling the submit_ratings_findings tool."
)

# The key the adapter reports failures under.
_KEY = "ratings-analysis"


class BedrockRatingsAnalysisProvider(RatingsAnalysisProvider):
    """Generates a ``RatingsAnalysis`` with Claude on Amazon Bedrock.

    Structured exactly like ``BedrockEarningsAnalysisProvider`` (its earnings sibling):
    defaults to the fast Haiku tier since the output is short and plain, takes
    ``model_id``/``region`` as deploy-time config (the model id may be a cross-region
    inference profile, env-overridable so a deploy can swap models without a code change),
    and accepts a ``client`` injection seam so tests can bypass the Anthropic SDK entirely.
    Otherwise the Bedrock client is built lazily and authenticates through the process's AWS
    credentials.
    """

    # Full versioned inference-profile id — Haiku 4.5 has no bare alias on Bedrock, so the
    # short form 400s. Same default as the earnings/market/sector reads.
    _DEFAULT_MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
    _DEFAULT_REGION = "us-east-1"
    # Short, plain output (a few sentences + a few findings), so a tight cap is ample — and
    # fewer generated tokens is the main lever on latency.
    _MAX_TOKENS = 900

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
        # Imported here, not at module load: the SDK is an optional heavyweight dependency
        # (it pulls boto3). A missing extra raises ImportError, which the wiring
        # (router.get_ratings_analysis_provider) turns into a 503.
        from anthropic import AnthropicBedrock

        self._client = AnthropicBedrock(aws_region=region)

    def analyze(
        self,
        symbol: str,
        recommendations: AnalystRecommendations | None = None,
        top_firms: tuple[FirmRating, ...] = (),
    ) -> RatingsAnalysis:
        prompt = _render_prompt(symbol, recommendations, top_firms)
        try:
            message = self._client.messages.create(
                model=self._model_id,
                max_tokens=self._MAX_TOKENS,
                system=_SYSTEM_PROMPT,
                tools=[_ANALYSIS_TOOL],
                tool_choice={"type": "tool", "name": "submit_ratings_findings"},
                messages=[{"role": "user", "content": prompt}],
            )
        except Exception as exc:  # SDK/botocore raise a family of errors; map all
            raise StockDataUnavailable(
                symbol, f"ratings analysis model call failed: {exc}"
            ) from exc
        payload = _tool_payload(message)
        if payload is None:
            raise StockDataUnavailable(
                symbol, "ratings analysis model returned no structured result"
            )
        return _to_entity(symbol, payload, self._model_id)


def _tool_payload(message) -> dict | None:
    """Pull the submit_ratings_findings arguments out of the tool call, if any."""
    for block in getattr(message, "content", None) or []:
        if (
            getattr(block, "type", None) == "tool_use"
            and getattr(block, "name", None) == "submit_ratings_findings"
        ):
            inputs = getattr(block, "input", None)
            if isinstance(inputs, dict):
                return inputs
    return None


def _to_entity(symbol: str, payload: dict, model_id: str) -> RatingsAnalysis:
    """Map the validated tool arguments onto the domain entity.

    The forced-tool schema constrains the shape, but a defensive guard keeps an off-schema
    result (e.g. an unknown ``verdict``) from leaking out as something other than this port's
    documented ``StockDataUnavailable``.
    """
    try:
        verdict = RatingsVerdict(payload["verdict"])
        confidence = Confidence(payload["confidence"])
        summary = str(payload["summary"]).strip()
    except (KeyError, ValueError) as exc:
        raise StockDataUnavailable(
            symbol, f"ratings analysis model returned an unexpected result: {exc}"
        ) from exc
    findings = _string_tuple(payload.get("findings"))
    return RatingsAnalysis(
        symbol=symbol.upper(),
        verdict=verdict,
        confidence=confidence,
        summary=summary,
        findings=findings,
        model=model_id,
        generated_at=datetime.now(timezone.utc),
    )


def _string_tuple(value) -> tuple[str, ...]:
    """Coerce the model's ``findings`` field into non-empty, stripped strings.

    Guards against a non-list: the forced tool constrains the schema, but Bedrock does not
    strictly enforce it, and Haiku occasionally returns a list field as a single string.
    Iterating a ``str`` would split it into characters — a wall of one-character "findings" —
    so anything that isn't a list yields none instead. Mirrors
    ``bedrock_earnings_analysis_provider._string_tuple``.
    """
    if not isinstance(value, list):
        return ()
    return tuple(text for item in value if (text := str(item).strip()))


def _money(value: float | None) -> str:
    """A per-share dollar figure, or a dash when missing."""
    return "n/a" if value is None else f"${value:,.2f}"


def _num(value: float | None) -> str:
    """A plain 2dp number, or a dash when missing."""
    return "n/a" if value is None else f"{value:.2f}"


def _render_prompt(
    symbol: str,
    recommendations: AnalystRecommendations | None,
    top_firms: tuple[FirmRating, ...],
) -> str:
    """Render the analyst coverage into a compact, labelled block for the model.

    The current buy/hold/sell split leads, then the consensus target range, then the most
    credible firms' stances. Only present figures are included, so sparse coverage renders a
    shorter block — the read stands on whatever it's handed.
    """
    lines = [f"Analyst coverage for {symbol.upper()}:", ""]

    latest = recommendations.latest if recommendations else None
    if latest is not None:
        lines.append("Current ratings split (how many analysts hold each stance):")
        lines.append(
            f"- Strong Buy {latest.strong_buy}, Buy {latest.buy}, Hold {latest.hold}, "
            f"Sell {latest.sell}, Strong Sell {latest.strong_sell} "
            f"({latest.total} analysts)"
        )
        if latest.consensus is not None:
            lines.append(
                f"- Consensus rating: {latest.consensus} "
                f"(score {_num(latest.score)} on a 1=Strong Buy to 5=Strong Sell scale)"
            )
        direction = recommendations.direction if recommendations else None
        if direction is not None:
            lines.append(f"- Versus last month: {direction}")
        lines.append("")

    targets = recommendations.price_targets if recommendations else None
    if targets is not None and not targets.is_empty:
        parts = []
        if targets.mean is not None:
            parts.append(f"mean {_money(targets.mean)}")
        if targets.median is not None:
            parts.append(f"median {_money(targets.median)}")
        if targets.low is not None:
            parts.append(f"low {_money(targets.low)}")
        if targets.high is not None:
            parts.append(f"high {_money(targets.high)}")
        lines.append("Consensus 12-month price target:")
        lines.append("- " + ", ".join(parts))
        lines.append("")

    if top_firms:
        lines.append(
            "Most credible covering firms (most credible first), their current stance:"
        )
        for firm in top_firms:
            parts = [firm.firm]
            if firm.rating:
                parts.append(firm.rating)
            if firm.target is not None:
                parts.append(f"target {_money(firm.target)}")
            lines.append("- " + " · ".join(parts))
        lines.append("")

    return "\n".join(lines).rstrip()
