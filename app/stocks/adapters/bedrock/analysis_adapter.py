"""Interface Adapter: AI investment analysis via Claude on Amazon Bedrock.

The only module that knows Bedrock (and the Anthropic SDK) exists. It takes the
data the use case already gathered — the price snapshot, trailing performance,
the trailing *and* forward valuation/health/growth metrics, the recent quarterly
and annual earnings, the analyst recommendation trends, and the stock's industry
P/E benchmark (how its valuation sits against its peers) — renders it into a
compact prompt, and asks Claude for a balanced buy/hold/sell read written in
plain, everyday language a non-expert can follow. Swap models or vendors and only
this file changes.

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
(e.g. ``us.anthropic.claude-haiku-4-5-20251001-v1:0``). Both are deploy-time config, surfaced
through the constructor / env (see ``router.get_analysis_provider``).

Docs: https://docs.anthropic.com/en/api/claude-on-amazon-bedrock
"""

from datetime import datetime, timezone

from app.stocks.entities import (
    Confidence,
    InvestmentAnalysis,
    Recommendation,
    Stock,
)
from app.stocks.adapters.bedrock.cost import CostAccumulator
from app.stocks.earnings.annual.entities import AnnualEarningsTimeline
from app.stocks.earnings.quarterly.entities import QuarterlyEarningsTimeline
from app.stocks.exceptions import StockDataUnavailable
from app.stocks.ports import InvestmentAnalysisProvider
from app.stocks.recommendations.entities import AnalystRecommendations
from app.stocks.universe.entities import IndustryValuation

# A single forced tool is how the model is pinned to structured output: Claude
# must call submit_analysis, so the response comes back as validated JSON
# arguments instead of prose. The schema mirrors the InvestmentAnalysis entity,
# minus the fields the adapter stamps itself (symbol, model, generated_at).
_ANALYSIS_TOOL = {
    "name": "submit_analysis",
    "description": (
        "Record a balanced buy/hold/sell read on the stock in plain, everyday "
        "language, grounded only in the figures provided in the prompt."
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
                    "1-2 short sentences in plain, everyday language explaining the "
                    "overall take and the main reason for it — as if to a friend who "
                    "doesn't follow the markets. No jargon."
                ),
            },
            "strengths": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 2,
                "maxItems": 2,
                "description": (
                    "Exactly 2 short, plain-language reasons the stock looks good — "
                    "each clear on its own to someone with no finance background. "
                    "Never return an empty list (put them here, not only in the thesis)."
                ),
            },
            "risks": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 2,
                "maxItems": 2,
                "description": (
                    "Exactly 2 short, plain-language reasons to be cautious — each "
                    "clear on its own to someone with no finance background. "
                    "Never return an empty list (put them here, not only in the thesis)."
                ),
            },
        },
        "required": ["recommendation", "confidence", "thesis", "strengths", "risks"],
    },
}

_SYSTEM_PROMPT = (
    "You are a friendly investing assistant explaining one stock to an everyday "
    "person with no finance background. You are given a snapshot of the stock's "
    "price, its valuation and profitability figures, its recent quarterly and "
    "annual earnings, what Wall Street analysts recommend, and how its valuation "
    "compares with other companies in the same industry. From only those figures, "
    "give a clear, balanced read on whether it currently looks like a buy, hold, "
    "or sell.\n"
    "When an industry benchmark is provided, weigh the stock's own price-to-"
    "earnings against it — a much higher figure than its peers means it's "
    "priced richly (expensive) for its industry, a much lower one means cheaply "
    "— and explain that comparison in plain words.\n"
    "Write in plain, warm, everyday language — short sentences, no jargon. When a "
    "figure matters, say what it means in a few plain words (e.g. 'its price is "
    "high compared with its earnings') rather than naming the ratio. Never assume "
    "the reader knows finance terms. Ground every statement ONLY in the figures "
    "provided — do not use outside knowledge, recent news, or prices you may "
    "recall, and never invent numbers. If the data is thin, say so plainly and "
    "lower your confidence. Be honest about both the good and the bad — always "
    "name at least two strengths and at least two risks, and never leave either "
    "list empty. This is general information, not personal financial advice. "
    "Respond by calling the submit_analysis tool."
)

# The recovery tool for the retry path: when the first pass packs everything into the
# thesis and hands back empty strengths/risks, the retry asks for *only* the two bullet
# lists — a far shorter generation than re-running the whole analysis (recommendation +
# confidence + thesis + bullets). Output tokens dominate this endpoint's cost, so a
# bullets-only retry is the cheap way to recover, and the narrower ask lands more reliably.
_BULLETS_TOOL = {
    "name": "submit_bullets",
    "description": (
        "Record only the plain-language strengths and risks for the stock, grounded "
        "only in the figures provided. Exactly two of each; never leave either empty."
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
                    "Exactly 2 short, plain-language reasons the stock looks good — each "
                    "clear on its own to someone with no finance background. Never empty."
                ),
            },
            "risks": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 2,
                "maxItems": 2,
                "description": (
                    "Exactly 2 short, plain-language reasons to be cautious — each clear "
                    "on its own to someone with no finance background. Never empty."
                ),
            },
        },
        "required": ["strengths", "risks"],
    },
}

_BULLETS_SYSTEM = (
    "You already gave the overall read on this stock. Now give ONLY the plain-language "
    "strengths and risks — exactly two of each, grounded only in the figures below, each "
    "clear to someone with no finance background. Never leave either list empty. Respond "
    "by calling the submit_bullets tool."
)

# Prepended to the same figures the first pass saw, so the recovered bullets stay grounded.
_BULLETS_INSTRUCTION = (
    "List the two strengths and two risks for this stock, grounded only in these figures:\n\n"
)


class BedrockAnalysisProvider(InvestmentAnalysisProvider):
    """Generates an ``InvestmentAnalysis`` with Claude on Amazon Bedrock.

    Defaults to the fast Haiku tier (``model_id``) since the analysis output is
    short and plain — speed matters more than extra reasoning here. ``model_id``
    and ``region`` are deploy-time config (the model id may be a cross-region
    inference profile), so a deploy can swap in a larger model via env without a
    code change. ``client`` is an injection seam: pass a ready-made client (e.g. a
    test stub) to bypass the Anthropic SDK entirely; otherwise the Bedrock client
    is built lazily and authenticates through the process's AWS credentials.
    """

    # Defaults to the fast Haiku tier: this endpoint's output is short and plain by
    # design, so the extra reasoning of a larger model buys little here — and Haiku
    # generates markedly faster, the whole point of this endpoint. The id is a
    # cross-region inference profile (the form Bedrock wants for current Claude
    # models) and is env-overridable, so a deploy can point BEDROCK_ANALYSIS_MODEL_ID
    # at whatever model the account is entitled to (prod has run Sonnet). Haiku 4.5
    # has no bare alias on Bedrock, so this is the full versioned id (the short
    # us.anthropic.claude-haiku-4-5 400s with "invalid model identifier").
    _DEFAULT_MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
    _DEFAULT_REGION = "us-east-1"
    # The output is short and plain by design (a few sentences + two brief bullet
    # lists), so a tight cap is ample — and fewer generated tokens is the main
    # lever on this endpoint's latency, since output generation dominates the
    # model call. Kept above the worst case so a full read is never truncated.
    _MAX_TOKENS = 800
    # Bedrock does not enforce the tool schema's minItems, and the fast Haiku tier
    # occasionally returns empty strengths/risks anyway. Re-issue the forced call up
    # to this many *extra* times to recover the bullets; paired with the use case
    # refusing to cache an incomplete read, an empty result is effectively never
    # served (and never frozen for the TTL). Only fires on the miss — zero cost when
    # the first call is already complete.
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
        # Imported here, not at module load: the SDK is an optional heavyweight
        # dependency (it pulls boto3), and neither the app's other endpoints nor
        # the offline tests need it present. A missing extra raises ImportError,
        # which the wiring (router.get_analysis_provider) turns into a 503.
        from anthropic import AnthropicBedrock

        self._client = AnthropicBedrock(aws_region=region)

    def analyze(
        self,
        stock: Stock,
        quarterly: QuarterlyEarningsTimeline | None = None,
        annual: AnnualEarningsTimeline | None = None,
        recommendations: AnalystRecommendations | None = None,
        industry_valuation: IndustryValuation | None = None,
    ) -> InvestmentAnalysis:
        prompt = _render_prompt(
            stock, quarterly, annual, recommendations, industry_valuation
        )
        # One cost line per endpoint call: the retry loop below may make several model
        # calls, so their token usage is summed and logged once (in a finally, so a
        # mid-retry failure still records what was spent).
        costs = CostAccumulator()
        try:
            payload = self._invoke(prompt, stock.symbol, costs)
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
                recovered = self._recover_bullets(prompt, stock.symbol, costs)
                if recovered is not None:
                    payload = _merge_bullets(payload, recovered)
            if payload is None:
                raise StockDataUnavailable(
                    stock.symbol, "analysis model returned no structured result"
                )
            return _to_entity(stock.symbol, payload, self._model_id)
        finally:
            costs.log(
                label="stock analysis", model_id=self._model_id, key=stock.symbol
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


def _render_prompt(
    stock: Stock,
    quarterly: QuarterlyEarningsTimeline | None,
    annual: AnnualEarningsTimeline | None = None,
    recommendations: AnalystRecommendations | None = None,
    industry_valuation: IndustryValuation | None = None,
) -> str:
    """Render the gathered data into a compact, labelled block for the model.

    Only fields that are present are included, so the model is never handed a
    ``None`` to reason about — thin coverage simply yields a shorter prompt. The
    sections mirror what the app's own endpoints expose: the enriched snapshot
    (price, dividend, performance, and the trailing *and* forward
    valuation/health/growth metrics — the ticker card's figures) followed, when
    available, by the quarterly and annual earnings timelines, the analyst
    recommendation trends, and the industry P/E benchmark.
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
            ("FCF/share (trailing)", metrics.fcf_per_share),
            ("EPS growth YoY %", metrics.eps_growth_yoy),
            ("Revenue growth YoY %", metrics.revenue_growth_yoy),
            ("Gross margin %", metrics.gross_margin),
            ("Operating margin %", metrics.operating_margin),
            ("Net margin %", metrics.net_margin),
            ("ROE %", metrics.roe),
            ("Current ratio", metrics.current_ratio),
            ("Debt/equity", metrics.debt_to_equity),
            ("Beta", metrics.beta),
            ("52-week high", metrics.week_52_high),
            ("52-week low", metrics.week_52_low),
        ]
    # Forward-looking consensus: what analysts expect next, the same figures the
    # ticker card's forward valuation is built on. Trailing metrics say what the
    # business has done; these say what it's expected to do.
    fields += [
        ("Forward P/E (consensus)", stock.forward_pe),
        ("Forward P/S (consensus)", stock.forward_ps),
    ]
    growth = stock.growth
    if growth is not None:
        fields += [
            ("Expected revenue growth next year %", growth.forward_revenue_growth),
            ("Expected EPS growth next year %", growth.forward_eps_growth),
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
    for block in (
        _render_quarterly(quarterly),
        _render_annual(annual),
        _render_recommendations(recommendations),
        _render_industry_valuation(industry_valuation),
    ):
        if block:
            lines.append("")
            lines.append(block)
    return "\n".join(lines)


def _render_industry_valuation(valuation: IndustryValuation | None) -> str:
    """Render the industry P/E benchmark as a short labelled block (or '' if none)
    — the peer-valuation anchor that turns the stock's own trailing P/E from an
    absolute number into a relative one ("28 against an industry that trades near
    21"). The use case only passes a benchmark with at least one valued peer, so a
    present block always carries a median."""
    if valuation is None or valuation.count == 0 or valuation.median_pe is None:
        return ""
    # ``cohort`` names the size slice the peers were drawn from: "industry" for the whole
    # (mid-cap-and-up) industry, or a tier label ("mega", "large/mega") when the benchmark was
    # scoped to the stock's own cap class — so the model reads a mega-cap median as a like-for-
    # like comparison, not an industry-wide one.
    peer_group = (
        "in the same industry"
        if valuation.cohort == "industry"
        else f"of the same size ({valuation.cohort}-cap) in the industry"
    )
    lines = [
        "Industry valuation benchmark "
        f"(trailing P/E across {valuation.count} peer(s) {peer_group}):",
        f"- Industry: {valuation.industry}",
        f"- Peer group: {valuation.cohort}",
        f"- Median P/E: {_num(valuation.median_pe)}",
    ]
    if valuation.p25_pe is not None and valuation.p75_pe is not None:
        lines.append(
            f"- Typical range (25th-75th percentile): "
            f"{_num(valuation.p25_pe)} to {_num(valuation.p75_pe)}"
        )
    return "\n".join(lines)


def _render_recommendations(recommendations: AnalystRecommendations | None) -> str:
    """Render the analyst recommendation trend as a short labelled block (or '' if
    none) — the sell-side's own buy/hold/sell consensus, for the model to weigh
    against its read (agreeing or deliberately differing)."""
    if recommendations is None or recommendations.is_empty:
        return ""
    latest = recommendations.latest
    if latest is None or latest.total == 0:
        return ""
    lines = ["Analyst recommendations (the sell-side's own view):"]
    if latest.consensus is not None:
        lines.append(
            f"- Consensus: {latest.consensus} "
            f"(average {latest.score} on a 1=Strong Buy to 5=Strong Sell scale, "
            f"from {latest.total} analysts)"
        )
    lines.append(
        "- Breakdown: "
        f"{latest.strong_buy} strong buy, {latest.buy} buy, {latest.hold} hold, "
        f"{latest.sell} sell, {latest.strong_sell} strong sell"
    )
    if recommendations.direction is not None:
        lines.append(f"- Trend vs last month: {recommendations.direction}")
    return "\n".join(lines)


def _render_quarterly(quarterly: QuarterlyEarningsTimeline | None) -> str:
    """Render the reported half of the quarterly timeline as a short labelled
    block (or '' if none) — newest quarter first, the order the read scans in."""
    if quarterly is None or not quarterly.past:
        return ""
    reported = list(reversed(quarterly.past))  # the timeline is oldest-first
    scoreable = [q for q in reported if q.beat is not None]
    beats = sum(1 for q in scoreable if q.beat)
    lines = ["Recent quarterly earnings (newest quarter first):"]
    if scoreable:
        rate = round(beats / len(scoreable) * 100, 1)
        lines.append(
            f"- Beat rate: {rate}% "
            f"({beats}/{len(scoreable)} quarters met or beat estimate)"
        )
    for q in reported:
        parts: list[str] = []
        if q.period_end is not None:
            parts.append(str(q.period_end))
        else:
            parts.append(f"FY{q.fiscal_year} Q{q.fiscal_quarter}")
        if q.eps_actual is not None:
            parts.append(f"EPS actual {q.eps_actual}")
        if q.eps_estimate is not None:
            parts.append(f"est {q.eps_estimate}")
        if q.eps_surprise_percent is not None:
            parts.append(f"surprise {q.eps_surprise_percent}%")
        if q.revenue_actual is not None:
            parts.append(f"revenue {q.revenue_actual:,.0f}")
        if parts:
            lines.append("- " + ", ".join(parts))
    return "\n".join(lines)


def _render_annual(annual: AnnualEarningsTimeline | None) -> str:
    """Render the annual timeline as a short labelled block (or '' if none) —
    reported fiscal years newest first, then the forward (estimated) years.

    Reported EPS is shown on the analyst-consensus (adjusted) basis when it's
    available (``eps_actual_consensus``), falling back to GAAP diluted, so a
    reported year and a forward estimate sit on the same basis."""
    if annual is None or annual.is_empty:
        return ""
    lines = ["Annual earnings (fiscal years):"]
    for y in reversed(annual.past):  # oldest-first -> newest-first
        parts = [f"FY{y.fiscal_year} reported"]
        eps = y.eps_actual_consensus if y.eps_actual_consensus is not None else y.eps_actual
        if eps is not None:
            parts.append(f"EPS {eps}")
        if y.revenue_actual is not None:
            parts.append(f"revenue {y.revenue_actual:,.0f}")
        if y.net_income is not None:
            parts.append(f"net income {y.net_income:,.0f}")
        lines.append("- " + ", ".join(parts))
    for y in annual.future:  # soonest-first
        parts = [f"FY{y.fiscal_year} estimated"]
        if y.eps_estimate is not None:
            parts.append(f"EPS est {y.eps_estimate}")
        if y.revenue_estimate is not None:
            parts.append(f"revenue est {y.revenue_estimate:,.0f}")
        lines.append("- " + ", ".join(parts))
    return "\n".join(lines)


def _num(value: object) -> str:
    """Format a numeric field readably; pass non-numbers through unchanged."""
    if isinstance(value, bool):  # bool is an int subclass — keep it as-is
        return str(value)
    if isinstance(value, float):
        return f"{value:,.2f}"
    return str(value)
