from __future__ import annotations

from datetime import date, datetime, timezone

from app.stocks.adapters.bedrock.cost import CostAccumulator
from app.stocks.ai.brief.entities import (
    BriefHeadline,
    BriefIndexMove,
    BriefMover,
    BriefSectorMove,
    BriefTone,
    MarketBrief,
    MarketBriefContext,
    MarketBriefSection,
)
from app.stocks.ai.brief.interfaces import MarketBriefAdapter
from app.stocks.exceptions import StockDataUnavailable

# The key the adapter reports failures under — there is no single symbol here, so the market
# as a whole is named, the same convention the market-summary adapter uses.
_BRIEF_KEY = "market-brief"

# A single forced tool pins the model to structured output: Claude must call
# submit_market_brief, so the response comes back as validated JSON arguments instead of prose.
# The schema mirrors the MarketBrief entity, minus the fields the adapter stamps itself (the
# date, generated_at, model).
_SECTION_SCHEMA = {
    "type": "object",
    "properties": {
        "heading": {
            "type": "string",
            "description": (
                "A short, plain title for this part of the brief — e.g. 'Market overview', "
                "'How sectors moved', 'The day's big movers', 'What to watch'. A few words."
            ),
        },
        "body": {
            "type": "string",
            "description": (
                "2-4 short, plain-language sentences for this section, clear to someone with "
                "no finance background. Ground every statement only in the figures provided."
            ),
        },
    },
    "required": ["heading", "body"],
}

_BRIEF_TOOL = {
    "name": "submit_market_brief",
    "description": (
        "Record a short, everyday-language daily brief on how the US stock market is moving "
        "today — the overall picture and mood, how the sectors are rotating, and the day's "
        "biggest movers — grounded only in the figures in the prompt."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "summary": {
                "type": "string",
                "description": (
                    "2-3 short sentences in plain, everyday language: the day's overall "
                    "picture — is the market broadly up or down, and what's the mood — as if "
                    "to a friend who doesn't follow the markets. No jargon."
                ),
            },
            "tone": {
                "type": "string",
                "enum": [t.value for t in BriefTone],
                "description": (
                    "The market's mood today's moves imply: 'risk_on' when the market is "
                    "broadly rising and the growth-heavy, tech-leaning names are leading, "
                    "'risk_off' when it's broadly falling or the defensive sectors are "
                    "leading, 'mixed' when there's no clear lean."
                ),
            },
            "sections": {
                "type": "array",
                "items": _SECTION_SCHEMA,
                "minItems": 3,
                "maxItems": 5,
                "description": (
                    "3 to 5 short narrative sections that make up the body of the brief, in "
                    "reading order (e.g. an overview, how the sectors moved, the day's big "
                    "movers, and what to watch). Never return an empty list."
                ),
            },
        },
        "required": ["summary", "tone", "sections"],
    },
}

_SYSTEM_PROMPT = (
    "You are a friendly investing assistant writing a short daily brief on how the US stock "
    "market is doing, for an everyday person with no finance background. You are given the "
    "day's figures: how the two headline US indices moved (the S&P 500, the broad market, and "
    "the Nasdaq, the growth-heavy, tech-leaning index, each read through the ETF that tracks "
    "it), how each market sector moved, the day's biggest gaining and losing stocks, how "
    "many stocks rose versus fell, and — where available — recent news headlines about those "
    "movers, each with the outlet that ran it.\n"
    "From only those figures and headlines, write a clear, balanced brief: a 2-3 sentence "
    "summary of the day, the mood it implies, and a few short narrative sections (an overview, "
    "how the sectors rotated, the day's notable movers, and what an everyday reader might watch "
    "next).\n"
    "When a provided headline plausibly explains why a stock or the market moved, weave it in "
    "as the reason ('shares of X rose after …'), and you may name the outlet. But only use the "
    "headlines you are given — never invent news, quotes, or events, and if no headline "
    "explains a move, simply describe the move without guessing a cause.\n"
    "When the market is broadly rising and the growth-heavy Nasdaq is leading, that usually "
    "signals an optimistic, risk-taking mood; when the market is falling, or defensive sectors "
    "lead, that usually signals a more cautious, defensive mood — say which it looks like in "
    "plain words.\n"
    "Write in plain, warm, everyday language — short sentences, no jargon. Refer to the indices "
    "by name (the S&P 500, the Nasdaq), never by their ETF ticker. Ground every statement ONLY "
    "in the figures and headlines provided — do not use outside knowledge or prices you may "
    "recall, and never invent numbers. Be honest that markets carry risk and past moves don't "
    "predict future ones. This is general information, not personal financial advice. Respond "
    "by calling the submit_market_brief tool."
)


class BedrockMarketBriefAdapter(MarketBriefAdapter):
    # Full versioned inference-profile id — Haiku 4.5 has no bare alias on Bedrock, so the
    # short form 400s. (Same default as the market-summary / sector adapters.)
    _DEFAULT_MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
    _DEFAULT_REGION = "us-east-1"
    # A few sentences + a handful of short sections; a generous cap is still ample, and fewer
    # generated tokens is the main lever on latency.
    _MAX_TOKENS = 1200
    # Bedrock does not enforce the tool schema's minItems, and the fast Haiku tier occasionally
    # returns an empty sections list anyway. Re-issue the forced call this many *extra* times to
    # recover the sections. Kept at ONE: re-calling the same fast model rarely recovers what it
    # just dropped, so extra Haiku retries mostly just bill; the single retry is instead escalated
    # onto ``recovery_model_id`` (when configured). Only fires on the miss.
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
        # The model the single empty-sections retry runs on. Defaults to the primary model
        # (a plain retry); set it to a more capable entitled model to escalate the recovery
        # (see ``wiring.bedrock_recovery_model_id``).
        self._recovery_model_id = recovery_model_id or model_id
        if client is not None:
            self._client = client
            return
        # Imported here, not at module load: the SDK is an optional heavyweight dependency (it
        # pulls boto3). A missing extra raises ImportError, which the wiring turns into a 503.
        from anthropic import AnthropicBedrock

        self._client = AnthropicBedrock(aws_region=region)

    def generate(self, context: MarketBriefContext, brief_date: date) -> MarketBrief:
        prompt = _render_prompt(context)
        # One cost line per generation: the retry loop below may make several model calls, so
        # their token usage is summed and logged once (in a finally, so a mid-retry failure
        # still records what was spent).
        costs = CostAccumulator()
        try:
            payload = self._invoke(prompt, costs)
            # The forced tool asks for a few sections, but Bedrock does not enforce array
            # length and the fast Haiku tier sometimes fills only the summary. Re-issue a
            # bounded number of times to recover the sections.
            # The retry escalates onto ``recovery_model_id`` when configured (a more capable
            # model fills what the fast tier dropped). Best-effort: a failure on the recovery
            # call is swallowed so escalation can never sink a summary-present brief.
            for _ in range(self._MAX_EMPTY_RETRIES):
                if not _missing_sections(payload):
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
                    _BRIEF_KEY, "brief model returned no structured result"
                )
            return _to_entity(payload, self._model_id, brief_date)
        finally:
            costs.log(label="market brief", model_id=self._model_id)

    def _invoke(
        self, prompt: str, costs: CostAccumulator, *, model: str | None = None
    ) -> dict | None:
        chosen = model or self._model_id
        try:
            message = self._client.messages.create(
                model=chosen,
                max_tokens=self._MAX_TOKENS,
                system=_SYSTEM_PROMPT,
                tools=[_BRIEF_TOOL],
                tool_choice={"type": "tool", "name": "submit_market_brief"},
                messages=[{"role": "user", "content": prompt}],
            )
        except Exception as exc:  # SDK/botocore raise a family of errors; map them all
            raise StockDataUnavailable(
                _BRIEF_KEY, f"brief model call failed: {exc}"
            ) from exc
        costs.add(message, chosen)
        return _tool_payload(message)


def _tool_payload(message) -> dict | None:
    for block in getattr(message, "content", None) or []:
        if (
            getattr(block, "type", None) == "tool_use"
            and getattr(block, "name", None) == "submit_market_brief"
        ):
            inputs = getattr(block, "input", None)
            if isinstance(inputs, dict):
                return inputs
    return None


def _to_entity(payload: dict, model_id: str, brief_date: date) -> MarketBrief:
    try:
        summary = str(payload["summary"]).strip()
        tone = BriefTone(payload["tone"])
    except (KeyError, ValueError) as exc:
        raise StockDataUnavailable(
            _BRIEF_KEY, f"brief model returned an unexpected result: {exc}"
        ) from exc
    sections = _sections(payload.get("sections"))
    return MarketBrief(
        brief_date=brief_date,
        generated_at=datetime.now(timezone.utc),
        tone=tone,
        summary=summary,
        sections=sections,
        model=model_id,
    )


def _sections(value) -> tuple[MarketBriefSection, ...]:
    if not isinstance(value, list):
        return ()
    out: list[MarketBriefSection] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        heading = str(item.get("heading", "")).strip()
        body = str(item.get("body", "")).strip()
        if heading and body:
            out.append(MarketBriefSection(heading=heading, body=body))
    return tuple(out)


def _missing_sections(payload: dict | None) -> bool:
    if payload is None:
        return False
    return not _sections(payload.get("sections"))


def _render_prompt(context: MarketBriefContext) -> str:
    blocks: list[str] = ["US market today (each index/sector read through the ETF that tracks it):"]

    if context.indexes:
        blocks.append("Headline indices:")
        blocks.extend(_index_line(i) for i in context.indexes)

    if context.sectors:
        blocks.append("Sectors (by the day's move):")
        blocks.extend(_sector_line(s) for s in context.sectors)

    if context.quoted:
        blocks.append(
            f"Breadth: {context.advancers} stocks up vs {context.decliners} down "
            f"(of {context.quoted} with a live quote today)."
        )

    if context.gainers:
        blocks.append("Biggest gainers today:")
        blocks.extend(_mover_line(m) for m in context.gainers)
    if context.losers:
        blocks.append("Biggest losers today:")
        blocks.extend(_mover_line(m) for m in context.losers)

    if context.headlines:
        blocks.append("Recent news headlines about today's movers:")
        blocks.extend(_headline_line(h) for h in context.headlines)

    return "\n".join(blocks)


def _index_line(index: BriefIndexMove) -> str:
    parts = [f"{index.name} ({index.symbol})"]
    if index.change_percent is not None:
        parts.append(f"today {_num(index.change_percent)}%")
    for label, value in (
        ("past week", index.one_week),
        ("past month", index.one_month),
        ("past year", index.one_year),
    ):
        if value is not None:
            parts.append(f"{label} {_num(value)}%")
    return "- " + ", ".join(parts)


def _sector_line(sector: BriefSectorMove) -> str:
    move = f"{_num(sector.change_percent)}%" if sector.change_percent is not None else "n/a"
    return f"- {sector.sector}: today {move}"


def _mover_line(mover: BriefMover) -> str:
    name = mover.name or mover.ticker
    sector = f", {mover.sector}" if mover.sector else ""
    return f"- {name} ({mover.ticker}{sector}): {_num(mover.change_percent)}%"


def _headline_line(headline: BriefHeadline) -> str:
    outlet = f"{headline.publisher}: " if headline.publisher else ""
    return f"- {outlet}{headline.title} (about {headline.ticker})"


def _num(value: object) -> str:
    if isinstance(value, bool):  # bool is an int subclass — keep it as-is
        return str(value)
    if isinstance(value, float):
        return f"{value:,.2f}"
    return str(value)
