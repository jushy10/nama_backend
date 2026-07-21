from datetime import datetime, timezone

from app.stocks.adapters.bedrock.cost import CostAccumulator
from app.stocks.company.earnings.annual.entities import AnnualEarningsTimeline
from app.stocks.company.earnings.quarterly.entities import QuarterlyEarningsTimeline
from app.stocks.ai.analysis.entities import EarningsAnalysis, EarningsTrend
from app.stocks.exceptions import StockDataUnavailable
from app.stocks.ai.analysis.interfaces import EarningsAnalysisProvider

# How many recent reported periods to feed the model — enough to show a trend
# without a wall of history the plain-language read would only skim. Four quarters
# is a full year of beats, which is all the plain read needs; fewer input lines is
# a small but free token saving on every call.
_MAX_REPORTED = 4

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
                    "2-3 short sentences in plain, everyday language: how this "
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
                "maxItems": 3,
                "description": (
                    "2 to 3 short, plain-language takeaways a reader should "
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

# The recovery tool for the retry path: when the first pass packs everything into the
# summary and hands back an empty highlights list, the retry asks for *only* the
# highlights — a far shorter generation than re-running the whole analysis (summary +
# trend + highlights). Output tokens dominate this endpoint's cost, so a highlights-only
# retry is the cheap way to recover, and the narrower ask lands more reliably.
_HIGHLIGHTS_TOOL = {
    "name": "submit_highlights",
    "description": (
        "Record only the plain-language earnings highlights, grounded only in the "
        "figures provided. Two or three takeaways; never leave the list empty."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "highlights": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 2,
                "maxItems": 3,
                "description": (
                    "2 to 3 short, plain-language takeaways a reader should remember — one "
                    "clear point each (a beat streak, a growth rate, a forward "
                    "expectation). No jargon, no invented numbers. Never empty."
                ),
            },
        },
        "required": ["highlights"],
    },
}

_HIGHLIGHTS_SYSTEM = (
    "You already summarized this company's earnings. Now give ONLY the plain-language "
    "highlights — two or three short takeaways, grounded only in the figures below, each "
    "clear to someone with no finance background. Never leave the list empty. Respond by "
    "calling the submit_highlights tool."
)

# Prepended to the same figures the first pass saw, so the recovered highlights stay grounded.
_HIGHLIGHTS_INSTRUCTION = (
    "List the two or three earnings highlights for this company, grounded only in these "
    "figures:\n\n"
)

# The key the adapter reports failures under.
_KEY = "earnings-analysis"


class BedrockEarningsAnalysisProvider(EarningsAnalysisProvider):
    # Full versioned inference-profile id — Haiku 4.5 has no bare alias on
    # Bedrock, so the short form 400s. Same default as the market/sector reads.
    _DEFAULT_MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
    _DEFAULT_REGION = "us-east-1"
    # Short, plain output (a few sentences + a few highlights), so a tight cap is
    # ample — and fewer generated tokens is the main lever on latency.
    _MAX_TOKENS = 900
    # Bedrock does not enforce the tool schema's minItems, and the fast Haiku tier
    # occasionally returns an empty highlights list anyway. Re-issue the forced call
    # this many *extra* times to recover it. Kept at ONE: re-calling the same fast model
    # rarely recovers what it just dropped, so extra Haiku retries mostly just bill; the
    # single retry is instead escalated onto ``recovery_model_id`` (when configured).
    # Only fires on the miss.
    _MAX_EMPTY_RETRIES = 1

    def __init__(
        self,
        *,
        model_id: str = _DEFAULT_MODEL_ID,
        region: str = _DEFAULT_REGION,
        recovery_model_id: str | None = None,
        client=None,
    ) -> None:
        self._model_id = model_id
        # The model the single empty-list retry runs on. Defaults to the primary model (a
        # plain retry); set it to a more capable entitled model to escalate the recovery
        # (see ``wiring.bedrock_recovery_model_id``).
        self._recovery_model_id = recovery_model_id or model_id
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
        # One cost line per endpoint call: the retry loop below may make several model
        # calls, so their token usage is summed and logged once (in a finally, so a
        # mid-retry failure still records what was spent).
        costs = CostAccumulator()
        try:
            payload = self._invoke(prompt, symbol, costs)
            # The forced tool asks for a few highlights, but Bedrock does not enforce
            # array length, and the fast Haiku tier sometimes packs everything into the
            # summary and hands back an empty highlights list. Re-issue a bounded number
            # of times to recover them (this read isn't result-cached, so a view that
            # still came back empty would otherwise simply show none). Each retry asks
            # for *only* the highlights (not the whole analysis again), so it regenerates
            # a fraction of the tokens — output is this endpoint's dominant cost. A
            # recovery that doesn't land leaves the payload unchanged and consumes a
            # bounded retry, so a truly stuck read still exits.
            for _ in range(self._MAX_EMPTY_RETRIES):
                if not _missing_highlights(payload):
                    break
                recovered = self._recover_highlights(prompt, symbol, costs)
                if recovered is not None:
                    payload = _merge_highlights(payload, recovered)
            if payload is None:
                raise StockDataUnavailable(
                    symbol, "earnings analysis model returned no structured result"
                )
            return _to_entity(symbol, payload, self._model_id)
        finally:
            costs.log(label="earnings analysis", model_id=self._model_id, key=symbol)

    def _invoke(
        self,
        prompt: str,
        key: str,
        costs: CostAccumulator,
        *,
        tool: dict = _ANALYSIS_TOOL,
        tool_name: str = "submit_earnings_analysis",
        system: str = _SYSTEM_PROMPT,
        model: str | None = None,
    ) -> dict | None:
        chosen = model or self._model_id
        try:
            message = self._client.messages.create(
                model=chosen,
                max_tokens=self._MAX_TOKENS,
                system=system,
                tools=[tool],
                tool_choice={"type": "tool", "name": tool_name},
                messages=[{"role": "user", "content": prompt}],
            )
        except Exception as exc:  # SDK/botocore raise a family of errors; map all
            raise StockDataUnavailable(
                key, f"earnings analysis model call failed: {exc}"
            ) from exc
        costs.add(message, chosen)
        return _tool_payload(message, tool_name)

    def _recover_highlights(
        self, prompt: str, key: str, costs: CostAccumulator
    ) -> dict | None:
        try:
            return self._invoke(
                _HIGHLIGHTS_INSTRUCTION + prompt,
                key,
                costs,
                tool=_HIGHLIGHTS_TOOL,
                tool_name="submit_highlights",
                system=_HIGHLIGHTS_SYSTEM,
                model=self._recovery_model_id,
            )
        except StockDataUnavailable:
            return None


def _tool_payload(message, name: str) -> dict | None:
    for block in getattr(message, "content", None) or []:
        if (
            getattr(block, "type", None) == "tool_use"
            and getattr(block, "name", None) == name
        ):
            inputs = getattr(block, "input", None)
            if isinstance(inputs, dict):
                return inputs
    return None


def _merge_highlights(payload: dict, recovered: dict) -> dict:
    if _string_tuple(payload.get("highlights")):
        return payload
    if not _string_tuple(recovered.get("highlights")):
        return payload
    merged = dict(payload)
    merged["highlights"] = recovered["highlights"]
    return merged


def _to_entity(symbol: str, payload: dict, model_id: str) -> EarningsAnalysis:
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
    if not isinstance(value, list):
        return ()
    return tuple(text for item in value if (text := str(item).strip()))


def _missing_highlights(payload: dict | None) -> bool:
    if payload is None:
        return False
    return not _string_tuple(payload.get("highlights"))


def _fmt_eps(value: float | None) -> str:
    return "n/a" if value is None else f"${value:,.2f}"


def _fmt_money(value: float | None) -> str:
    if value is None:
        return "n/a"
    magnitude = abs(value)
    if magnitude >= 1e9:
        return f"${value / 1e9:,.1f}B"
    if magnitude >= 1e6:
        return f"${value / 1e6:,.1f}M"
    return f"${value:,.0f}"


def _fmt_pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:+.1f}%"


def _quarter_label(fiscal_year: int, fiscal_quarter: int) -> str:
    return f"Q{fiscal_quarter} FY{str(fiscal_year)[-2:]}"


def _render_prompt(
    symbol: str,
    quarterly: QuarterlyEarningsTimeline | None,
    annual: AnnualEarningsTimeline | None,
) -> str:
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
