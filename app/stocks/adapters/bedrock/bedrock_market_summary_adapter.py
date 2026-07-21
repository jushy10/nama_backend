from datetime import datetime, timezone

from app.stocks.adapters.bedrock.cost import CostAccumulator
from app.stocks.ai.analysis.entities import (
    MarketIndexReturn,
    MarketPeriod,
    MarketPeriodHighlight,
    MarketSummary,
    MarketTone,
)
from app.stocks.ai.analysis.interfaces import MarketSummaryProvider
from app.stocks.exceptions import StockDataUnavailable
from app.stocks.market.boards.entities import MarketIndexPerformance

# The key the adapter reports failures under — there is no single symbol here, so
# the market as a whole is named, the same convention the Alpaca overview uses.
_MARKET_KEY = "market"

# The periods rendered, in the order the card reads (year -> month -> week), each
# mapped to the trailing-window field it reads off ``StockPerformance``.
_PERIOD_WINDOW: dict[MarketPeriod, str] = {
    MarketPeriod.YEAR: "one_year",
    MarketPeriod.MONTH: "one_month",
    MarketPeriod.WEEK: "one_week",
}

# A single forced tool pins the model to structured output: Claude must call
# submit_market_summary, so the response comes back as validated JSON arguments
# instead of prose. The schema mirrors the MarketSummary entity, minus the fields
# the adapter stamps itself (model, generated_at) and the index returns the
# adapter joins from the board rather than trusting the model to author.
_PERIOD_SCHEMA = {
    "type": "object",
    "properties": {
        "period": {
            "type": "string",
            "enum": [p.value for p in MarketPeriod],
            "description": (
                "Which timeframe this note covers: 'year' (the past year), "
                "'month' (the past month), or 'week' (the past week)."
            ),
        },
        "note": {
            "type": "string",
            "description": (
                "One short, plain-language sentence on how the US market did over "
                "this timeframe — clear to someone with no finance background."
            ),
        },
    },
    "required": ["period", "note"],
}

_SUMMARY_TOOL = {
    "name": "submit_market_summary",
    "description": (
        "Record a plain, everyday-language overview of how the US stock market "
        "has moved over the past year, month and week — the overall picture and "
        "the mood it implies — grounded only in the figures in the prompt."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "summary": {
                "type": "string",
                "description": (
                    "2-3 short sentences in plain, everyday language: the overall "
                    "picture of how the US market (the S&P 500 and the Nasdaq) has "
                    "done over the past year, month and week — as if to a friend "
                    "who doesn't follow the markets. No jargon."
                ),
            },
            "tone": {
                "type": "string",
                "enum": [t.value for t in MarketTone],
                "description": (
                    "The market's mood the recent moves imply: 'risk_on' when the "
                    "market is broadly rising and the growth-heavy Nasdaq is "
                    "leading, 'risk_off' when the market is falling or the broad "
                    "S&P is holding up better than the Nasdaq (a defensive lean), "
                    "'mixed' when there's no clear lean."
                ),
            },
            "periods": {
                "type": "array",
                "items": _PERIOD_SCHEMA,
                "minItems": 3,
                "maxItems": 3,
                "description": (
                    "One note for EACH timeframe — the past year, the past month, "
                    "and the past week (exactly three notes, one per timeframe; "
                    "never return an empty list)."
                ),
            },
        },
        "required": ["summary", "tone", "periods"],
    },
}

_SYSTEM_PROMPT = (
    "You are a friendly investing assistant explaining how the US stock market "
    "has been doing to an everyday person with no finance background. You are "
    "given how the two headline US indices — the S&P 500 (the broad market) and "
    "the Nasdaq (the growth-heavy, tech-leaning index) — have moved over the past "
    "week, month and year, read through the exchange-traded fund that tracks each "
    "one. From only those figures, give a clear, balanced read on how the market "
    "has done over the past year, then the past month, then the past week, and "
    "what the overall picture looks like.\n"
    "When the market is broadly rising and the growth-heavy Nasdaq is leading, "
    "that usually signals an optimistic, risk-taking mood; when the market is "
    "falling, or the broad S&P is holding up better than the Nasdaq, that usually "
    "signals a more cautious, defensive mood — explain which it looks like in "
    "plain words.\n"
    "Write in plain, warm, everyday language — short sentences, no jargon. Refer "
    "to the indices by name (the S&P 500, the Nasdaq), never by their ETF ticker. "
    "Ground every statement ONLY in the figures provided — do not use outside "
    "knowledge, recent news, or prices you may recall, and never invent numbers. "
    "Always include a note for all three timeframes — never leave that list empty. "
    "Be honest that markets carry risk and past moves don't predict future ones. "
    "This is general information, not personal financial advice. Respond by "
    "calling the submit_market_summary tool."
)


class BedrockMarketSummaryProvider(MarketSummaryProvider):
    # Full versioned inference-profile id — Haiku 4.5 has no bare alias on Bedrock,
    # so the short form 400s with "invalid model identifier". Verified ACTIVE +
    # invokable in us-east-1. (Same default as the sector adapter.)
    _DEFAULT_MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
    _DEFAULT_REGION = "us-east-1"
    # Short, plain output (a few sentences + three brief period notes), so a tight
    # cap is ample — and fewer generated tokens is the main lever on latency.
    _MAX_TOKENS = 800
    # Bedrock does not enforce the tool schema's minItems, and the fast Haiku tier
    # occasionally returns an empty periods list anyway. Re-issue the forced call this
    # many *extra* times to recover the notes. Kept at ONE: re-calling the same fast
    # model rarely recovers what it just dropped, so extra Haiku retries mostly just
    # bill; the single retry is instead escalated onto ``recovery_model_id`` (when
    # configured). Only fires on the miss.
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
        # The model the single empty-notes retry runs on. Defaults to the primary model
        # (a plain retry); set it to a more capable entitled model to escalate the recovery
        # (see ``wiring.bedrock_recovery_model_id``).
        self._recovery_model_id = recovery_model_id or model_id
        if client is not None:
            self._client = client
            return
        # Imported here, not at module load: the SDK is an optional heavyweight
        # dependency (it pulls boto3). A missing extra raises ImportError, which
        # the wiring (router.get_market_summary_provider) turns into a 503.
        from anthropic import AnthropicBedrock

        self._client = AnthropicBedrock(aws_region=region)

    def analyze(self, indexes: list[MarketIndexPerformance]) -> MarketSummary:
        prompt = _render_prompt(indexes)
        # One cost line per endpoint call: the retry loop below may make several model
        # calls, so their token usage is summed and logged once (in a finally, so a
        # mid-retry failure still records what was spent).
        costs = CostAccumulator()
        try:
            payload = self._invoke(prompt, costs)
            # The forced tool asks for a note per timeframe, but Bedrock does not
            # enforce array length, and the fast Haiku tier sometimes fills only the
            # summary and hands back an empty periods list. Re-issue a bounded number of
            # times to recover the notes (this read isn't result-cached, so a view that
            # still came back empty would otherwise show the periods without notes).
            # The retry escalates onto ``recovery_model_id`` when configured (a more
            # capable model fills what the fast tier dropped and yields a *complete*
            # read the use case caches). Best-effort: a failure on the recovery call is
            # swallowed so escalation can never sink a summary-present read.
            for _ in range(self._MAX_EMPTY_RETRIES):
                if not _missing_notes(payload):
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
                    _MARKET_KEY, "summary model returned no structured result"
                )
            return _to_entity(indexes, payload, self._model_id)
        finally:
            costs.log(label="market summary", model_id=self._model_id)

    def _invoke(
        self, prompt: str, costs: CostAccumulator, *, model: str | None = None
    ) -> dict | None:
        chosen = model or self._model_id
        try:
            message = self._client.messages.create(
                model=chosen,
                max_tokens=self._MAX_TOKENS,
                system=_SYSTEM_PROMPT,
                tools=[_SUMMARY_TOOL],
                tool_choice={"type": "tool", "name": "submit_market_summary"},
                messages=[{"role": "user", "content": prompt}],
            )
        except Exception as exc:  # SDK/botocore raise a family of errors; map them all
            raise StockDataUnavailable(
                _MARKET_KEY, f"summary model call failed: {exc}"
            ) from exc
        costs.add(message, chosen)
        return _tool_payload(message)


def _tool_payload(message) -> dict | None:
    for block in getattr(message, "content", None) or []:
        if (
            getattr(block, "type", None) == "tool_use"
            and getattr(block, "name", None) == "submit_market_summary"
        ):
            inputs = getattr(block, "input", None)
            if isinstance(inputs, dict):
                return inputs
    return None


def _to_entity(
    indexes: list[MarketIndexPerformance], payload: dict, model_id: str
) -> MarketSummary:
    try:
        summary = str(payload["summary"]).strip()
        tone = MarketTone(payload["tone"])
    except (KeyError, ValueError) as exc:
        raise StockDataUnavailable(
            _MARKET_KEY, f"summary model returned an unexpected result: {exc}"
        ) from exc
    notes = _notes_by_period(payload.get("periods"))
    periods = tuple(
        _period_highlight(period, notes.get(period.value, ""), indexes)
        for period in _PERIOD_WINDOW
    )
    return MarketSummary(
        summary=summary,
        tone=tone,
        periods=periods,
        model=model_id,
        generated_at=datetime.now(timezone.utc),
    )


def _notes_by_period(value) -> dict[str, str]:
    if not isinstance(value, list):
        return {}
    out: dict[str, str] = {}
    for item in value:
        if not isinstance(item, dict):
            continue
        period = str(item.get("period", "")).strip().casefold()
        note = str(item.get("note", "")).strip()
        if period and note:
            out[period] = note
    return out


def _missing_notes(payload: dict | None) -> bool:
    if payload is None:
        return False
    return not _notes_by_period(payload.get("periods"))


def _period_highlight(
    period: MarketPeriod, note: str, indexes: list[MarketIndexPerformance]
) -> MarketPeriodHighlight:
    returns = tuple(
        MarketIndexReturn(
            name=index.name,
            symbol=index.symbol,
            change_percent=_window_return(index, period),
        )
        for index in indexes
    )
    return MarketPeriodHighlight(period=period, note=note, indexes=returns)


def _window_return(index: MarketIndexPerformance, period: MarketPeriod) -> float | None:
    performance = index.performance
    if performance is None:
        return None
    return getattr(performance, _PERIOD_WINDOW[period])


def _render_prompt(indexes: list[MarketIndexPerformance]) -> str:
    lines = ["US market today (each index read through the ETF that tracks it):"]
    for index in indexes:
        parts = [f"{index.name} ({index.symbol})"]
        if index.change_percent is not None:
            parts.append(f"today {_num(index.change_percent)}%")
        perf = index.performance
        if perf is not None:
            for label, value in (
                ("past week", perf.one_week),
                ("past month", perf.one_month),
                ("past year", perf.one_year),
            ):
                if value is not None:
                    parts.append(f"{label} {_num(value)}%")
        lines.append("- " + ", ".join(parts))
    return "\n".join(lines)


def _num(value: object) -> str:
    if isinstance(value, bool):  # bool is an int subclass — keep it as-is
        return str(value)
    if isinstance(value, float):
        return f"{value:,.2f}"
    return str(value)
