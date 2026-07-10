"""Interface Adapter: AI analysis of an ETF via Claude on Amazon Bedrock.

The ETF sibling of ``analysis_adapter.py`` (the stock analyser) — the only
module that knows Bedrock (and the Anthropic SDK) exists for the fund read. It takes the
``EtfDetail`` the use case already assembled — the live quote, the fund's size (AUM), yearly cost
(expense ratio), yield, NAV, its trailing and long-term returns, its top holdings, and its sector
split — renders it into a compact prompt, and asks Claude for a balanced buy/hold/sell read written
in plain, everyday language a non-expert can follow. Swap models or vendors and only this file
changes.

It duplicates the stock analyser's scaffolding (the forced-tool structured output, the lazy SDK
import, the ``StockDataUnavailable`` error mapping) rather than sharing it: the hard rule is that an
adapter never imports another adapter, and the two prompts genuinely differ — a fund is a basket, so
its read weighs cost drag, diversification, and concentration where the stock read weighs earnings
and valuation. The shared, asset-agnostic pieces are the *entities* (``InvestmentAnalysis`` /
``Recommendation`` / ``Confidence``), which both adapters map onto.

Two deliberate choices, the same as the stock analyser:

* **Auth is the runtime's job, not ours.** Bedrock authenticates through the process's AWS
  credentials (in production, the ECS task role), so there is no API key to read or pass.
* **Structured output via a forced tool call.** Claude must call ``submit_analysis``, so it returns
  the analysis as validated JSON arguments that map straight onto the ``InvestmentAnalysis`` entity —
  no brittle prose parsing.

The Anthropic SDK is imported lazily inside ``__init__`` so the app (and the offline test suite,
which injects a stub client) imports cleanly without the ``bedrock`` extra installed. Any Bedrock/SDK
failure is translated to ``StockDataUnavailable`` — the one error this port documents.

Docs: https://docs.anthropic.com/en/api/claude-on-amazon-bedrock
"""

from datetime import datetime, timezone

from app.stocks.adapters.bedrock.cost import CostAccumulator
from app.stocks.entities import Confidence, InvestmentAnalysis, Recommendation
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
        "Record a balanced buy/hold/sell read on the ETF in plain, everyday language, grounded "
        "only in the figures provided in the prompt."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "recommendation": {
                "type": "string",
                "enum": [r.value for r in Recommendation],
                "description": "The overall call, weighing everything on balance.",
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
    "sectors. From only those figures, give a clear, balanced read on whether it currently looks "
    "like a buy, hold, or sell for a long-term investor.\n"
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
    """Generates an ``InvestmentAnalysis`` of an ETF with Claude on Amazon Bedrock.

    Defaults to the fast Haiku tier (``model_id``) since the analysis output is short and plain —
    speed matters more than extra reasoning here. ``model_id`` and ``region`` are deploy-time config
    (the model id may be a cross-region inference profile), so a deploy can swap in a larger model
    via env without a code change. ``client`` is an injection seam: pass a ready-made client (e.g. a
    test stub) to bypass the Anthropic SDK entirely; otherwise the Bedrock client is built lazily and
    authenticates through the process's AWS credentials. Mirrors the stock ``BedrockAnalysisProvider``
    (same defaults, same env), so one deploy config drives both analysers.
    """

    # Full versioned inference-profile id — Haiku 4.5 has no bare alias on Bedrock,
    # so the short us.anthropic.claude-haiku-4-5 400s with "invalid model identifier".
    _DEFAULT_MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
    _DEFAULT_REGION = "us-east-1"
    # The output is short and plain by design (a few sentences + two brief bullet lists), so a tight
    # cap is ample — and fewer generated tokens is the main lever on this endpoint's latency.
    _MAX_TOKENS = 800
    # Bedrock does not enforce the tool schema's minItems, and the fast Haiku tier occasionally returns
    # empty strengths/risks anyway. Re-issue the forced call up to this many *extra* times to recover
    # the bullets; paired with the use case refusing to cache an incomplete read, an empty result is
    # effectively never served (and never frozen for the TTL). Only fires on the miss.
    _MAX_EMPTY_RETRIES = 4

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
    ) -> dict | None:
        """One forced-tool call, returning the tool's arguments (or ``None`` if the
        model somehow didn't call the forced tool). Defaults to the full analysis
        tool; the retry path passes the lighter ``submit_bullets`` tool. Any
        SDK/botocore failure is mapped to this port's documented
        ``StockDataUnavailable``. The call's token usage is folded into ``costs`` for
        the caller's single per-endpoint cost line."""
        try:
            message = self._client.messages.create(
                model=self._model_id,
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
        costs.add(message)
        return _tool_payload(message, tool_name)

    def _recover_bullets(
        self, prompt: str, key: str, costs: CostAccumulator
    ) -> dict | None:
        """One targeted retry that regenerates *only* the strengths/risks bullets,
        grounded in the same figures the first pass saw — far fewer output tokens than
        re-running the whole analysis. Returns the ``submit_bullets`` arguments, or
        ``None`` when the model didn't call the tool (the caller then leaves the payload
        unchanged and consumes a bounded retry)."""
        return self._invoke(
            _BULLETS_INSTRUCTION + prompt,
            key,
            costs,
            tool=_BULLETS_TOOL,
            tool_name="submit_bullets",
            system=_BULLETS_SYSTEM,
        )


def _tool_payload(message, name: str) -> dict | None:
    """Pull the named forced tool's arguments out of the model's tool call, if any."""
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
    """Fill only the *empty* bullet lists from a targeted recovery call, leaving any
    list the first pass already produced untouched — so a retry that recovers one side
    (say, risks) never overwrites the good other side."""
    merged = dict(payload)
    for field in ("strengths", "risks"):
        if not _string_tuple(merged.get(field)) and _string_tuple(recovered.get(field)):
            merged[field] = recovered[field]
    return merged


def _to_entity(symbol: str, payload: dict, model_id: str) -> InvestmentAnalysis:
    """Map the validated tool arguments onto the domain entity.

    The forced-tool schema constrains the shape, but a defensive guard keeps an off-schema result
    (e.g. an unknown enum value) from leaking out as something other than this port's documented
    ``StockDataUnavailable``.
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


def _missing_bullets(payload: dict | None) -> bool:
    """True when a returned tool result is present but missing either bullet list —
    the signal to retry. A ``None`` payload (the model didn't call the tool at all)
    is left for the caller to surface as ``StockDataUnavailable``, not retried, so
    the existing no-structured-result path is unchanged."""
    if payload is None:
        return False
    return not _string_tuple(payload.get("strengths")) or not _string_tuple(
        payload.get("risks")
    )


def _render_prompt(detail: EtfDetail) -> str:
    """Render the assembled ETF detail into a compact, labelled block for the model.

    Only fields that are present are included, so the model is never handed a ``None`` to reason
    about — thin coverage simply yields a shorter prompt (and the model is told to lower its
    confidence). The sections mirror what the detail card exposes: the identity + size/cost/yield
    facts, the trailing and long-term returns, then the top holdings, the sector split, and the
    fund's own description.
    """
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
    """Render the fund's top holdings as a short labelled block (or '' if none) — the concentration
    signal: how much of the fund sits in its biggest positions."""
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
    """Render the fund's sector split as a short labelled block (or '' if none) — how the money is
    spread across the market, the other half of the diversification picture."""
    if not weights:
        return ""
    lines = ["Sector weightings (percent of the fund, largest first):"]
    for s in weights:
        lines.append(f"- {s.sector}: {_num(s.weight)}%")
    return "\n".join(lines)


def _render_description(description: object) -> str:
    """Render the fund's own description as a short block (or '' if none) — plain-English context on
    what the fund is trying to do, in the issuer's words."""
    if not isinstance(description, str) or not description.strip():
        return ""
    return f"Fund description:\n{description.strip()}"


def _num(value: object) -> str:
    """Format a numeric field readably; pass non-numbers through unchanged."""
    if isinstance(value, bool):  # bool is an int subclass — keep it as-is
        return str(value)
    if isinstance(value, float):
        return f"{value:,.2f}"
    return str(value)
