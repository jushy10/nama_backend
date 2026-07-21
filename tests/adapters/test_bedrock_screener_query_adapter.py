import pytest

from app.stocks.adapters.bedrock.screener_query_adapter import (
    BedrockScreenerQueryTranslator,
)
from app.stocks.exceptions import StockDataUnavailable
from app.stocks.catalog.universe.entities import MarketCapTier, SortDirection, StockSort


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
    payload = dict(
        sectors=["technology"],
        market_cap_tiers=["mega"],
        sort="market_cap",
        direction="desc",
    )
    payload.update(input_overrides)
    return _StubMessage([_StubBlock("tool_use", "build_screen", payload)])


def _translator(message) -> BedrockScreenerQueryTranslator:
    return BedrockScreenerQueryTranslator(client=_StubClient(message))


# --- Happy path: payload -> ScreenIntent -------------------------------------------------------


def test_translates_a_full_payload_into_an_intent():
    translator = _translator(
        _tool_message(
            query="acme",
            industries=["semiconductors"],
            in_sp500=True,
            in_nasdaq100=False,
            limit=10,
        )
    )
    intent = translator.translate(
        "mega cap tech stocks",
        sectors=["technology", "energy"],
        industries=["semiconductors"],
    )
    assert intent.query == "acme"
    assert intent.sectors == ("technology",)
    assert intent.industries == ("semiconductors",)
    assert intent.market_cap_tiers == (MarketCapTier.MEGA,)
    assert (intent.sort, intent.direction) == (StockSort.MARKET_CAP, SortDirection.DESC)
    assert (intent.in_sp500, intent.in_nasdaq100) == (True, False)
    assert intent.limit == 10


def test_empty_payload_is_a_neutral_intent():
    intent = _translator(
        _StubMessage([_StubBlock("tool_use", "build_screen", {})])
    ).translate("anything", sectors=["technology"], industries=[])
    assert intent.query is None
    assert intent.sectors == ()
    assert intent.industries == ()
    assert intent.market_cap_tiers == ()
    assert intent.sort is None
    assert intent.direction is SortDirection.DESC  # the default when unset
    assert intent.limit is None


# --- The forced-tool call is well-formed -------------------------------------------------------


def test_constrains_sector_and_industry_to_the_supplied_vocabulary():
    client = _StubClient(_tool_message())
    BedrockScreenerQueryTranslator(client=client).translate(
        "tech", sectors=["technology", "energy"], industries=["semiconductors"]
    )
    tool = client.calls[0]["tools"][0]
    props = tool["input_schema"]["properties"]
    assert props["sectors"]["items"]["enum"] == ["technology", "energy"]
    assert props["industries"]["items"]["enum"] == ["semiconductors"]
    # The call forces the build_screen tool.
    assert client.calls[0]["tool_choice"] == {"type": "tool", "name": "build_screen"}


def test_omits_the_sector_field_when_the_vocabulary_is_empty():
    # Nothing classified yet -> no enum to offer, so the field is dropped rather than empty.
    client = _StubClient(_StubMessage([_StubBlock("tool_use", "build_screen", {})]))
    BedrockScreenerQueryTranslator(client=client).translate(
        "tech", sectors=[], industries=[]
    )
    props = client.calls[0]["tools"][0]["input_schema"]["properties"]
    assert "sectors" not in props
    assert "industries" not in props
    # The size/sort fields are always offered (their vocabulary is fixed).
    assert "market_cap_tiers" in props and "sort" in props


def test_drops_unknown_enum_values_and_a_bad_limit():
    intent = _translator(
        _tool_message(
            market_cap_tiers=["mega", "gigantic"],  # unknown tier dropped
            sort="not_a_column",  # unknown sort -> None
            direction="sideways",  # unknown direction -> default desc
            limit=0,  # non-positive -> None
        )
    ).translate("x", sectors=["technology"], industries=[])
    assert intent.market_cap_tiers == (MarketCapTier.MEGA,)
    assert intent.sort is None
    assert intent.direction is SortDirection.DESC
    assert intent.limit is None


def test_dedupes_repeated_tiers_preserving_order():
    intent = _translator(
        _tool_message(market_cap_tiers=["large", "mid", "large"])
    ).translate("x", sectors=[], industries=[])
    assert intent.market_cap_tiers == (MarketCapTier.LARGE, MarketCapTier.MID)


def test_no_tool_call_yields_a_neutral_intent():
    # If the model somehow returns prose instead of the tool call, degrade to a browse.
    intent = _translator(_StubMessage([_StubBlock("text")])).translate(
        "x", sectors=[], industries=[]
    )
    assert intent.sectors == ()
    assert intent.sort is None


def test_vendor_failure_becomes_stock_data_unavailable():
    translator = BedrockScreenerQueryTranslator(client=_BoomClient())
    with pytest.raises(StockDataUnavailable):
        translator.translate("x", sectors=[], industries=[])
