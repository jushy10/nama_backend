import logging
from datetime import datetime, timezone

from app.adapters.bedrock.cost import CostAccumulator
from app.domains.research.analysis.entities import (
    MarketTone,
    SectorAnalysis,
    SectorContext,
    SectorHighlight,
)
from app.domains.research.analysis.interfaces import SectorAnalysisAdapter
from app.domains.shared.exceptions import StockDataUnavailable

# The key the adapter reports failures under — there is no single symbol here, so
# the board as a whole is named, the same convention the Alpaca sector adapter uses.
_SECTORS_KEY = "sectors"

logger = logging.getLogger(__name__)

# A single forced tool pins the model to structured output: Claude must call
# submit_sector_analysis, so the response comes back as validated JSON arguments
# instead of prose. The schema mirrors the SectorAnalysis entity, minus the fields
# the adapter stamps itself (model, generated_at) and the highlight change_percent
# the adapter joins from the board rather than trusting the model to author.
_HIGHLIGHT_SCHEMA = {
    "type": "object",
    "properties": {
        "sector": {
            "type": "string",
            "description": (
                "The sector's name, copied EXACTLY as it appears in the prompt "
                "(e.g. 'Technology', 'Consumer Discretionary')."
            ),
        },
        "note": {
            "type": "string",
            "description": (
                "One short, plain-language sentence on *why* this sector stands out "
                "today — clear to someone with no finance background. Point to what's "
                "driving it: name the specific big-name stocks moving the sector (from "
                "the 'driven by' list shown for it) and, if a headline explains the "
                "move, mention that reason. Refer to companies by name, not ticker. "
                "Ground it only in the movers and headlines shown for this sector — "
                "never invent a stock, a number, or a reason."
            ),
        },
    },
    "required": ["sector", "note"],
}

_ANALYSIS_TOOL = {
    "name": "submit_sector_analysis",
    "description": (
        "Record a plain, everyday-language read of how the market's sectors are "
        "moving today — which are leading, which are lagging, and what that says "
        "about the market's mood — grounded only in the figures in the prompt."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "summary": {
                "type": "string",
                "description": (
                    "2-3 short sentences in plain, everyday language: which parts "
                    "of the market are up today and which are down, and the overall "
                    "picture — as if to a friend who doesn't follow the markets. "
                    "No jargon."
                ),
            },
            "tone": {
                "type": "string",
                "enum": [t.value for t in MarketTone],
                "description": (
                    "The market's mood the day's rotation implies: 'risk_on' when "
                    "growth/cyclical sectors (technology, consumer discretionary) "
                    "lead, 'risk_off' when defensive sectors (utilities, consumer "
                    "staples, health care) lead, 'mixed' when there's no clear lean."
                ),
            },
            "leaders": {
                "type": "array",
                "items": _HIGHLIGHT_SCHEMA,
                "minItems": 2,
                "maxItems": 3,
                "description": (
                    "2 to 3 standout sectors doing well today, strongest first. "
                    "Always name at least two; never return an empty list."
                ),
            },
            "laggards": {
                "type": "array",
                "items": _HIGHLIGHT_SCHEMA,
                "minItems": 2,
                "maxItems": 3,
                "description": (
                    "2 to 3 standout sectors doing poorly today, weakest first. "
                    "Always name at least two; never return an empty list."
                ),
            },
        },
        "required": ["summary", "tone", "leaders", "laggards"],
    },
}

_SYSTEM_PROMPT = (
    "You are a friendly investing assistant explaining the stock market's day to "
    "an everyday person with no finance background. You are given today's move for "
    "each of the major market sectors (technology, energy, financials, and so on), "
    "read through the exchange-traded fund that tracks each one, along with how "
    "each sector has performed over recent weeks and months. For each sector you are "
    "ALSO given the specific big-name stocks driving its move today (with their own "
    "day change), how broadly the sector moved (how many of its companies rose vs. "
    "fell), and — where available — a recent headline about one of those movers. Use "
    "all of it to give a clear, balanced read on which sectors are doing well today, "
    "which are struggling, WHY, and what the overall picture looks like.\n"
    "Explaining the 'why' is the point: when a sector stands out, say what's behind "
    "it — the specific companies moving it (e.g. 'lifted by strong gains in Nvidia "
    "and Broadcom'), whether the move was broad or driven by just a few names (use "
    "the up-vs-down count), and any reason a headline gives. \n"
    "When growth-oriented, economically-sensitive sectors (like technology or "
    "consumer discretionary) lead, that usually signals an optimistic, "
    "risk-taking mood; when safe, defensive sectors (like utilities, consumer "
    "staples, or health care) lead, that usually signals a cautious, defensive "
    "mood — explain which it looks like in plain words.\n"
    "Write in plain, warm, everyday language — short sentences, no jargon. Refer "
    "to sectors and companies by name, never by ticker. When you name a leading or "
    "lagging sector, copy its name EXACTLY as written in the data. Ground every "
    "statement ONLY in the figures, movers, and headlines provided — do NOT use "
    "outside knowledge or prices you may recall, do not invent a stock, a number, "
    "or a reason, and if no headline explains a move, simply describe which stocks "
    "moved it without guessing a cause. Always name at least two leading and two "
    "lagging sectors — never leave either list empty. Be honest that a single day's "
    "move is a snapshot, not a trend. This is general information, not personal "
    "financial advice. Respond by calling the submit_sector_analysis tool."
)


class SectorAnalysisAdapterImpl(SectorAnalysisAdapter):
    # Full versioned inference-profile id — Haiku 4.5 has no bare alias on Bedrock
    # (unlike us.anthropic.claude-sonnet-4-6), so the short form 400s with "invalid
    # model identifier". Verified ACTIVE + invokable in us-east-1.
    _DEFAULT_MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
    _DEFAULT_REGION = "us-east-1"
    # Short, plain output (a few sentences + two brief highlight lists), so a tight
    # cap is ample — and fewer generated tokens is the main lever on latency. Kept
    # above the worst case so a full read is never truncated.
    _MAX_TOKENS = 800
    # Bedrock does not enforce the tool schema's minItems, and the fast Haiku tier
    # occasionally returns empty leaders/laggards anyway. Re-issue the forced call this
    # many *extra* times to recover them. Kept at ONE: re-calling the same fast model
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
        # the wiring (router.get_sector_analysis_provider) turns into a 503.
        from anthropic import AnthropicBedrock

        self._client = AnthropicBedrock(aws_region=region)

    def analyze(self, contexts: list[SectorContext]) -> SectorAnalysis:
        prompt = _render_prompt(contexts)
        # The exact text the model reasons over — the ground truth for "did a headline
        # even reach the model". At DEBUG so it stays off in normal INFO logging (it's
        # multi-line); crank the app log level to see per-call what was sent.
        logger.debug("sector analysis prompt:\n%s", prompt)
        # One cost line per endpoint call: the retry loop below may make several model
        # calls, so their token usage is summed and logged once (in a finally, so a
        # mid-retry failure still records what was spent).
        costs = CostAccumulator()
        try:
            payload = self._invoke(prompt, costs)
            # The forced tool asks for both a leaders and a laggards list, but Bedrock
            # does not enforce array length, and the fast Haiku tier sometimes fills
            # only the summary and hands back empty lists. Re-issue a bounded number of
            # times to recover them (this read isn't result-cached, so a view that still
            # came back empty would otherwise simply show none).
            # The retry escalates onto ``recovery_model_id`` when configured (a more
            # capable model fills what the fast tier dropped and yields a *complete*
            # read the use case caches). Best-effort: a failure on the recovery call is
            # swallowed so escalation can never sink a summary-present read.
            for _ in range(self._MAX_EMPTY_RETRIES):
                if not _missing_highlights(payload):
                    break
                try:
                    recovered = self._invoke(
                        prompt, costs, model=self._recovery_model_id
                    )
                except StockDataUnavailable:
                    recovered = None
                if recovered is not None:
                    payload = recovered
            if payload is None:
                raise StockDataUnavailable(
                    _SECTORS_KEY, "analysis model returned no structured result"
                )
            return _to_entity(contexts, payload, self._model_id)
        finally:
            costs.log(label="sector analysis", model_id=self._model_id)

    def _invoke(
        self, prompt: str, costs: CostAccumulator, *, model: str | None = None
    ) -> dict | None:
        chosen = model or self._model_id
        try:
            message = self._client.messages.create(
                model=chosen,
                max_tokens=self._MAX_TOKENS,
                system=_SYSTEM_PROMPT,
                tools=[_ANALYSIS_TOOL],
                tool_choice={"type": "tool", "name": "submit_sector_analysis"},
                messages=[{"role": "user", "content": prompt}],
            )
        except Exception as exc:  # SDK/botocore raise a family of errors; map them all
            raise StockDataUnavailable(
                _SECTORS_KEY, f"analysis model call failed: {exc}"
            ) from exc
        costs.add(message, chosen)
        return _tool_payload(message)


def _tool_payload(message) -> dict | None:
    for block in getattr(message, "content", None) or []:
        if (
            getattr(block, "type", None) == "tool_use"
            and getattr(block, "name", None) == "submit_sector_analysis"
        ):
            inputs = getattr(block, "input", None)
            if isinstance(inputs, dict):
                return inputs
    return None


def _missing_highlights(payload: dict | None) -> bool:
    if payload is None:
        return False
    return not payload.get("leaders") or not payload.get("laggards")


def _to_entity(
    contexts: list[SectorContext], payload: dict, model_id: str
) -> SectorAnalysis:
    try:
        summary = str(payload["summary"]).strip()
        tone = MarketTone(payload["tone"])
    except (KeyError, ValueError) as exc:
        raise StockDataUnavailable(
            _SECTORS_KEY, f"analysis model returned an unexpected result: {exc}"
        ) from exc
    by_name = {c.sector.casefold(): c for c in contexts}
    return SectorAnalysis(
        summary=summary,
        tone=tone,
        leaders=_highlights(payload.get("leaders"), by_name),
        laggards=_highlights(payload.get("laggards"), by_name),
        model=model_id,
        generated_at=datetime.now(timezone.utc),
    )


def _highlights(
    value, by_name: dict[str, SectorContext]
) -> tuple[SectorHighlight, ...]:
    if not isinstance(value, list):
        return ()
    out: list[SectorHighlight] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        note = str(item.get("note", "")).strip()
        match = by_name.get(str(item.get("sector", "")).strip().casefold())
        if match is None or not note:
            continue
        out.append(
            SectorHighlight(
                sector=match.sector,
                symbol=match.symbol,
                change_percent=match.change_percent,
                note=note,
                movers=match.movers,
                headlines=match.headlines,
            )
        )
    return tuple(out)


def _render_prompt(contexts: list[SectorContext]) -> str:
    lines = ["Market sectors today (ranked best performer first):"]
    for c in contexts:
        parts = [f"{c.sector} ({c.symbol})"]
        if c.change_percent is not None:
            parts.append(f"day {_num(c.change_percent)}%")
        perf = c.performance
        if perf is not None:
            for label, value in (
                ("1w", perf.one_week),
                ("1m", perf.one_month),
                ("3m", perf.three_month),
                ("6m", perf.six_month),
                ("ytd", perf.ytd),
                ("1y", perf.one_year),
            ):
                if value is not None:
                    parts.append(f"{label} {_num(value)}%")
        lines.append("- " + ", ".join(parts))
        if c.movers:
            movers = "; ".join(
                f"{m.name or m.ticker} {_signed(m.change_percent)}" for m in c.movers
            )
            lines.append(f"    driven by: {movers}")
        if c.breadth is not None and c.breadth.total:
            b = c.breadth
            lines.append(
                f"    breadth: {b.advancers} of {b.total} companies up, {b.decliners} down"
            )
        for h in c.headlines:
            lines.append(f"    headline ({h.ticker}): {h.title}")
    return "\n".join(lines)


def _signed(value: object) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "n/a"
    return f"{value:+,.2f}%"


def _num(value: object) -> str:
    if isinstance(value, bool):  # bool is an int subclass — keep it as-is
        return str(value)
    if isinstance(value, float):
        return f"{value:,.2f}"
    return str(value)
