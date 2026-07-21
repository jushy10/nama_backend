import pytest

from app.stocks.adapters.bedrock.etf_screener_query_adapter import (
    BedrockEtfScreenerQueryTranslator,
)
from app.stocks.etfs.entities import EtfSort, SortDirection
from app.stocks.exceptions import StockDataUnavailable


# --- Stub Bedrock client (same shape as the analysis adapters') --------------------------------


class _StubBlock:
    def __init__(self, type, name=None, input=None):
        self.type = type
        self.name = name
        self.input = input


class _StubMessage:
    def __init__(self, content):
        self.content = content


class _StubMessages:
    def __init__(self, message, recorder):
        self._message = message
        self._recorder = recorder

    def create(self, **kwargs):
        self._recorder.append(kwargs)
        return self._message


class _StubClient:
    def __init__(self, message):
        self.calls: list[dict] = []
        self.messages = _StubMessages(message, self.calls)


class _BoomMessages:
    def create(self, **kwargs):
        raise RuntimeError("bedrock exploded")


class _BoomClient:
    messages = _BoomMessages()


def _tool_message(**input_overrides) -> _StubMessage:
    payload = dict(categories=["large_blend"], sort="net_assets", direction="desc")
    payload.update(input_overrides)
    return _StubMessage([_StubBlock("tool_use", "build_etf_screen", payload)])


def _translator(message) -> BedrockEtfScreenerQueryTranslator:
    return BedrockEtfScreenerQueryTranslator(client=_StubClient(message))


# --- Happy path: payload -> EtfScreenIntent ----------------------------------------------------


def test_translates_a_full_payload_into_an_intent():
    translator = _translator(
        _tool_message(
            query="vanguard",
            categories=["large_blend", "large_growth"],
            sort="expense_ratio",
            direction="asc",
            limit=10,
        )
    )
    intent = translator.translate(
        "cheap vanguard index funds",
        categories=["large_blend", "large_growth", "commodities_focused"],
    )
    assert intent.query == "vanguard"
    assert intent.categories == ("large_blend", "large_growth")
    assert (intent.sort, intent.direction) == (EtfSort.EXPENSE_RATIO, SortDirection.ASC)
    assert intent.limit == 10


def test_empty_payload_is_a_neutral_intent():
    intent = _translator(
        _StubMessage([_StubBlock("tool_use", "build_etf_screen", {})])
    ).translate("anything", categories=["large_blend"])
    assert intent.query is None
    assert intent.categories == ()
    assert intent.sort is None
    assert intent.direction is SortDirection.DESC  # the default when unset
    assert intent.limit is None


# --- The forced-tool call is well-formed -------------------------------------------------------


def test_constrains_categories_to_the_supplied_vocabulary():
    client = _StubClient(_tool_message())
    BedrockEtfScreenerQueryTranslator(client=client).translate(
        "index funds", categories=["large_blend", "large_growth"]
    )
    tool = client.calls[0]["tools"][0]
    props = tool["input_schema"]["properties"]
    assert props["categories"]["items"]["enum"] == ["large_blend", "large_growth"]
    # The call forces the build_etf_screen tool.
    assert client.calls[0]["tool_choice"] == {
        "type": "tool",
        "name": "build_etf_screen",
    }


def test_omits_the_category_field_when_the_vocabulary_is_empty():
    # Nothing categorised yet -> no enum to offer, so the field is dropped rather than empty.
    client = _StubClient(_StubMessage([_StubBlock("tool_use", "build_etf_screen", {})]))
    BedrockEtfScreenerQueryTranslator(client=client).translate("x", categories=[])
    props = client.calls[0]["tools"][0]["input_schema"]["properties"]
    assert "categories" not in props
    # The sort fields are always offered (their vocabulary is fixed).
    assert "sort" in props and "direction" in props


def test_drops_unknown_values_and_a_bad_limit():
    intent = _translator(
        _tool_message(
            sort="not_a_column",  # unknown sort -> None
            direction="sideways",  # unknown direction -> default desc
            limit=0,  # non-positive -> None
        )
    ).translate("x", categories=["large_blend"])
    assert intent.sort is None
    assert intent.direction is SortDirection.DESC
    assert intent.limit is None


def test_dedupes_repeated_categories_preserving_order():
    intent = _translator(
        _tool_message(categories=["large_growth", "large_blend", "large_growth"])
    ).translate("x", categories=[])
    assert intent.categories == ("large_growth", "large_blend")


def test_no_tool_call_yields_a_neutral_intent():
    # If the model somehow returns prose instead of the tool call, degrade to a browse.
    intent = _translator(_StubMessage([_StubBlock("text")])).translate(
        "x", categories=[]
    )
    assert intent.categories == ()
    assert intent.sort is None


def test_vendor_failure_becomes_stock_data_unavailable():
    translator = BedrockEtfScreenerQueryTranslator(client=_BoomClient())
    with pytest.raises(StockDataUnavailable):
        translator.translate("x", categories=[])
