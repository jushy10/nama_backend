"""Interface Adapter: AI investment scorecard via Claude on Amazon Bedrock.

The only module that knows Bedrock (and the Anthropic SDK) exists. It takes the
data the use case already gathered — the price snapshot, trailing performance,
the trailing *and* forward valuation/health/growth metrics, the recent quarterly
and annual earnings, the analyst recommendation trends, and the stock's industry
P/E benchmark (how its valuation sits against its peers) — renders it into a
compact prompt, and asks Claude for a five-point strong-buy…strong-sell read
**broken into graded sections** (profitability, cash generation, growth,
valuation, financial health, earnings, analyst view), written in plain, everyday
language a non-expert can follow. Swap models or vendors and only this file changes.

The sections are a **single registry** (``_SECTIONS``) — one entry per facet, with
its key, title, a prompt hint, and the builder that turns gathered data into its
numeric chips. The forced-tool schema, the recovery tool, the prompt's section list,
and the entity assembly all derive from that one list, so adding a category is one
entry plus one metric builder, not an edit in four places.

Two deliberate choices keep it robust and on-pattern:

* **Auth is the runtime's job, not ours.** Bedrock authenticates through the
  process's AWS credentials (in production, the ECS task role), so — unlike
  every other vendor in this slice — there is no API key to read or pass. The
  IAM policy on the task role is what grants access.
* **Structured output via a forced tool call.** Rather than parse free text we
  hand Claude one ``submit_scorecard`` tool and require it, so the model returns
  the read as validated JSON arguments that map straight onto the
  ``StockScorecard`` entity — no brittle prose parsing. The model authors only the
  *words* (each section's stance / label / summary + the overall verdict); every
  supporting **number** is attached here from the figures already gathered, so a
  chip can never carry a hallucinated value.

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

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone

from app.stocks.analysis.entities import (
    Confidence,
    Recommendation,
    ScorecardSection,
    SectionMetric,
    SectionStance,
    StockScorecard,
)
from app.stocks.analysis.ports import StockScorecardProvider
from app.stocks.adapters.bedrock.cost import CostAccumulator
from app.stocks.earnings.annual.entities import AnnualEarningsTimeline
from app.stocks.earnings.quarterly.entities import QuarterlyEarningsTimeline
from app.stocks.entities import Stock
from app.stocks.exceptions import StockDataUnavailable
from app.stocks.recommendations.entities import AnalystRecommendations
from app.stocks.universe.entities import IndustryValuation


@dataclass(frozen=True)
class _Facts:
    """Everything the use case gathered, bundled so each section's metric builder
    takes one argument. The model never sees this — it's what the *service* turns
    into the numeric chips (so a chip can never carry a hallucinated number)."""

    stock: Stock
    quarterly: QuarterlyEarningsTimeline | None
    recommendations: AnalystRecommendations | None
    industry_valuation: IndustryValuation | None


# --- per-section chip builders (each takes the gathered _Facts) --------------------
# Every displayed number comes from here, never the model. A chip is dropped when its
# figure is absent, so thin coverage just yields fewer chips.


def _profitability_metrics(f: _Facts) -> tuple[SectionMetric, ...]:
    """How much profit the company keeps — margins and return on equity."""
    m = f.stock.metrics
    if m is None:
        return ()
    return _metrics(
        ("Net margin", m.net_margin, "%"),
        ("Operating margin", m.operating_margin, "%"),
        ("Gross margin", m.gross_margin, "%"),
        ("Return on equity", m.roe, "%"),
    )


def _cash_generation_metrics(f: _Facts) -> tuple[SectionMetric, ...]:
    """How much real cash the business throws off — free cash flow per share and yield."""
    m = f.stock.metrics
    if m is None:
        return ()
    out = list(_metrics(("FCF / share", m.fcf_per_share, "")))
    fcf_yield = _fcf_yield(m.fcf_per_share, f.stock.price)
    if fcf_yield is not None:
        out.append(SectionMetric("FCF yield", f"{_num(fcf_yield)}%"))
    return tuple(out)


def _growth_metrics(f: _Facts) -> tuple[SectionMetric, ...]:
    """Trailing revenue/EPS growth plus what analysts expect next year."""
    m = f.stock.metrics
    growth = f.stock.growth
    return _metrics(
        ("Revenue growth YoY", m.revenue_growth_yoy if m else None, "%"),
        ("EPS growth YoY", m.eps_growth_yoy if m else None, "%"),
        (
            "Est. revenue growth (next yr)",
            growth.forward_revenue_growth if growth else None,
            "%",
        ),
        ("Est. EPS growth (next yr)", growth.forward_eps_growth if growth else None, "%"),
    )


def _valuation_metrics(f: _Facts) -> tuple[SectionMetric, ...]:
    """Trailing + forward multiples, plus the industry median for the peer anchor."""
    m = f.stock.metrics
    iv = f.industry_valuation
    rows = [
        ("P/E (trailing)", m.pe if m else None, ""),
        ("Forward P/E", f.stock.forward_pe, ""),
        ("PEG", m.peg if m else None, ""),
        ("P/B", m.pb if m else None, ""),
        ("P/S", m.ps if m else None, ""),
    ]
    if iv is not None and iv.median_pe is not None:
        rows.append(("Industry median P/E", iv.median_pe, ""))
    return _metrics(*rows)


def _financial_health_metrics(f: _Facts) -> tuple[SectionMetric, ...]:
    """Balance-sheet strength — liquidity and leverage."""
    m = f.stock.metrics
    if m is None:
        return ()
    return _metrics(
        ("Current ratio", m.current_ratio, ""),
        ("Debt / equity", m.debt_to_equity, ""),
    )


def _earnings_metrics(f: _Facts) -> tuple[SectionMetric, ...]:
    """The recent beat track record and the latest surprise."""
    out: list[SectionMetric] = []
    quarterly = f.quarterly
    if quarterly is not None and quarterly.past:
        reported = list(reversed(quarterly.past))  # the timeline is oldest-first
        scoreable = [q for q in reported if q.beat is not None]
        if scoreable:
            beats = sum(1 for q in scoreable if q.beat)
            out.append(
                SectionMetric("Beat rate", f"{beats}/{len(scoreable)} quarters")
            )
        newest = reported[0]
        if newest.eps_surprise_percent is not None:
            out.append(
                SectionMetric(
                    "Latest surprise", f"{_num(newest.eps_surprise_percent)}%"
                )
            )
    return tuple(out)


def _analyst_metrics(f: _Facts) -> tuple[SectionMetric, ...]:
    """The current consensus, its analyst count, and the average 1-5 score."""
    recommendations = f.recommendations
    if recommendations is None or recommendations.is_empty:
        return ()
    latest = recommendations.latest
    if latest is None or latest.total == 0:
        return ()
    out: list[SectionMetric] = []
    if latest.consensus is not None:
        out.append(SectionMetric("Consensus", str(latest.consensus)))
    out.append(SectionMetric("Analysts", str(latest.total)))
    if latest.score is not None:
        out.append(SectionMetric("Avg score (1-5)", _num(latest.score)))
    return tuple(out)


def _fcf_yield(fcf_per_share: float | None, price: float | None) -> float | None:
    """Free-cash-flow yield (percent): FCF per share over the live price. Keeps its
    sign (a negative yield is a real 'burning cash' read); ``None`` without both inputs
    or a non-positive price."""
    if fcf_per_share is None or not price or price <= 0:
        return None
    return round(fcf_per_share / price * 100, 2)


@dataclass(frozen=True)
class _SectionSpec:
    """One graded facet of the scorecard — the single source of truth. ``key`` is the
    stable id the client renders off, ``title`` its display name, ``hint`` what the
    section is about (feeds the tool schema + the prompt's section list), and
    ``metrics`` the builder that turns gathered data into its chips."""

    key: str
    title: str
    hint: str
    metrics: Callable[[_Facts], tuple[SectionMetric, ...]]


# The section catalogue, in card order. Add a facet by adding an entry (plus its
# builder above) — the tool schemas, the prompt, and the entity assembly all derive
# from this list.
_SECTIONS: tuple[_SectionSpec, ...] = (
    _SectionSpec(
        "profitability",
        "Profitability",
        "how much profit the company keeps (its margins and return on equity)",
        _profitability_metrics,
    ),
    _SectionSpec(
        "cash_generation",
        "Cash generation",
        "how much real cash the business throws off (free cash flow)",
        _cash_generation_metrics,
    ),
    _SectionSpec(
        "growth",
        "Growth",
        "how fast revenue and earnings are growing, recently and expected next year",
        _growth_metrics,
    ),
    _SectionSpec(
        "valuation",
        "Valuation",
        "the price relative to earnings and growth, and versus its industry peers",
        _valuation_metrics,
    ),
    _SectionSpec(
        "financial_health",
        "Financial health",
        "the strength of its balance sheet — how easily it covers its debts",
        _financial_health_metrics,
    ),
    _SectionSpec(
        "earnings",
        "Earnings track record",
        "its recent record of beating or missing expectations",
        _earnings_metrics,
    ),
    _SectionSpec(
        "analyst_view",
        "Analyst view",
        "what Wall Street analysts currently recommend",
        _analyst_metrics,
    ),
)

# The section list rendered into the system prompt, kept in lock-step with the registry.
_SECTION_LIST_TEXT = "; ".join(f"{s.title} ({s.hint})" for s in _SECTIONS)


def _section_schema(what: str) -> dict:
    """The forced-tool schema for one section — the model authors only these three
    fields; the numbers are attached by the service afterwards."""
    return {
        "type": "object",
        "properties": {
            "stance": {
                "type": "string",
                "enum": [s.value for s in SectionStance],
                "description": (
                    f"Whether {what} reads well for the stock: 'positive' (a point in "
                    "its favour), 'negative' (a point against), or 'neutral' (mixed or "
                    "unremarkable)."
                ),
            },
            "label": {
                "type": "string",
                "description": (
                    "A 1-3 word plain-language tag for this section (e.g. 'Exceptional', "
                    "'Expensive', 'Accelerating', 'Mostly buys'). No jargon."
                ),
            },
            "summary": {
                "type": "string",
                "description": (
                    "1-2 short sentences in plain, everyday language explaining this "
                    "section, as if to a friend with no finance background. Never empty."
                ),
            },
        },
        "required": ["stance", "label", "summary"],
    }


def _scorecard_tool() -> dict:
    """Build the forced ``submit_scorecard`` tool from the section registry — the
    overall verdict plus one object per section, so a new section flows through
    automatically."""
    props: dict = {
        "recommendation": {
            "type": "string",
            "enum": [r.value for r in Recommendation],
            "description": (
                "The overall call on the five-point scale (strong buy / buy / hold / "
                "sell / strong sell), weighing every section on balance."
            ),
        },
        "thesis": {
            "type": "string",
            "description": (
                "One short sentence in plain, everyday language — the overall take and "
                "the main reason for it, as if to a friend who doesn't follow the "
                "markets. No jargon."
            ),
        },
    }
    # Note: the model does not author `confidence` — the service computes it from data
    # coverage (see ``_confidence_for``), so it's not in the schema.
    props.update({s.key: _section_schema(s.hint) for s in _SECTIONS})
    return {
        "name": "submit_scorecard",
        "description": (
            "Record a balanced strong-buy/buy/hold/sell/strong-sell scorecard on the "
            "stock in plain, everyday language, grounded only in the figures provided. "
            "Give an overall verdict plus a read on each section."
        ),
        "input_schema": {
            "type": "object",
            "properties": props,
            "required": ["recommendation", "thesis", *[s.key for s in _SECTIONS]],
        },
    }


def _sections_tool() -> dict:
    """Build the lighter ``submit_sections`` recovery tool — only the section reads,
    derived from the same registry."""
    return {
        "name": "submit_sections",
        "description": (
            "Record ONLY the section reads for the stock — a stance, a short label, and "
            "a plain-language summary for each section — grounded only in the figures "
            "provided. Every section must have a non-empty label and a non-empty summary."
        ),
        "input_schema": {
            "type": "object",
            "properties": {s.key: _section_schema(s.hint) for s in _SECTIONS},
            "required": [s.key for s in _SECTIONS],
        },
    }


# A single forced tool pins the model to structured output; both schemas derive from
# the section registry so they never drift from it.
_SCORECARD_TOOL = _scorecard_tool()
_SECTIONS_TOOL = _sections_tool()

_SYSTEM_PROMPT = (
    "You are a friendly investing assistant explaining one stock to an everyday "
    "person with no finance background. You are given a snapshot of the stock's "
    "price, its valuation, profitability, growth and balance-sheet figures, its "
    "recent quarterly and annual earnings, what Wall Street analysts recommend, and "
    "how its valuation compares with other companies in the same industry. From only "
    "those figures, grade the stock across the sections below and give a clear, "
    "balanced overall read on a five-point scale — strong buy, buy, hold, sell, or "
    "strong sell — reserving the 'strong' calls for when the figures line up "
    "especially clearly one way.\n"
    f"The sections are: {_SECTION_LIST_TEXT}. For every section you MUST give a "
    "stance (positive/neutral/negative), a short non-empty label, and a non-empty "
    "one-to-two-sentence plain-language summary — never leave a section's label or "
    "summary blank, and do not fold the whole read into the thesis.\n"
    "When an industry benchmark is provided, weigh the stock's own price-to-"
    "earnings against it in the valuation section — a much higher figure than its "
    "peers means it's priced richly (expensive) for its industry, a much lower one "
    "means cheaply — and explain that comparison in plain words.\n"
    "Write in plain, warm, everyday language — short sentences, no jargon. When a "
    "figure matters, say what it means in a few plain words (e.g. 'its price is "
    "high compared with its earnings') rather than naming the ratio. Never assume "
    "the reader knows finance terms. Ground every statement ONLY in the figures "
    "provided — do not use outside knowledge, recent news, or prices you may "
    "recall, and never invent numbers. If the data for a section is thin, say so "
    "plainly. Be honest in every section about both the good and the bad. This is "
    "general information, not personal financial advice. Respond by calling the "
    "submit_scorecard tool."
)

_SECTIONS_SYSTEM = (
    "You already gave the overall read on this stock. Now give ONLY the section reads. "
    "For each section give a stance (positive/neutral/negative), a short non-empty "
    "label, and a non-empty one-to-two-sentence plain-language summary a non-expert can "
    "follow, grounded only in the figures below. Never leave a section's label or "
    "summary blank. The sections are: "
    f"{_SECTION_LIST_TEXT}. Respond by calling the submit_sections tool."
)

# Prepended to the same figures the first pass saw, so the recovered sections stay grounded.
_SECTIONS_INSTRUCTION = (
    "Give the section reads (a stance, label, and summary each) for this stock, "
    "grounded only in these figures:\n\n"
)


class BedrockScorecardProvider(StockScorecardProvider):
    """Generates a ``StockScorecard`` with Claude on Amazon Bedrock.

    Defaults to the fast Haiku tier (``model_id``) since the output is short and
    plain — speed matters more than extra reasoning here. ``model_id`` and
    ``region`` are deploy-time config (the model id may be a cross-region inference
    profile), so a deploy can swap in a larger model via env without a code change.
    ``client`` is an injection seam: pass a ready-made client (e.g. a test stub) to
    bypass the Anthropic SDK entirely; otherwise the Bedrock client is built lazily
    and authenticates through the process's AWS credentials.
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
    # The output is short and plain by design (a one-line thesis + a handful of brief
    # sections), so a moderate cap is ample — but it scales with the section count, so
    # this is kept well above the worst case for the full registry so a scorecard is
    # never truncated mid-section (a truncated section is one way it comes back
    # incomplete). Fewer generated tokens is the main lever on this endpoint's latency.
    _MAX_TOKENS = 3000
    # Bedrock does not enforce the tool schema's required/non-empty fields, and the
    # fast Haiku tier occasionally returns the overall verdict with the sections left
    # blank (empty label/summary). Re-issue a *targeted* sections-only call up to this
    # many extra times to fill them; paired with the use case refusing to cache an
    # incomplete read, a blank-section result is effectively never served (and never
    # frozen for the TTL). Only fires on the miss — zero cost when the first call is
    # already complete.
    _MAX_INCOMPLETE_RETRIES = 3

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
    ) -> StockScorecard:
        prompt = _render_prompt(
            stock, quarterly, annual, recommendations, industry_valuation
        )
        # One cost line per endpoint call, logged in a finally so a failure still
        # records what was spent.
        costs = CostAccumulator()
        try:
            payload = self._invoke(prompt, stock.symbol, costs)
            if payload is None:
                raise StockDataUnavailable(
                    stock.symbol, "analysis model returned no structured result"
                )
            # The forced tool requires every section's label + summary, but Bedrock does
            # not enforce it, and the fast tier sometimes packs the whole read into the
            # thesis and hands the sections back blank. Re-issue a *targeted*
            # sections-only call to fill the blanks — a narrower ask that lands reliably
            # and regenerates a fraction of the tokens. Bounded; the use case won't cache
            # an incomplete one, so a truly stuck read regenerates next view rather than
            # freezing empty sections for the TTL.
            for _ in range(self._MAX_INCOMPLETE_RETRIES):
                if not _missing_sections(payload):
                    break
                recovered = self._recover_sections(prompt, stock.symbol, costs)
                if recovered is not None:
                    payload = _merge_section_reads(payload, recovered)
            return _build_scorecard(
                stock.symbol,
                payload,
                self._model_id,
                stock,
                quarterly,
                recommendations,
                industry_valuation,
            )
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
        tool: dict = _SCORECARD_TOOL,
        tool_name: str = "submit_scorecard",
        system: str = _SYSTEM_PROMPT,
    ) -> dict | None:
        """One forced-tool call, returning the tool's arguments (or ``None`` if the
        model somehow didn't call the forced tool). Defaults to the full scorecard
        tool; the retry path passes the lighter ``submit_sections`` tool. Any
        SDK/botocore failure is mapped to this port's documented ``StockDataUnavailable``.
        The call's token usage is folded into ``costs`` for the caller's single
        per-endpoint cost line."""
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

    def _recover_sections(
        self, prompt: str, key: str, costs: CostAccumulator
    ) -> dict | None:
        """One targeted retry that regenerates *only* the section reads
        (stance/label/summary), grounded in the same figures the first pass saw — far
        fewer output tokens than re-running the whole scorecard, and a narrower ask that
        lands more reliably. Returns the ``submit_sections`` arguments, or ``None`` when
        the model didn't call the tool (the caller then leaves the payload unchanged and
        consumes a bounded retry)."""
        return self._invoke(
            _SECTIONS_INSTRUCTION + prompt,
            key,
            costs,
            tool=_SECTIONS_TOOL,
            tool_name="submit_sections",
            system=_SECTIONS_SYSTEM,
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


def _section_read_complete(read: object) -> bool:
    """Whether a section's model-authored read carries its substance — a non-empty
    label *and* a non-empty summary (the two fields the card shows in words). The
    stance defaults harmlessly to neutral, so it's not the completeness signal."""
    if not isinstance(read, dict):
        return False
    return bool(str(read.get("label") or "").strip()) and bool(
        str(read.get("summary") or "").strip()
    )


def _missing_sections(payload: dict | None) -> bool:
    """True when a returned scorecard is present but any section is missing or blank —
    the signal to retry. A ``None`` payload (the model didn't call the tool at all) is
    left for the caller to surface as ``StockDataUnavailable``, not retried."""
    if payload is None:
        return False
    return any(not _section_read_complete(payload.get(s.key)) for s in _SECTIONS)


def _merge_section_reads(payload: dict, recovered: dict) -> dict:
    """Fill only the *blank* section reads from a targeted recovery call, leaving any
    section the first pass already wrote untouched — so a retry that recovers some
    sections never overwrites the good ones. The overall verdict and the metric chips
    are untouched (the recovery only carries the sections' words)."""
    merged = dict(payload)
    for s in _SECTIONS:
        if not _section_read_complete(merged.get(s.key)) and _section_read_complete(
            recovered.get(s.key)
        ):
            merged[s.key] = recovered[s.key]
    return merged


def _build_scorecard(
    symbol: str,
    payload: dict,
    model_id: str,
    stock: Stock,
    quarterly: QuarterlyEarningsTimeline | None,
    recommendations: AnalystRecommendations | None,
    industry_valuation: IndustryValuation | None,
) -> StockScorecard:
    """Map the validated tool arguments onto the domain entity.

    The overall verdict comes from the model (with a defensive guard: an off-schema
    recommendation surfaces as this port's documented ``StockDataUnavailable`` rather
    than leaking out). Each section merges the model's *words* (stance / label /
    summary) with metric chips computed here from the data already gathered (via the
    section registry), so the numbers are always the service's, never the model's. The
    ``confidence`` is likewise the service's — a deterministic read of data coverage
    (see ``_confidence_for``), not a guess the model makes.
    """
    try:
        recommendation = Recommendation(payload["recommendation"])
        thesis = str(payload["thesis"]).strip()
    except (KeyError, ValueError) as exc:
        raise StockDataUnavailable(
            symbol, f"analysis model returned an unexpected result: {exc}"
        ) from exc
    facts = _Facts(stock, quarterly, recommendations, industry_valuation)
    sections = tuple(
        _section(s.key, s.title, payload.get(s.key), s.metrics(facts))
        for s in _SECTIONS
    )
    return StockScorecard(
        symbol=symbol,
        recommendation=recommendation,
        confidence=_confidence_for(sections),
        thesis=thesis,
        sections=sections,
        model=model_id,
        generated_at=datetime.now(timezone.utc),
    )


# How much of the scorecard has to be backed by real figures for each confidence
# band, as a fraction of the sections. Confidence here is a read of *data coverage* —
# how many data sources resolved — not the model's conviction: a rich, multi-source
# snapshot reads HIGH, a bare quote LOW. (With 7 sections: HIGH needs >=6 covered,
# MEDIUM 3-5, LOW <=2 — i.e. HIGH wants the earnings/analyst context on top of the
# fundamentals-fed sections, not fundamentals alone.)
_HIGH_COVERAGE = 0.8
_MEDIUM_COVERAGE = 0.4


def _confidence_for(sections: tuple[ScorecardSection, ...]) -> Confidence:
    """Confidence as a deterministic read of *data coverage* — the share of sections
    that came back with real figures (i.e. how many data sources resolved). This is the
    'service owns the numbers' split applied to confidence: how much data we had is a
    fact about our gather, so we compute it rather than trust the model to guess it. A
    section with no chips is one whose source (fundamentals / earnings / analyst /
    industry) didn't resolve for this symbol."""
    if not sections:
        return Confidence.LOW
    covered = sum(1 for s in sections if s.metrics)
    ratio = covered / len(sections)
    if ratio >= _HIGH_COVERAGE:
        return Confidence.HIGH
    if ratio >= _MEDIUM_COVERAGE:
        return Confidence.MEDIUM
    return Confidence.LOW


def _section(
    key: str, title: str, read: object, metrics: tuple[SectionMetric, ...]
) -> ScorecardSection:
    """Combine the model's read of one section with its (service-computed) chips.

    A missing or malformed section, or an off-enum stance, degrades to a neutral,
    empty-summary section rather than sinking the whole scorecard — the use case
    won't cache an incomplete read (an empty summary), so it regenerates next view."""
    read = read if isinstance(read, dict) else {}
    try:
        stance = SectionStance(read.get("stance"))
    except ValueError:
        stance = SectionStance.NEUTRAL
    return ScorecardSection(
        key=key,
        title=title,
        stance=stance,
        label=str(read.get("label") or "").strip(),
        summary=str(read.get("summary") or "").strip(),
        metrics=metrics,
    )


def _metrics(*rows: tuple[str, object, str]) -> tuple[SectionMetric, ...]:
    """Format a batch of ``(label, value, suffix)`` rows into chips, dropping any whose
    value is ``None`` (a figure the gather didn't have) — so a chip is only present
    when its number is real."""
    out: list[SectionMetric] = []
    for label, value, suffix in rows:
        if value is None:
            continue
        out.append(SectionMetric(label, f"{_num(value)}{suffix}"))
    return tuple(out)


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
