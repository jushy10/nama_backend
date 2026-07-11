"""Interface Adapter: plain-English ETF screening via Claude on Amazon Bedrock.

The only module that knows Bedrock (and the Anthropic SDK) exists for the AI ETF
screener. It takes a user's request ("cheap S&P 500 index funds", "high-yield
dividend ETFs", "gold funds by size") plus the stored set's current category
vocabulary, and asks Claude to fill in the screen's own filters — returning an
``EtfScreenIntent`` the use case hands back to the client to apply to the ordinary
``GET /stocks/etfs`` search. The model only *chooses filters*; it never names funds,
so an AI screen can only ever surface real screened rows. Swap models or vendors and
only this file changes.

The ETF sibling of ``screener_query_adapter.py`` (the stock screener) — same shape on
every axis:

* **Auth is the runtime's job.** Bedrock authenticates through the process's AWS
  credentials (the ECS task role in production) — no API key to read or pass.
* **Structured output via a forced tool call.** Claude must call one
  ``build_etf_screen`` tool, so the response is validated JSON arguments mapping onto
  ``EtfScreenIntent``, not prose to parse.
* **Constrained vocabulary.** The category slugs currently in the stored set are
  injected as an ``enum`` list on the tool schema *per call*, so the model can only
  pick values the search can actually match. (The use case re-normalizes anyway, so it
  degrades to "matches nothing" even if one leaked through.)

The Anthropic SDK is imported lazily inside ``__init__`` so the app and the offline
tests import cleanly without the ``bedrock`` extra; a test injects a stub client
through the same seam. Any Bedrock/SDK failure is translated to
``StockDataUnavailable`` — the one error the port documents (a 502 at the edge).

Docs: https://docs.anthropic.com/en/api/claude-on-amazon-bedrock
"""

from collections.abc import Sequence

from app.stocks.adapters.bedrock.cost import CostAccumulator
from app.stocks.etfs.entities import EtfScreenIntent, EtfSort, SortDirection
from app.stocks.etfs.ports import EtfScreenerQueryTranslator
from app.stocks.exceptions import StockDataUnavailable

_SYSTEM_PROMPT = (
    "You convert a plain-English ETF-screen request into a set of structured filters "
    "for a screener over the top US exchange-traded funds. You do NOT pick individual "
    "funds — you only choose the filters, and the screener runs them.\n"
    "Rules:\n"
    "- Only use the fund-category values provided in the tool schema. A request can map "
    "to several (an OR set), so include every category that clearly fits (e.g. 'growth "
    "funds' -> the large/mid/small growth categories that are present). If a request names "
    "no fund type, leave categories unset.\n"
    "- Set a sort only when the request implies an ordering. Choose the field that "
    "matches: net_assets for size ('biggest / largest / top' funds, descending), "
    "expense_ratio for cost ('cheapest / lowest-fee', ascending), dividend_yield for "
    "income ('highest-yield / best income', descending).\n"
    "- Set direction to match: desc for 'top / biggest / highest', asc for 'cheapest / "
    "lowest'. Ignored without a sort.\n"
    "- Set limit only when the request asks for a specific count ('top 10' -> 10).\n"
    "- Use the free-text query ONLY for a specific fund name, issuer, or brand keyword "
    "that no category expresses (e.g. 'Vanguard', 'ARK', 'SPY'); leave it unset otherwise.\n"
    "- Leave any filter unset when the request doesn't call for it. Never invent values. "
    "Respond by calling the build_etf_screen tool."
)


class BedrockEtfScreenerQueryTranslator(EtfScreenerQueryTranslator):
    """Translates a plain-English ETF-screen request into an ``EtfScreenIntent`` with Claude on
    Amazon Bedrock.

    Defaults to the fast Haiku tier (``model_id``): the output is a short, structured filter set,
    so speed matters more than extra reasoning. ``model_id`` / ``region`` are deploy-time config
    (the id may be a cross-region inference profile), env-overridable so a deploy can swap models
    without a code change. It shares the stock screener's ``BEDROCK_SCREENER_MODEL_ID`` env (one
    screener-model config drives both). ``client`` is an injection seam — pass a ready-made client
    (a test stub) to bypass the Anthropic SDK entirely; otherwise the Bedrock client is built
    lazily and authenticates through the process's AWS credentials.
    """

    # The same fast Haiku tier the analysis adapters default to; the full versioned id (the short
    # alias 400s on Bedrock). Env-overridable via BEDROCK_SCREENER_MODEL_ID (shared with the stock
    # screener — one screener-model config for both).
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
        # Imported here, not at module load: the SDK is an optional heavyweight dependency (it
        # pulls boto3) neither the app's other endpoints nor the offline tests need. A missing
        # extra raises ImportError, which the wiring (router.get_etf_screener_translator) turns
        # into a 503.
        from anthropic import AnthropicBedrock

        self._client = AnthropicBedrock(aws_region=region)

    def translate(
        self,
        query: str,
        *,
        categories: Sequence[str],
    ) -> EtfScreenIntent:
        tool = _build_tool(categories)
        costs = CostAccumulator()
        try:
            payload = self._invoke(query, tool, costs)
            return _to_intent(payload)
        finally:
            # One cost line per screen request, at info in CloudWatch. The key is a short prefix of
            # the request so the log ties to the query without dumping the lot.
            costs.log(label="ai etf screen", model_id=self._model_id, key=query[:48])

    def _invoke(self, query: str, tool: dict, costs: CostAccumulator) -> dict | None:
        """One forced-tool call, returning the ``build_etf_screen`` arguments (or ``None`` if the
        model somehow didn't call the tool). Any SDK/botocore failure is mapped to this port's
        documented ``StockDataUnavailable``; the call's token usage folds into ``costs``."""
        try:
            message = self._client.messages.create(
                model=self._model_id,
                max_tokens=self._MAX_TOKENS,
                system=_SYSTEM_PROMPT,
                tools=[tool],
                tool_choice={"type": "tool", "name": "build_etf_screen"},
                messages=[{"role": "user", "content": query}],
            )
        except Exception as exc:  # SDK/botocore raise a family of errors; map them all
            raise StockDataUnavailable(
                query[:48], f"etf screen translation call failed: {exc}"
            ) from exc
        costs.add(message)
        return _tool_payload(message)


def _build_tool(categories: Sequence[str]) -> dict:
    """Build the forced ``build_etf_screen`` tool, pinning the category field to the stored set's
    *current* slugs (as an ``enum`` list) so the model can only choose values the search can match.
    When the vocabulary is empty (nothing categorised yet) the field is omitted rather than offered
    as an empty ``enum`` the model couldn't satisfy."""
    properties: dict = {
        "query": {
            "type": "string",
            "description": (
                "Optional free-text term matched against fund name or ticker. Use ONLY for a "
                "specific fund name, issuer, or brand keyword that no category expresses (e.g. "
                "'Vanguard', 'ARK', 'SPY'). Leave unset when the request is expressed by the "
                "category filter."
            ),
        },
        "sort": {
            "type": "string",
            "enum": [s.value for s in EtfSort],
            "description": (
                "How to rank results. Omit if no ordering is implied. net_assets for size (the "
                "biggest/top funds); expense_ratio for cost (cheapest with asc); dividend_yield "
                "for income (highest with desc)."
            ),
        },
        "direction": {
            "type": "string",
            "enum": [d.value for d in SortDirection],
            "description": (
                "Sort direction: desc for 'top/biggest/highest', asc for 'cheapest/lowest'. "
                "Ignored without a sort."
            ),
        },
        "limit": {
            "type": "integer",
            "description": "Result count when the request asks for a specific number (e.g. 'top 10' -> 10). Omit otherwise.",
        },
    }
    if categories:
        properties["categories"] = {
            "type": "array",
            "items": {"type": "string", "enum": list(categories)},
            "description": (
                "Fund-category slugs to match (an OR set). Choose only from the allowed values; "
                "include every category that clearly fits the request."
            ),
        }
    return {
        "name": "build_etf_screen",
        "description": (
            "Record the ETF-screen filters that express the user's request. Set only the fields "
            "the request calls for; leave the rest unset."
        ),
        "input_schema": {
            "type": "object",
            "properties": properties,
            "required": [],
        },
    }


def _tool_payload(message) -> dict | None:
    """Pull the build_etf_screen arguments out of the model's tool call, if any."""
    for block in getattr(message, "content", None) or []:
        if (
            getattr(block, "type", None) == "tool_use"
            and getattr(block, "name", None) == "build_etf_screen"
        ):
            inputs = getattr(block, "input", None)
            if isinstance(inputs, dict):
                return inputs
    return None


def _to_intent(payload: dict | None) -> EtfScreenIntent:
    """Map the validated tool arguments onto the domain ``EtfScreenIntent``.

    Defensive throughout: the forced-tool schema constrains the shape, but a stray or off-schema
    value never raises — an unknown enum is dropped, a non-list field is treated as empty, a
    non-positive limit as unset — so a screen request always yields a usable intent (an all-unset
    one is a neutral browse). The use case re-normalizes on top of this."""
    if not payload:
        return EtfScreenIntent()
    query = payload.get("query")
    text = query.strip() if isinstance(query, str) else ""
    return EtfScreenIntent(
        query=text or None,
        categories=_string_tuple(payload.get("categories")),
        sort=_enum_or_none(EtfSort, payload.get("sort")),
        direction=_enum_or_none(SortDirection, payload.get("direction"))
        or SortDirection.DESC,
        limit=_positive_int_or_none(payload.get("limit")),
    )


def _string_tuple(value) -> tuple[str, ...]:
    """Coerce a list field into a tuple of non-empty, stripped strings (else empty), de-duplicated
    with first-seen order kept."""
    if not isinstance(value, list):
        return ()
    return tuple(
        dict.fromkeys(text for item in value if (text := str(item).strip()))
    )


def _enum_or_none(enum_cls, value):
    """The ``enum_cls`` member for ``value``, or ``None`` when it's missing/unknown."""
    if not isinstance(value, str):
        return None
    try:
        return enum_cls(value)
    except ValueError:
        return None


def _positive_int_or_none(value) -> int | None:
    """A positive int (the requested count), or ``None`` for missing/zero/negative/non-int.
    A bool is rejected — it's an int subclass but never a valid count here."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value > 0 else None
