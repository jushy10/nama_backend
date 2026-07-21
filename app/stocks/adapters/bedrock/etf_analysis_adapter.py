from datetime import datetime, timezone

from app.stocks.adapters.bedrock.cost import CostAccumulator
from app.stocks.analysis.entities import Confidence, InvestmentAnalysis, Recommendation
from app.stocks.etfs.entities import EtfDetail
from app.stocks.etfs.ports import EtfAnalysisProvider
from app.stocks.exceptions import StockDataUnavailable

# A single forced tool is how the model is pinned to structured output: Claude must call
# submit_analysis, so the response comes back as validated JSON arguments instead of prose. The
# schema mirrors the InvestmentAnalysis entity, minus the fields the adapter stamps itself (symbol,
# model, generated_at) — the same shape the stock analyser uses, since the *output* is asset-agnostic.
_ANALYSIS_TOOL = {
    "name": "submit_analysis",
    "description": (
        "Record a balanced strong-buy/buy/hold/sell/strong-sell read on the ETF in plain, "
        "everyday language, grounded only in the figures provided in the prompt."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "recommendation": {
                "type": "string",
                "enum": [r.value for r in Recommendation],
                "description": (
                    "The overall call on the five-point scale (strong buy / buy / hold / sell / "
                    "strong sell), weighing everything on balance."
                ),
            },
            "confidence": {
                "type": "string",
                "enum": [c.value for c in Confidence],
                "description": "How sure you are, given how much clear data there is.",
            },
            "thesis": {
                "type": "string",
                "description": (
                    "1-2 short sentences in plain, everyday language explaining the overall take "
                    "and the main reason for it — as if to a friend who doesn't follow the markets. "
                    "No jargon."
                ),
            },
            "strengths": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 2,
                "maxItems": 2,
                "description": (
                    "Exactly 2 short, plain-language reasons the fund looks good — each clear on "
                    "its own to someone with no finance background. Never return an empty list "
                    "(put them here, not only in the thesis)."
                ),
            },
            "risks": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 2,
                "maxItems": 2,
                "description": (
                    "Exactly 2 short, plain-language reasons to be cautious — each clear on its "
                    "own to someone with no finance background. Never return an empty list "
                    "(put them here, not only in the thesis)."
                ),
            },
        },
        "required": ["recommendation", "confidence", "thesis", "strengths", "risks"],
    },
}

_SYSTEM_PROMPT = (
    "You are a friendly investing assistant explaining one exchange-traded fund (ETF) to an "
    "everyday person with no finance background. An ETF is a basket of many investments bought as "
    "one, so what matters is what it holds, how spread out (diversified) or concentrated it is, "
    "what it costs to own each year, what it pays out, and how it has performed. You are given a "
    "snapshot of the fund: its price and recent move, its size, its yearly cost, its dividend, its "
    "recent and longer-term returns, its biggest holdings, and how its money is split across market "
    "sectors. From only those figures, give a clear, balanced read on a five-point scale — strong "
    "buy, buy, hold, sell, or strong sell — for a long-term investor, reserving the 'strong' calls "
    "for when the figures line up especially clearly one way.\n"
    "Weigh the yearly cost (the expense ratio) — even a small one quietly eats into returns over "
    "many years, so a cheaper fund has an edge over a pricier one holding much the same thing. Weigh "
    "how concentrated the fund is — if a handful of holdings or a single sector make up most of it, "
    "the fund rises and falls with them (more risk); a broad spread across many holdings and sectors "
    "is steadier. Weigh the long-term returns more than the day's move.\n"
    "Write in plain, warm, everyday language — short sentences, no jargon. When a figure matters, "
    "say what it means in a few plain words (e.g. 'it costs very little to own' or 'most of the "
    "money is in tech') rather than naming the ratio. Never assume the reader knows finance terms. "
    "Ground every statement ONLY in the figures provided — do not use outside knowledge, recent "
    "news, or prices you may recall, and never invent numbers. If the data is thin, say so plainly "
    "and lower your confidence. Be honest about both the good and the bad — always name at least "
    "two strengths and at least two risks, and never leave either list empty. This is general "
    "information, not personal financial advice. Respond by calling the submit_analysis tool."
)

# The recovery tool for the retry path: when the first pass packs everything into the thesis and
# hands back empty strengths/risks, the retry asks for *only* the two bullet lists — a far shorter
# generation than re-running the whole analysis. Output tokens dominate this endpoint's cost, so a
# bullets-only retry is the cheap way to recover, and the narrower ask lands more reliably.
_BULLETS_TOOL = {
    "name": "submit_bullets",
    "description": (
        "Record only the plain-language strengths and risks for the fund, grounded only in the "
        "figures provided. Exactly two of each; never leave either empty."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "strengths": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 2,
                "maxItems": 2,
                "description": (
                    "Exactly 2 short, plain-language reasons the fund looks good — each clear on "
                    "its own to someone with no finance background. Never empty."
                ),
            },
            "risks": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 2,
                "maxItems": 2,
                "description": (
                    "Exactly 2 short, plain-language reasons to be cautious — each clear on its "
                    "own to someone with no finance background. Never empty."
                ),
            },
        },
        "required": ["strengths", "risks"],
    },
}

_BULLETS_SYSTEM = (
    "You already gave the overall read on this fund. Now give ONLY the plain-language strengths "
    "and risks — exactly two of each, grounded only in the figures below, each clear to someone "
    "with no finance background. Never leave either list empty. Respond by calling the "
    "submit_bullets tool."
)

# Prepended to the same figures the first pass saw, so the recovered bullets stay grounded.
_BULLETS_INSTRUCTION = (
    "List the two strengths and two risks for this fund, grounded only in these figures:\n\n"
)


class BedrockEtfAnalysisProvider(EtfAnalysisProvider):
    # Full versioned inference-profile id — Haiku 4.5 has no bare alias on Bedrock,
    # so the short us.anthropic.claude-haiku-4-5 400s with "invalid model identifier".
    _DEFAULT_MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
    _DEFAULT_REGION = "us-east-1"
    # The output is short and plain by design (a few sentences + two brief bullet lists), so a tight
    # cap is ample — and fewer generated tokens is the main lever on this endpoint's latency.
    _MAX_TOKENS = 800
    # Bedrock does not enforce the tool schema's minItems, and the fast Haiku tier occasionally returns
    # empty strengths/risks anyway. Re-issue the forced call this many *extra* times to recover the
    # bullets. Kept at ONE: re-calling the same fast model rarely recovers what it just dropped, so
    # extra Haiku retries mostly just bill; the single retry is instead escalated onto
    # ``recovery_model_id`` (when configured). Only fires on the miss.
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
        # The model the single empty-bullets retry runs on. Defaults to the primary model
        # (a plain retry); set it to a more capable entitled model to escalate the recovery
        # (see ``wiring.bedrock_recovery_model_id``).
        self._recovery_model_id = recovery_model_id or model_id
        if client is not None:
            self._client = client
            return
        # Imported here, not at module load: the SDK is an optional heavyweight dependency (it pulls
        # boto3), and neither the app's other endpoints nor the offline tests need it present. A
        # missing extra raises ImportError, which the wiring turns into a 503.
        from anthropic import AnthropicBedrock

        self._client = AnthropicBedrock(aws_region=region)

    def analyze(self, detail: EtfDetail) -> InvestmentAnalysis:
        prompt = _render_prompt(detail)
        # One cost line per endpoint call: the retry loop below may make several model
        # calls, so their token usage is summed and logged once (in a finally, so a
        # mid-retry failure still records what was spent).
        costs = CostAccumulator()
        try:
            payload = self._invoke(prompt, detail.ticker, costs)
            # The forced tool asks for both bullet lists, but Bedrock does not enforce
            # array length, and the fast Haiku tier sometimes packs everything into the
            # thesis and hands back empty strengths/risks. Re-issue a bounded number of
            # times to recover them; the use case won't cache an incomplete one, so an
            # empty result is never frozen for the TTL — it regenerates next view. Each
            # retry asks for *only* the missing bullets (not the whole analysis again),
            # so it regenerates a fraction of the tokens — output is this endpoint's
            # dominant cost. A recovery that doesn't land leaves the payload unchanged
            # and simply consumes a bounded retry, so a truly stuck read still exits.
            for _ in range(self._MAX_EMPTY_RETRIES):
                if not _missing_bullets(payload):
                    break
                recovered = self._recover_bullets(prompt, detail.ticker, costs)
                if recovered is not None:
                    payload = _merge_bullets(payload, recovered)
            if payload is None:
                raise StockDataUnavailable(
                    detail.ticker, "analysis model returned no structured result"
                )
            return _to_entity(detail.ticker, payload, self._model_id)
        finally:
            costs.log(
                label="etf analysis", model_id=self._model_id, key=detail.ticker
            )

    def _invoke(
        self,
        prompt: str,
        key: str,
        costs: CostAccumulator,
        *,
        tool: dict = _ANALYSIS_TOOL,
        tool_name: str = "submit_analysis",
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
        except Exception as exc:  # SDK/botocore raise a family of errors; map them all
            raise StockDataUnavailable(
                key, f"analysis model call failed: {exc}"
            ) from exc
        costs.add(message, chosen)
        return _tool_payload(message, tool_name)

    def _recover_bullets(
        self, prompt: str, key: str, costs: CostAccumulator
    ) -> dict | None:
        try:
            return self._invoke(
                _BULLETS_INSTRUCTION + prompt,
                key,
                costs,
                tool=_BULLETS_TOOL,
                tool_name="submit_bullets",
                system=_BULLETS_SYSTEM,
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


def _merge_bullets(payload: dict, recovered: dict) -> dict:
    merged = dict(payload)
    for field in ("strengths", "risks"):
        if not _string_tuple(merged.get(field)) and _string_tuple(recovered.get(field)):
            merged[field] = recovered[field]
    return merged


def _to_entity(symbol: str, payload: dict, model_id: str) -> InvestmentAnalysis:
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
    if not isinstance(value, list):
        return ()
    return tuple(text for item in value if (text := str(item).strip()))


def _missing_bullets(payload: dict | None) -> bool:
    if payload is None:
        return False
    return not _string_tuple(payload.get("strengths")) or not _string_tuple(
        payload.get("risks")
    )


def _render_prompt(detail: EtfDetail) -> str:
    quote = detail.quote
    profile = detail.profile
    perf = detail.performance
    fields: list[tuple[str, object]] = [
        ("Name", detail.name),
        ("Exchange", detail.exchange),
        ("Category (fund type)", detail.category),
        ("Fund family", profile.fund_family),
        ("Price", quote.price),
        ("Day change %", quote.change_percent),
        ("Previous close", quote.previous_close),
        ("Net assets (AUM, USD)", detail.net_assets),
        ("Expense ratio % (yearly cost)", detail.expense_ratio),
        ("NAV per share", profile.nav),
        ("Dividend yield %", profile.dividend_yield),
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
    # The long-horizon returns ride the profile (a live Yahoo read the use case overlays for the
    # performance snapshot); they're the figures that matter most for a long-term fund read.
    fields += [
        ("Return 3y % (annualized)", profile.three_year_return),
        ("Return 5y % (annualized)", profile.five_year_return),
    ]
    lines = [f"ETF: {detail.ticker}"]
    lines += [
        f"- {label}: {_num(value)}" for label, value in fields if value is not None
    ]
    for block in (
        _render_holdings(profile.top_holdings),
        _render_sectors(profile.sector_weightings),
        _render_description(profile.description),
    ):
        if block:
            lines.append("")
            lines.append(block)
    return "\n".join(lines)


def _render_holdings(holdings) -> str:
    if not holdings:
        return ""
    lines = ["Top holdings (largest first, as a percent of the fund):"]
    for h in holdings:
        name = h.name or h.ticker
        if not name:
            continue
        if h.weight is not None:
            lines.append(f"- {name}: {_num(h.weight)}%")
        else:
            lines.append(f"- {name}")
    return "\n".join(lines)


def _render_sectors(weights) -> str:
    if not weights:
        return ""
    lines = ["Sector weightings (percent of the fund, largest first):"]
    for s in weights:
        lines.append(f"- {s.sector}: {_num(s.weight)}%")
    return "\n".join(lines)


def _render_description(description: object) -> str:
    if not isinstance(description, str) or not description.strip():
        return ""
    return f"Fund description:\n{description.strip()}"


def _num(value: object) -> str:
    if isinstance(value, bool):  # bool is an int subclass — keep it as-is
        return str(value)
    if isinstance(value, float):
        return f"{value:,.2f}"
    return str(value)
