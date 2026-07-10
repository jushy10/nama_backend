"""Interface Adapter: AI earnings summary via Claude on Amazon Bedrock.

The earnings-focused sibling of ``analysis_adapter.py`` (the full
buy/hold/sell read) and ``market_summary_adapter.py`` (the whole-market
read). The only module — alongside its stock/ETF/sector/market cousins — that
knows Bedrock (and the Anthropic SDK) exists. It takes the quarterly and annual
earnings timelines the use case gathered, renders them into a compact prompt,
and asks Claude for a plain-language read of the company's earnings story: how
consistently it beats, where EPS and revenue are trending, and what the forward
consensus expects. Swap models or vendors and only this file changes.

The same two choices that keep the market-summary adapter robust apply here:

* **Auth is the runtime's job, not ours.** Bedrock authenticates through the
  process's AWS credentials (in production, the ECS task role), so there is no
  API key to read or pass — only ``model_id`` and ``region``.
* **Structured output via a forced tool call.** Claude must call
  ``submit_earnings_analysis``, so the model returns validated JSON arguments
  that map straight onto the ``EarningsAnalysis`` entity — no prose parsing.

The prompt carries the *real* figures (beats, EPS, revenue, forward consensus)
and the model writes only plain prose over them — it never authors a number that
reaches the card. The Anthropic SDK is imported lazily inside ``__init__`` so the
app (and the offline test suite, which injects a stub client) imports cleanly
without the ``bedrock`` extra. Any Bedrock/SDK failure is translated to
``StockDataUnavailable`` — the one error this port documents.

Docs: https://docs.anthropic.com/en/api/claude-on-amazon-bedrock
"""

from datetime import datetime, timezone

from app.stocks.earnings.annual.entities import AnnualEarningsTimeline
from app.stocks.earnings.quarterly.entities import QuarterlyEarningsTimeline
from app.stocks.entities import EarningsAnalysis, EarningsTrend
from app.stocks.exceptions import StockDataUnavailable
from app.stocks.ports import EarningsAnalysisProvider

# How many recent reported periods to feed the model — enough to show a trend
# without a wall of history the plain-language read would only skim.
_MAX_REPORTED = 6

# A single forced tool pins the model to structured output: Claude must call
# submit_earnings_analysis, so the response comes back as validated JSON
# arguments instead of prose. The schema mirrors the EarningsAnalysis entity,
# minus the fields the adapter stamps itself (symbol, model, generated_at).
_ANALYSIS_TOOL = {
    "name": "submit_earnings_analysis",
    "description": (
        "Record a plain, everyday-language read of a company's recent earnings — "
        "how reliably it beats expectations, where profit (EPS) and sales "
        "(revenue) are trending, and what analysts expect next — grounded only in "
        "the figures in the prompt."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "summary": {
                "type": "string",
                "description": (
                    "2-4 short sentences in plain, everyday language: how this "
                    "company's earnings have been going — whether it tends to beat "
                    "or miss, how its profit and sales are trending, and what's "
                    "expected next — as if to a friend who doesn't follow markets. "
                    "No jargon."
                ),
            },
            "trend": {
                "type": "string",
                "enum": [t.value for t in EarningsTrend],
                "description": (
                    "Where the earnings story is heading: 'accelerating' when "
                    "profit/sales growth is picking up (or beats are getting "
                    "bigger), 'slowing' when growth is fading or the company is "
                    "starting to miss, 'steady' when it's holding a consistent "
                    "pace."
                ),
            },
            "highlights": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 2,
                "maxItems": 4,
                "description": (
                    "2 to 4 short, plain-language takeaways a reader should "
                    "remember — one clear point each (e.g. a beat streak, a growth "
                    "rate, a forward expectation). No jargon, no invented numbers. "
                    "Always give at least two; never return an empty list."
                ),
            },
        },
        "required": ["summary", "trend", "highlights"],
    },
}

_SYSTEM_PROMPT = (
    "You are a friendly investing assistant explaining a company's earnings to an "
    "everyday person with no finance background. You are given its recent reported "
    "quarters and fiscal years — the profit per share (EPS) it reported, the "
    "estimate analysts expected, whether it beat or missed, its revenue (sales), "
    "and the consensus estimates for the quarters and years still ahead. From only "
    "those figures, give a clear, balanced read of how its earnings have been "
    "going and where they look headed.\n"
    "Explain in plain words whether the company reliably beats expectations, "
    "whether its profit and sales are growing, shrinking, or holding steady, and "
    "what analysts expect for the upcoming periods. A company beating by more each "
    "time or growing faster is 'accelerating'; one starting to miss or slowing its "
    "growth is 'slowing'; a consistent pace is 'steady'.\n"
    "Write in plain, warm, everyday language — short sentences, no jargon (say "
    "'profit per share' or 'EPS', 'sales' or 'revenue'; avoid terms like 'consensus "
    "surprise' or 'guidance'). Ground every statement ONLY in the figures provided "
    "— do not use outside knowledge, recent news, or numbers you may recall, and "
    "never invent figures. Be honest that estimates can be wrong and past results "
    "don't guarantee future ones. Always give at least two highlights — never "
    "leave that list empty. This is general information, not personal financial "
    "advice. Respond by calling the submit_earnings_analysis tool."
)

# The key the adapter reports failures under.
_KEY = "earnings-analysis"


class BedrockEarningsAnalysisProvider(EarningsAnalysisProvider):
    """Generates an ``EarningsAnalysis`` with Claude on Amazon Bedrock.

    Structured exactly like ``BedrockMarketSummaryProvider`` (its market sibling):
    defaults to the fast Haiku tier since the output is short and plain, takes
    ``model_id``/``region`` as deploy-time config (the model id may be a
    cross-region inference profile, env-overridable so a deploy can swap models
    without a code change), and accepts a ``client`` injection seam so tests can
    bypass the Anthropic SDK entirely. Otherwise the Bedrock client is built
    lazily and authenticates through the process's AWS credentials.
    """

    # Full versioned inference-profile id — Haiku 4.5 has no bare alias on
    # Bedrock, so the short form 400s. Same default as the market/sector reads.
    _DEFAULT_MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
    _DEFAULT_REGION = "us-east-1"
    # Short, plain output (a few sentences + a few highlights), so a tight cap is
    # ample — and fewer generated tokens is the main lever on latency.
    _MAX_TOKENS = 900
    # Bedrock does not enforce the tool schema's minItems, and the fast Haiku tier
    # occasionally returns an empty highlights list anyway. Re-issue the forced call
    # up to this many *extra* times to recover it. Only fires on the miss.
    _MAX_EMPTY_RETRIES = 2

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
        # dependency (it pulls boto3). A missing extra raises ImportError, which
        # the wiring (router.get_earnings_analysis_provider) turns into a 503.
        from anthropic import AnthropicBedrock

        self._client = AnthropicBedrock(aws_region=region)

    def analyze(
        self,
        symbol: str,
        quarterly: QuarterlyEarningsTimeline | None = None,
        annual: AnnualEarningsTimeline | None = None,
    ) -> EarningsAnalysis:
        prompt = _render_prompt(symbol, quarterly, annual)
        payload = self._invoke(prompt, symbol)
        # The forced tool asks for a few highlights, but Bedrock does not enforce
        # array length, and the fast Haiku tier sometimes packs everything into the
        # summary and hands back an empty highlights list. Re-issue a bounded number
        # of times to recover them (this read isn't result-cached, so a view that
        # still came back empty would otherwise simply show none).
        for _ in range(self._MAX_EMPTY_RETRIES):
            if not _missing_highlights(payload):
                break
            payload = self._invoke(prompt, symbol) or payload
        if payload is None:
            raise StockDataUnavailable(
                symbol, "earnings analysis model returned no structured result"
            )
        return _to_entity(symbol, payload, self._model_id)

    def _invoke(self, prompt: str, key: str) -> dict | None:
        """One forced-tool call, returning the ``submit_earnings_analysis``
        arguments (or ``None`` if the model somehow didn't call the tool). Any
        SDK/botocore failure is mapped to this port's ``StockDataUnavailable``."""
        try:
            message = self._client.messages.create(
                model=self._model_id,
                max_tokens=self._MAX_TOKENS,
                system=_SYSTEM_PROMPT,
                tools=[_ANALYSIS_TOOL],
                tool_choice={"type": "tool", "name": "submit_earnings_analysis"},
                messages=[{"role": "user", "content": prompt}],
            )
        except Exception as exc:  # SDK/botocore raise a family of errors; map all
            raise StockDataUnavailable(
                key, f"earnings analysis model call failed: {exc}"
            ) from exc
        return _tool_payload(message)


def _tool_payload(message) -> dict | None:
    """Pull the submit_earnings_analysis arguments out of the tool call, if any."""
    for block in getattr(message, "content", None) or []:
        if (
            getattr(block, "type", None) == "tool_use"
            and getattr(block, "name", None) == "submit_earnings_analysis"
        ):
            inputs = getattr(block, "input", None)
            if isinstance(inputs, dict):
                return inputs
    return None


def _to_entity(symbol: str, payload: dict, model_id: str) -> EarningsAnalysis:
    """Map the validated tool arguments onto the domain entity.

    The forced-tool schema constrains the shape, but a defensive guard keeps an
    off-schema result (e.g. an unknown ``trend``) from leaking out as something
    other than this port's documented ``StockDataUnavailable``.
    """
    try:
        summary = str(payload["summary"]).strip()
        trend = EarningsTrend(payload["trend"])
    except (KeyError, ValueError) as exc:
        raise StockDataUnavailable(
            symbol, f"earnings analysis model returned an unexpected result: {exc}"
        ) from exc
    highlights = _string_tuple(payload.get("highlights"))
    return EarningsAnalysis(
        symbol=symbol.upper(),
        summary=summary,
        trend=trend,
        highlights=highlights,
        model=model_id,
        generated_at=datetime.now(timezone.utc),
    )


def _string_tuple(value) -> tuple[str, ...]:
    """Coerce the model's ``highlights`` field into non-empty, stripped strings.

    Guards against a non-list: the forced tool constrains the schema, but Bedrock
    does not strictly enforce it, and Haiku occasionally returns ``highlights`` as
    a single string (e.g. a leaked ``<parameter name="highlights">[...]`` value).
    Iterating a ``str`` would split it into characters — a wall of one-character
    "notes" — so anything that isn't a list yields no highlights instead. Mirrors
    ``analysis_adapter._string_tuple``.
    """
    if not isinstance(value, list):
        return ()
    return tuple(text for item in value if (text := str(item).strip()))


def _missing_highlights(payload: dict | None) -> bool:
    """True when a returned tool result is present but carries no usable highlights
    — the signal to retry. A ``None`` payload (the model didn't call the tool at
    all) is left for the caller to surface as ``StockDataUnavailable``, not
    retried, so the existing no-structured-result path is unchanged."""
    if payload is None:
        return False
    return not _string_tuple(payload.get("highlights"))


def _fmt_eps(value: float | None) -> str:
    """A per-share dollar figure, or a dash when missing."""
    return "n/a" if value is None else f"${value:,.2f}"


def _fmt_money(value: float | None) -> str:
    """A raw revenue/income figure compacted to billions/millions."""
    if value is None:
        return "n/a"
    magnitude = abs(value)
    if magnitude >= 1e9:
        return f"${value / 1e9:,.1f}B"
    if magnitude >= 1e6:
        return f"${value / 1e6:,.1f}M"
    return f"${value:,.0f}"


def _fmt_pct(value: float | None) -> str:
    """A signed percent, or a dash when missing."""
    return "n/a" if value is None else f"{value:+.1f}%"


def _quarter_label(fiscal_year: int, fiscal_quarter: int) -> str:
    return f"Q{fiscal_quarter} FY{str(fiscal_year)[-2:]}"


def _render_prompt(
    symbol: str,
    quarterly: QuarterlyEarningsTimeline | None,
    annual: AnnualEarningsTimeline | None,
) -> str:
    """Render the earnings timelines into a compact, labelled block for the model.

    Reported quarters/years lead with their real figures; a short beat tally and
    the forward consensus follow. Only present figures are included, so a sparse
    timeline renders a shorter block — the read stands on whatever it's handed.
    """
    lines = [f"Earnings for {symbol.upper()} (most recent first):", ""]

    reported_q = list((quarterly.past if quarterly else ()))[-_MAX_REPORTED:]
    if reported_q:
        beats = sum(1 for q in reported_q if q.beat)
        scored = sum(1 for q in reported_q if q.beat is not None)
        lines.append(
            f"Reported quarters (beat or met the estimate in {beats} of {scored}):"
        )
        for q in reversed(reported_q):
            label = _quarter_label(q.fiscal_year, q.fiscal_quarter)
            parts = [
                f"{label}: EPS {_fmt_eps(q.eps_actual)} vs est "
                f"{_fmt_eps(q.eps_estimate)}"
            ]
            if q.eps_surprise_percent is not None:
                parts.append(f"({_fmt_pct(q.eps_surprise_percent)} surprise)")
            if q.revenue_actual is not None:
                parts.append(f"revenue {_fmt_money(q.revenue_actual)}")
            lines.append("- " + ", ".join(parts))
        lines.append("")

    upcoming_q = list((quarterly.future if quarterly else ()))[:3]
    if upcoming_q:
        lines.append("Upcoming quarters (analyst consensus):")
        for q in upcoming_q:
            label = _quarter_label(q.fiscal_year, q.fiscal_quarter)
            parts = [f"{label}: est EPS {_fmt_eps(q.eps_estimate)}"]
            if q.revenue_estimate is not None:
                parts.append(f"est revenue {_fmt_money(q.revenue_estimate)}")
            lines.append("- " + ", ".join(parts))
        lines.append("")

    reported_y = list((annual.past if annual else ()))[-4:]
    if reported_y:
        lines.append("Reported fiscal years:")
        for y in reversed(reported_y):
            eps = y.eps_actual_consensus if y.eps_actual_consensus is not None else y.eps_actual
            parts = [f"FY{str(y.fiscal_year)[-2:]}: EPS {_fmt_eps(eps)}"]
            if y.revenue_actual is not None:
                parts.append(f"revenue {_fmt_money(y.revenue_actual)}")
            if y.net_income is not None:
                parts.append(f"net income {_fmt_money(y.net_income)}")
            lines.append("- " + ", ".join(parts))
        lines.append("")

    upcoming_y = list((annual.future if annual else ()))[:2]
    if upcoming_y:
        lines.append("Upcoming fiscal years (analyst consensus):")
        for y in upcoming_y:
            parts = [f"FY{str(y.fiscal_year)[-2:]}: est EPS {_fmt_eps(y.eps_estimate)}"]
            if y.revenue_estimate is not None:
                parts.append(f"est revenue {_fmt_money(y.revenue_estimate)}")
            lines.append("- " + ", ".join(parts))
        lines.append("")

    return "\n".join(lines).rstrip()
