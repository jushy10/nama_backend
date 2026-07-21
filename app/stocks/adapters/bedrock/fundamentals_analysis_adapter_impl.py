from datetime import datetime, timezone

from app.stocks.adapters.bedrock.cost import log_model_cost
from app.stocks.ai.analysis.entities import (
    Confidence,
    FundamentalsAnalysis,
    FundamentalsVerdict,
)
from app.stocks.ai.analysis.interfaces import FundamentalsAnalysisAdapter
from app.stocks.entities import Stock
from app.stocks.exceptions import StockDataUnavailable
from app.stocks.company.ticker.entities import PeHistoryStats
from app.stocks.catalog.universe.entities import IndustryValuation

# A single forced tool pins the model to structured output: Claude must call
# submit_fundamentals_findings, so the response comes back as validated JSON arguments
# instead of prose. The schema mirrors the FundamentalsAnalysis entity, minus the fields
# the adapter stamps itself (symbol, model, generated_at).
_ANALYSIS_TOOL = {
    "name": "submit_fundamentals_findings",
    "description": (
        "Record a plain, everyday-language read of a company's fundamentals — how "
        "profitable and financially sound the business is, whether it's growing, and "
        "whether the shares look reasonably priced against all that — grounded only in the "
        "figures in the prompt."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "verdict": {
                "type": "string",
                "enum": [v.value for v in FundamentalsVerdict],
                "description": (
                    "The overall read of the company's fundamentals: 'strong' when they "
                    "clearly hold up (healthy margins and growth, a sound balance sheet, a "
                    "valuation the numbers support), 'weak' when they clearly don't (thin or "
                    "falling margins, shrinking growth, heavy debt, or a price the business "
                    "can't justify), 'mixed' when the picture is uneven or the signals "
                    "conflict."
                ),
            },
            "confidence": {
                "type": "string",
                "enum": [c.value for c in Confidence],
                "description": (
                    "How firmly to hold that verdict given the data: 'high' when the figures "
                    "are plentiful and point the same way, 'low' when they're sparse or "
                    "conflict, 'medium' otherwise."
                ),
            },
            "summary": {
                "type": "string",
                "description": (
                    "2-3 short sentences in plain, everyday language: how this company's "
                    "fundamentals look right now — how profitable it is, whether it's "
                    "growing, how sound its finances are, and whether the shares look "
                    "reasonably priced — as if to a friend who doesn't follow markets. No "
                    "jargon."
                ),
            },
            "findings": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 2,
                "maxItems": 4,
                "description": (
                    "2 to 4 short, plain-language takeaways a reader should remember — one "
                    "clear point each (e.g. a fat profit margin, revenue growth that's "
                    "fading, a price that's high compared with earnings, or a heavy debt "
                    "load). No jargon, no invented numbers. Always give at least two; never "
                    "return an empty list."
                ),
            },
        },
        "required": ["verdict", "confidence", "summary", "findings"],
    },
}

_SYSTEM_PROMPT = (
    "You are a friendly investing assistant explaining a company's fundamentals to an "
    "everyday person with no finance background. You are given a snapshot of the stock: its "
    "valuation multiples (how its price compares with its earnings, book value and sales, and "
    "how the whole business — including its debt — compares with its operating earnings via "
    "EV/EBITDA, which is fairer than price-to-earnings when companies carry very different debt "
    "loads; all both on past results and on what analysts expect next), its cash generation (how much "
    "free and operating cash flow it produces per dollar of share price, and how fast that "
    "cash is growing), its profitability (margins and return on equity), its financial health "
    "(debt and how easily it covers short-term bills), how fast its revenue and earnings are "
    "growing, its dividend and size, how its price-to-earnings compares with other companies "
    "in the same industry, and where its price-to-earnings sits versus its own history. From "
    "only those figures, give a clear, balanced read of how solid the business is and whether "
    "the shares look reasonably priced.\n"
    "When an industry benchmark is provided, weigh the stock's own price-to-earnings against "
    "it — a much higher figure than its peers means it's priced richly (expensive) for its "
    "industry, a much lower one means cheaply — and explain that comparison in plain words. "
    "When the stock's own P/E history is provided, also say whether it looks cheap or dear "
    "versus how it has usually traded (a low percentile means rarely cheaper, a high one "
    "rarely dearer); if that signal is 'not_meaningful' the earnings are at an unusual low, "
    "so don't call it expensive on that basis. "
    "Fundamentals that clearly hold up (good margins and growth, a sound balance sheet, a "
    "fair price) are 'strong'; thin or falling margins, shrinking growth, heavy debt, or a "
    "price the business can't justify are 'weak'; an uneven or conflicting picture is "
    "'mixed'.\n"
    "Write in plain, warm, everyday language — short sentences, no jargon. When a figure "
    "matters, say what it means in a few plain words (e.g. 'it keeps a big share of its sales "
    "as profit', 'its price is high compared with its earnings') rather than naming the "
    "ratio. Ground every statement ONLY in the figures provided — do not use outside "
    "knowledge, recent news, or numbers you may recall, and never invent figures. If the data "
    "is thin, say so plainly and lower your confidence. Always give at least two findings — "
    "never leave that list empty. This is general information, not personal financial advice. "
    "Respond by calling the submit_fundamentals_findings tool."
)

# The key the adapter reports failures under.
_KEY = "fundamentals-analysis"


class FundamentalsAnalysisAdapterImpl(FundamentalsAnalysisAdapter):
    # Full versioned inference-profile id — Haiku 4.5 has no bare alias on Bedrock, so the
    # short form 400s. Same default as the earnings/ratings/market/sector reads.
    _DEFAULT_MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
    _DEFAULT_REGION = "us-east-1"
    # Short, plain output (a few sentences + a few findings), so a tight cap is ample — and
    # fewer generated tokens is the main lever on latency.
    _MAX_TOKENS = 900

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
        # Imported here, not at module load: the SDK is an optional heavyweight dependency
        # (it pulls boto3). A missing extra raises ImportError, which the wiring
        # (router.get_fundamentals_analysis_provider) turns into a 503.
        from anthropic import AnthropicBedrock

        self._client = AnthropicBedrock(aws_region=region)

    def analyze(
        self,
        stock: Stock,
        industry_valuation: IndustryValuation | None = None,
        pe_history: PeHistoryStats | None = None,
    ) -> FundamentalsAnalysis:
        prompt = _render_prompt(stock, industry_valuation, pe_history)
        try:
            message = self._client.messages.create(
                model=self._model_id,
                max_tokens=self._MAX_TOKENS,
                system=_SYSTEM_PROMPT,
                tools=[_ANALYSIS_TOOL],
                tool_choice={"type": "tool", "name": "submit_fundamentals_findings"},
                messages=[{"role": "user", "content": prompt}],
            )
        except Exception as exc:  # SDK/botocore raise a family of errors; map all
            raise StockDataUnavailable(
                stock.symbol, f"fundamentals analysis model call failed: {exc}"
            ) from exc
        log_model_cost(
            label="fundamentals analysis",
            model_id=self._model_id,
            message=message,
            key=stock.symbol,
        )
        payload = _tool_payload(message)
        if payload is None:
            raise StockDataUnavailable(
                stock.symbol, "fundamentals analysis model returned no structured result"
            )
        return _to_entity(stock.symbol, payload, self._model_id)


def _tool_payload(message) -> dict | None:
    for block in getattr(message, "content", None) or []:
        if (
            getattr(block, "type", None) == "tool_use"
            and getattr(block, "name", None) == "submit_fundamentals_findings"
        ):
            inputs = getattr(block, "input", None)
            if isinstance(inputs, dict):
                return inputs
    return None


def _to_entity(symbol: str, payload: dict, model_id: str) -> FundamentalsAnalysis:
    try:
        verdict = FundamentalsVerdict(payload["verdict"])
        confidence = Confidence(payload["confidence"])
        summary = str(payload["summary"]).strip()
    except (KeyError, ValueError) as exc:
        raise StockDataUnavailable(
            symbol, f"fundamentals analysis model returned an unexpected result: {exc}"
        ) from exc
    findings = _string_tuple(payload.get("findings"))
    return FundamentalsAnalysis(
        symbol=symbol.upper(),
        verdict=verdict,
        confidence=confidence,
        summary=summary,
        findings=findings,
        model=model_id,
        generated_at=datetime.now(timezone.utc),
    )


def _string_tuple(value) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(text for item in value if (text := str(item).strip()))


def _num(value: object) -> str:
    if isinstance(value, bool):  # bool is an int subclass — keep it as-is
        return str(value)
    if isinstance(value, float):
        return f"{value:,.2f}"
    return str(value)


def _render_prompt(
    stock: Stock,
    industry_valuation: IndustryValuation | None,
    pe_history: PeHistoryStats | None = None,
) -> str:
    metrics = stock.metrics
    fields: list[tuple[str, object]] = [
        ("Name", stock.name),
        ("Price", stock.price),
        ("Market cap (USD)", stock.market_cap),
        ("Dividend yield %", stock.dividend_yield),
        ("Dividend per share", stock.dividend_per_share),
    ]
    if metrics is not None:
        fields += [
            ("P/E (trailing)", metrics.pe),
            ("PEG (trailing)", metrics.peg),
            ("P/B", metrics.pb),
            ("P/S", metrics.ps),
            # EV/EBITDA — the whole enterprise (equity + net debt) against its operating
            # earnings, so it's comparable across companies with different debt loads in a way
            # P/E isn't. Priced live off the quote upstream, like the other multiples.
            ("EV/EBITDA (trailing)", metrics.ev_to_ebitda),
            ("EPS (trailing)", metrics.eps),
            ("FCF/share (trailing)", metrics.fcf_per_share),
            # Cash-flow yields the ticker card shows, priced here on the live quote so the
            # model can read "how much cash am I buying per dollar" and the capex drag (the
            # gap between the operating and free yields).
            ("Price/FCF (trailing)", _price_to_fcf(metrics.fcf_per_share, stock.price)),
            ("FCF yield %", _cash_yield(metrics.fcf_per_share, stock.price)),
            ("OCF yield % (pre-capex)", _cash_yield(metrics.ocf_per_share, stock.price)),
            ("Revenue growth YoY %", metrics.revenue_growth_yoy),
            ("EPS growth YoY %", metrics.eps_growth_yoy),
            ("FCF/share growth YoY %", metrics.fcf_growth_yoy),
            ("Gross margin %", metrics.gross_margin),
            ("Operating margin %", metrics.operating_margin),
            ("Net margin %", metrics.net_margin),
            ("ROE %", metrics.roe),
            ("Current ratio", metrics.current_ratio),
            ("Debt/equity", metrics.debt_to_equity),
            ("Beta", metrics.beta),
        ]
    # Forward-looking consensus: what analysts expect next — the same figures the ticker
    # card's forward valuation is built on. Trailing metrics say what the business has done;
    # these say what it's expected to do.
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
    lines = [f"Fundamentals for {stock.symbol}:"]
    lines += [f"- {label}: {_num(value)}" for label, value in fields if value is not None]
    benchmark = _render_industry_valuation(industry_valuation)
    if benchmark:
        lines.append("")
        lines.append(benchmark)
    history = _render_pe_history(pe_history)
    if history:
        lines.append("")
        lines.append(history)
    return "\n".join(lines)


def _price_to_fcf(fcf_per_share: object, price: float | None) -> float | None:
    if not isinstance(fcf_per_share, (int, float)) or isinstance(fcf_per_share, bool):
        return None
    if fcf_per_share <= 0 or not price or price <= 0:
        return None
    return round(price / fcf_per_share, 2)


def _cash_yield(per_share: object, price: float | None) -> float | None:
    if not isinstance(per_share, (int, float)) or isinstance(per_share, bool):
        return None
    if not price or price <= 0:
        return None
    return round(per_share / price * 100, 2)


def _render_pe_history(history: PeHistoryStats | None) -> str:
    if history is None:
        return ""
    lines = [
        "Valuation vs its own history "
        f"(trailing P/E across {history.sample_size} past earnings releases):",
        f"- Current trailing P/E: {_num(history.current_pe)}",
        f"- Typical (median) P/E: {_num(history.median_pe)}",
        f"- Usual range (25th-75th percentile): "
        f"{_num(history.p25_pe)} to {_num(history.p75_pe)}",
        f"- Current percentile in that history: {_num(history.current_percentile)} "
        "(0 = cheapest it's been, 100 = dearest)",
        f"- Gap to its typical P/E %: {_num(history.discount_to_median_percent)} "
        "(negative = cheaper than usual)",
        f"- Signal: {history.signal.value}",
    ]
    return "\n".join(lines)


def _render_industry_valuation(valuation: IndustryValuation | None) -> str:
    if valuation is None or valuation.count == 0 or valuation.median_pe is None:
        return ""
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
