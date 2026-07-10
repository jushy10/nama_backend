"""Interface Adapter: plain-English stock screening via Claude on Amazon Bedrock.

The only module that knows Bedrock (and the Anthropic SDK) exists for the AI
screener. It takes a user's request ("mega-cap technology stocks", "top S&P 500
names by revenue growth") plus the universe's current sector/industry vocabulary,
and asks Claude to fill in the screen's own filters — returning a ``ScreenIntent``
the use case feeds straight into the ordinary ``SearchStocks``. The model only
*chooses filters*; it never names stocks, so an AI screen can only ever surface
real screened rows. Swap models or vendors and only this file changes.

It mirrors the analysis adapters on every axis that matters:

* **Auth is the runtime's job.** Bedrock authenticates through the process's AWS
  credentials (the ECS task role in production) — no API key to read or pass.
* **Structured output via a forced tool call.** Claude must call one
  ``build_screen`` tool, so the response is validated JSON arguments mapping onto
  ``ScreenIntent``, not prose to parse.
* **Constrained vocabulary.** The sector/industry slugs currently in the universe
  are injected as ``enum`` lists on the tool schema *per call*, so the model can
  only pick values the search can actually match — an invented industry can't leak
  through. (The use case re-normalizes anyway, so it degrades to "matches nothing"
  even if one did.)

The Anthropic SDK is imported lazily inside ``__init__`` so the app and the offline
tests import cleanly without the ``bedrock`` extra; a test injects a stub client
through the same seam. Any Bedrock/SDK failure is translated to
``StockDataUnavailable`` — the one error the port documents (a 502 at the edge).

Docs: https://docs.anthropic.com/en/api/claude-on-amazon-bedrock
"""

from collections.abc import Sequence

from app.stocks.adapters.bedrock.cost import CostAccumulator
from app.stocks.exceptions import StockDataUnavailable
from app.stocks.universe.entities import (
    MarketCapTier,
    ScreenIntent,
    SortDirection,
    StockSort,
)
from app.stocks.universe.ports import ScreenerQueryTranslator

_SYSTEM_PROMPT = (
    "You convert a plain-English stock-screen request into a set of structured "
    "filters for a screener over US-listed companies (market cap >= $1B). You do "
    "NOT pick individual stocks — you only choose the filters, and the screener "
    "runs them.\n"
    "Rules:\n"
    "- Only use the sector and industry values provided in the tool schema. If a "
    "request names a specific line of business (e.g. semiconductors, banks, "
    "biotech), prefer the matching INDUSTRY value(s) over a broad sector; a request "
    "can map to several (an OR set), so include every value that clearly fits "
    "(e.g. 'semiconductor stocks' -> the semiconductor industry plus semiconductor "
    "equipment/materials if present).\n"
    "- Map size words to market-cap tiers: mega (>= $200B), large ($10-200B), "
    "mid ($2-10B), small (< $2B). 'Large-cap and up' means large + mega.\n"
    "- Use the index flags only when the request explicitly mentions the S&P 500 "
    "or the Nasdaq-100.\n"
    "- Set a sort only when the request implies an ordering. 'Top / best / highest "
    "/ biggest / fastest-growing' means descending; 'cheapest / lowest / smallest' "
    "means ascending. Choose the sort field that matches: market_cap for size, "
    "revenue_growth or eps_growth (or growth for both) for growth, the forward_* "
    "variants for expected/next-year growth, pe for cheap-on-earnings (ascending), "
    "fcf_yield for cheap-on-cash (descending).\n"
    "- Set limit only when the request asks for a specific count ('top 10' -> 10).\n"
    "- Use the free-text query ONLY for a specific company name or brand keyword "
    "that no sector/industry expresses; leave it unset otherwise.\n"
    "- Leave any filter unset when the request doesn't call for it. Never invent "
    "values. Respond by calling the build_screen tool."
)


class BedrockScreenerQueryTranslator(ScreenerQueryTranslator):
    """Translates a plain-English screen request into a ``ScreenIntent`` with Claude on
    Amazon Bedrock.

    Defaults to the fast Haiku tier (``model_id``): the output is a short, structured filter
    set, so speed matters more than extra reasoning. ``model_id`` / ``region`` are deploy-time
    config (the id may be a cross-region inference profile), env-overridable so a deploy can
    swap models without a code change. ``client`` is an injection seam — pass a ready-made
    client (a test stub) to bypass the Anthropic SDK entirely; otherwise the Bedrock client is
    built lazily and authenticates through the process's AWS credentials.
    """

    # The same fast Haiku tier the analysis adapters default to; the full versioned id
    # (the short alias 400s on Bedrock). Env-overridable via BEDROCK_SCREENER_MODEL_ID.
    _DEFAULT_MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
    _DEFAULT_REGION = "us-east-1"
    # The output is a small JSON filter object, so a tight cap is ample.
    _MAX_TOKENS = 512

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
        # dependency (it pulls boto3) neither the app's other endpoints nor the
        # offline tests need. A missing extra raises ImportError, which the wiring
        # (router.get_screener_translator) turns into a 503.
        from anthropic import AnthropicBedrock

        self._client = AnthropicBedrock(aws_region=region)

    def translate(
        self,
        query: str,
        *,
        sectors: Sequence[str],
        industries: Sequence[str],
    ) -> ScreenIntent:
        tool = _build_tool(sectors, industries)
        costs = CostAccumulator()
        try:
            payload = self._invoke(query, tool, costs)
            return _to_intent(payload)
        finally:
            # One cost line per screen request, at info in CloudWatch. The key is a short
            # prefix of the request so the log ties to the query without dumping the lot.
            costs.log(label="ai screen", model_id=self._model_id, key=query[:48])

    def _invoke(self, query: str, tool: dict, costs: CostAccumulator) -> dict | None:
        """One forced-tool call, returning the ``build_screen`` arguments (or ``None`` if the
        model somehow didn't call the tool). Any SDK/botocore failure is mapped to this port's
        documented ``StockDataUnavailable``; the call's token usage folds into ``costs``."""
        try:
            message = self._client.messages.create(
                model=self._model_id,
                max_tokens=self._MAX_TOKENS,
                system=_SYSTEM_PROMPT,
                tools=[tool],
                tool_choice={"type": "tool", "name": "build_screen"},
                messages=[{"role": "user", "content": query}],
            )
        except Exception as exc:  # SDK/botocore raise a family of errors; map them all
            raise StockDataUnavailable(
                query[:48], f"screen translation call failed: {exc}"
            ) from exc
        costs.add(message)
        return _tool_payload(message)


def _build_tool(sectors: Sequence[str], industries: Sequence[str]) -> dict:
    """Build the forced ``build_screen`` tool, pinning the sector/industry fields to the
    universe's *current* slugs (as ``enum`` lists) so the model can only choose values the
    search can match. When a vocabulary is empty (nothing classified yet) its filter is
    omitted rather than offered as an empty ``enum`` the model couldn't satisfy."""
    properties: dict = {
        "query": {
            "type": "string",
            "description": (
                "Optional free-text term matched against company name or ticker. Use ONLY "
                "for a specific company name or brand keyword that no sector/industry "
                "expresses. Leave unset when the request is expressed by the other filters."
            ),
        },
        "in_sp500": {
            "type": "boolean",
            "description": "Set true to restrict to S&P 500 members. Only when explicitly asked.",
        },
        "in_nasdaq100": {
            "type": "boolean",
            "description": "Set true to restrict to Nasdaq-100 members. Only when explicitly asked.",
        },
        "market_cap_tiers": {
            "type": "array",
            "items": {"type": "string", "enum": [t.value for t in MarketCapTier]},
            "description": (
                "Company size buckets to include (an OR set): mega (>= $200B), large "
                "($10-200B), mid ($2-10B), small (< $2B)."
            ),
        },
        "sort": {
            "type": "string",
            "enum": [s.value for s in StockSort],
            "description": (
                "How to rank results. Omit if no ordering is implied. market_cap for size; "
                "revenue_growth / eps_growth / growth for trailing growth; forward_* for "
                "expected next-year growth; pe for cheap-on-earnings; fcf_yield for "
                "cheap-on-cash."
            ),
        },
        "direction": {
            "type": "string",
            "enum": [d.value for d in SortDirection],
            "description": (
                "Sort direction: desc for 'top/highest/biggest/fastest', asc for "
                "'cheapest/lowest/smallest'. Ignored without a sort."
            ),
        },
        "limit": {
            "type": "integer",
            "description": "Result count when the request asks for a specific number (e.g. 'top 10' -> 10). Omit otherwise.",
        },
    }
    if sectors:
        properties["sectors"] = {
            "type": "array",
            "items": {"type": "string", "enum": list(sectors)},
            "description": (
                "Sector slugs to match (an OR set). Choose only from the allowed values; "
                "prefer a specific industry over a broad sector when the request names a "
                "line of business."
            ),
        }
    if industries:
        properties["industries"] = {
            "type": "array",
            "items": {"type": "string", "enum": list(industries)},
            "description": (
                "Industry slugs to match (an OR set). Choose only from the allowed values. "
                "Include every value that clearly fits the request."
            ),
        }
    return {
        "name": "build_screen",
        "description": (
            "Record the screen filters that express the user's request. Set only the fields "
            "the request calls for; leave the rest unset."
        ),
        "input_schema": {
            "type": "object",
            "properties": properties,
            "required": [],
        },
    }


def _tool_payload(message) -> dict | None:
    """Pull the build_screen arguments out of the model's tool call, if any."""
    for block in getattr(message, "content", None) or []:
        if (
            getattr(block, "type", None) == "tool_use"
            and getattr(block, "name", None) == "build_screen"
        ):
            inputs = getattr(block, "input", None)
            if isinstance(inputs, dict):
                return inputs
    return None


def _to_intent(payload: dict | None) -> ScreenIntent:
    """Map the validated tool arguments onto the domain ``ScreenIntent``.

    Defensive throughout: the forced-tool schema constrains the shape, but a stray or
    off-schema value never raises — an unknown enum is dropped, a non-list field is treated as
    empty, a non-positive limit as unset — so a screen request always yields a usable intent
    (an all-unset one is a neutral browse). The use case re-normalizes on top of this."""
    if not payload:
        return ScreenIntent()
    query = payload.get("query")
    text = query.strip() if isinstance(query, str) else ""
    return ScreenIntent(
        query=text or None,
        sectors=_string_tuple(payload.get("sectors")),
        industries=_string_tuple(payload.get("industries")),
        in_sp500=_bool_or_none(payload.get("in_sp500")),
        in_nasdaq100=_bool_or_none(payload.get("in_nasdaq100")),
        market_cap_tiers=_enum_tuple(MarketCapTier, payload.get("market_cap_tiers")),
        sort=_enum_or_none(StockSort, payload.get("sort")),
        direction=_enum_or_none(SortDirection, payload.get("direction"))
        or SortDirection.DESC,
        limit=_positive_int_or_none(payload.get("limit")),
    )


def _string_tuple(value) -> tuple[str, ...]:
    """Coerce a list field into a tuple of non-empty, stripped strings (else empty)."""
    if not isinstance(value, list):
        return ()
    return tuple(text for item in value if (text := str(item).strip()))


def _enum_tuple(enum_cls, value) -> tuple:
    """Coerce a list field into a tuple of ``enum_cls`` members, dropping unknown values."""
    out = []
    for item in _string_tuple(value):
        member = _enum_or_none(enum_cls, item)
        if member is not None:
            out.append(member)
    return tuple(dict.fromkeys(out))  # dedupe, order-preserving


def _enum_or_none(enum_cls, value):
    """The ``enum_cls`` member for ``value``, or ``None`` when it's missing/unknown."""
    if not isinstance(value, str):
        return None
    try:
        return enum_cls(value)
    except ValueError:
        return None


def _bool_or_none(value) -> bool | None:
    """Pass a real bool through; anything else (including a stray string) is 'unset'."""
    return value if isinstance(value, bool) else None


def _positive_int_or_none(value) -> int | None:
    """A positive int (the requested count), or ``None`` for missing/zero/negative/non-int.
    A bool is rejected — it's an int subclass but never a valid count here."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value > 0 else None
