"""Interface Adapter: AI market-sector analysis via Claude on Amazon Bedrock.

The market-wide sibling of ``bedrock_analysis_provider.py`` (which reads one
stock). The only module — alongside its stock/ETF cousins — that knows Bedrock
(and the Anthropic SDK) exists. It takes the day's ranked sector board the use
case already gathered (each sector's move on the day + its trailing-window
returns, read through the SPDR sector ETFs), renders it into a compact prompt,
and asks Claude for a plain-language read of which corners of the market are
leading and lagging today. Swap models or vendors and only this file changes.

The same two choices that keep the stock adapter robust apply here:

* **Auth is the runtime's job, not ours.** Bedrock authenticates through the
  process's AWS credentials (in production, the ECS task role), so there is no
  API key to read or pass — only ``model_id`` and ``region``.
* **Structured output via a forced tool call.** Claude must call
  ``submit_sector_analysis``, so the model returns validated JSON arguments that
  map straight onto the ``SectorAnalysis`` entity — no brittle prose parsing.

One sector-specific rule: the model never authors the numbers. It names the
standout sectors (by the exact names in the prompt) and writes a note for each;
the adapter joins those names back to the board to attach the *real*
``change_percent`` a sector moved, dropping any name that doesn't match. So a
highlight's percentage is always a true quote, never something the model recalled
or invented.

The Anthropic SDK is imported lazily inside ``__init__`` so the app (and the
offline test suite, which injects a stub client) imports cleanly without the
``bedrock`` extra. Any Bedrock/SDK failure is translated to
``StockDataUnavailable`` — the one error this port documents.

Docs: https://docs.anthropic.com/en/api/claude-on-amazon-bedrock
"""

from datetime import datetime, timezone

from app.stocks.entities import (
    MarketTone,
    SectorAnalysis,
    SectorHighlight,
    SectorPerformance,
)
from app.stocks.exceptions import StockDataUnavailable
from app.stocks.ports import SectorAnalysisProvider

# The key the adapter reports failures under — there is no single symbol here, so
# the board as a whole is named, the same convention the Alpaca sector adapter uses.
_SECTORS_KEY = "sectors"

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
                "One short, plain-language sentence on why this sector stands out "
                "today — clear to someone with no finance background."
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
                    "2-4 short sentences in plain, everyday language: which parts "
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
                "description": (
                    "Up to 3 standout sectors doing well today, strongest first."
                ),
            },
            "laggards": {
                "type": "array",
                "items": _HIGHLIGHT_SCHEMA,
                "description": (
                    "Up to 3 standout sectors doing poorly today, weakest first."
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
    "each sector has performed over recent weeks and months. From only those "
    "figures, give a clear, balanced read on which sectors are doing well today, "
    "which are struggling, and what the overall picture looks like.\n"
    "When growth-oriented, economically-sensitive sectors (like technology or "
    "consumer discretionary) lead, that usually signals an optimistic, "
    "risk-taking mood; when safe, defensive sectors (like utilities, consumer "
    "staples, or health care) lead, that usually signals a cautious, defensive "
    "mood — explain which it looks like in plain words.\n"
    "Write in plain, warm, everyday language — short sentences, no jargon. Refer "
    "to sectors by name, never by their ETF ticker. When you name a leading or "
    "lagging sector, copy its name EXACTLY as written in the data. Ground every "
    "statement ONLY in the figures provided — do not use outside knowledge, recent "
    "news, or prices you may recall, and never invent numbers. Be honest that a "
    "single day's move is a snapshot, not a trend. This is general information, "
    "not personal financial advice. Respond by calling the submit_sector_analysis "
    "tool."
)


class BedrockSectorAnalysisProvider(SectorAnalysisProvider):
    """Generates a ``SectorAnalysis`` with Claude on Amazon Bedrock.

    Structured exactly like ``BedrockAnalysisProvider`` (its per-stock sibling):
    defaults to the fast Haiku tier since the output is short and plain, takes
    ``model_id``/``region`` as deploy-time config (the model id may be a
    cross-region inference profile, env-overridable so a deploy can swap models
    without a code change), and accepts a ``client`` injection seam so tests can
    bypass the Anthropic SDK entirely. Otherwise the Bedrock client is built
    lazily and authenticates through the process's AWS credentials.
    """

    # Full versioned inference-profile id — Haiku 4.5 has no bare alias on Bedrock
    # (unlike us.anthropic.claude-sonnet-4-6), so the short form 400s with "invalid
    # model identifier". Verified ACTIVE + invokable in us-east-1.
    _DEFAULT_MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
    _DEFAULT_REGION = "us-east-1"
    # Short, plain output (a few sentences + two brief highlight lists), so a tight
    # cap is ample — and fewer generated tokens is the main lever on latency. Kept
    # above the worst case so a full read is never truncated.
    _MAX_TOKENS = 800

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
        # the wiring (router.get_sector_analysis_provider) turns into a 503.
        from anthropic import AnthropicBedrock

        self._client = AnthropicBedrock(aws_region=region)

    def analyze(self, sectors: list[SectorPerformance]) -> SectorAnalysis:
        prompt = _render_prompt(sectors)
        try:
            message = self._client.messages.create(
                model=self._model_id,
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
        payload = _tool_payload(message)
        if payload is None:
            raise StockDataUnavailable(
                _SECTORS_KEY, "analysis model returned no structured result"
            )
        return _to_entity(sectors, payload, self._model_id)


def _tool_payload(message) -> dict | None:
    """Pull the submit_sector_analysis arguments out of the tool call, if any."""
    for block in getattr(message, "content", None) or []:
        if (
            getattr(block, "type", None) == "tool_use"
            and getattr(block, "name", None) == "submit_sector_analysis"
        ):
            inputs = getattr(block, "input", None)
            if isinstance(inputs, dict):
                return inputs
    return None


def _to_entity(
    sectors: list[SectorPerformance], payload: dict, model_id: str
) -> SectorAnalysis:
    """Map the validated tool arguments onto the domain entity.

    The forced-tool schema constrains the shape, but a defensive guard keeps an
    off-schema result (e.g. an unknown ``tone``) from leaking out as something
    other than this port's documented ``StockDataUnavailable``. The leader/laggard
    notes are joined back to the board so each highlight carries the sector's real
    day move, not a figure the model authored.
    """
    try:
        summary = str(payload["summary"]).strip()
        tone = MarketTone(payload["tone"])
    except (KeyError, ValueError) as exc:
        raise StockDataUnavailable(
            _SECTORS_KEY, f"analysis model returned an unexpected result: {exc}"
        ) from exc
    by_name = {s.sector.casefold(): s for s in sectors}
    return SectorAnalysis(
        summary=summary,
        tone=tone,
        leaders=_highlights(payload.get("leaders"), by_name),
        laggards=_highlights(payload.get("laggards"), by_name),
        model=model_id,
        generated_at=datetime.now(timezone.utc),
    )


def _highlights(
    value, by_name: dict[str, SectorPerformance]
) -> tuple[SectorHighlight, ...]:
    """Coerce the model's highlight list into entities, joining each to the board.

    A highlight is kept only when its name matches a sector on the board (so the
    ``change_percent`` is a real quote) and it carries a non-empty note; anything
    the model named that isn't on the board — or named with no note — is dropped.
    """
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
            )
        )
    return tuple(out)


def _render_prompt(sectors: list[SectorPerformance]) -> str:
    """Render the ranked board into a compact, labelled block for the model.

    One line per sector, best performer first (the order the use case ranks in),
    each carrying its day move and whatever trailing windows are available — only
    present figures are included, so a sector missing history simply renders a
    shorter line.
    """
    lines = ["Market sectors today (ranked best performer first):"]
    for s in sectors:
        parts = [f"{s.sector} ({s.symbol})"]
        if s.change_percent is not None:
            parts.append(f"day {_num(s.change_percent)}%")
        perf = s.performance
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
    return "\n".join(lines)


def _num(value: object) -> str:
    """Format a numeric field readably; pass non-numbers through unchanged."""
    if isinstance(value, bool):  # bool is an int subclass — keep it as-is
        return str(value)
    if isinstance(value, float):
        return f"{value:,.2f}"
    return str(value)
