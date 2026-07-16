"""Unit tests for the market-routing price provider.

No vendor: two recording fakes stand in for the US and CA feeds, so this checks only the
dispatch — a Canadian-suffixed symbol goes to the CA feed, everything else to US — across all
four per-symbol ports.
"""

from datetime import datetime, timezone

import pytest

from app.stocks.adapters.market_routing import (
    MarketRoutingPriceProvider,
    is_canadian,
)
from app.stocks.entities import (
    AllTimeHigh,
    CandleSeries,
    Quote,
    Stock,
    StockPerformance,
    Timeframe,
)
from app.stocks.ports import AllTimeHighProvider


@pytest.mark.parametrize(
    "symbol, expected",
    [
        ("SHOP.TO", True),
        ("ABC.V", True),
        ("XYZ.NE", True),
        ("QRS.CN", True),
        ("shop.to", True),  # case-insensitive
        ("AAPL", False),
        ("BRK-B", False),  # a US class share (dash), not a Canadian suffix
        ("", False),
        (None, False),
    ],
)
def test_is_canadian(symbol, expected):
    assert is_canadian(symbol) is expected


class _RecordingFeed:
    """Records every symbol it was asked for and returns a tagged stub, so a test can prove
    which feed a call was routed to."""

    def __init__(self, tag: str) -> None:
        self.tag = tag
        self.calls: list[tuple[str, str]] = []

    def get_quote(self, symbol):
        self.calls.append(("get_quote", symbol))
        return Quote(symbol=f"{symbol}/{self.tag}", price=1.0, previous_close=None, bid=None, ask=None, as_of=None)

    def get_stock(self, symbol):
        self.calls.append(("get_stock", symbol))
        return Stock(
            symbol=f"{symbol}/{self.tag}", name=None, exchange=None, price=1.0, open=None,
            high=None, low=None, previous_close=None, volume=None, bid=None, ask=None, as_of=None,
        )

    def get_performance(self, symbol):
        self.calls.append(("get_performance", symbol))
        return StockPerformance(None, None, None, None, None, None)

    def get_all_time_high(self, symbol):
        self.calls.append(("get_all_time_high", symbol))
        return AllTimeHigh(price=1.0, reached_on=None, since=None)

    def get_candles(self, symbol, timeframe, *, start, end):
        self.calls.append(("get_candles", symbol))
        return CandleSeries(symbol=f"{symbol}/{self.tag}", timeframe=timeframe, candles=())


def _router():
    us, ca = _RecordingFeed("us"), _RecordingFeed("ca")
    return MarketRoutingPriceProvider(us=us, ca=ca), us, ca


_ALL_PORTS = ["get_quote", "get_stock", "get_performance", "get_all_time_high", "get_candles"]


def _call_every_port(router, symbol):
    router.get_quote(symbol)
    router.get_stock(symbol)
    router.get_performance(symbol)
    router.get_all_time_high(symbol)
    router.get_candles(symbol, Timeframe.DAY_1, start=None, end=None)


def test_us_symbol_routes_every_port_to_the_us_feed():
    router, us, ca = _router()
    _call_every_port(router, "AAPL")

    assert [c[0] for c in us.calls] == _ALL_PORTS
    assert ca.calls == []


def test_canadian_symbol_routes_every_port_to_the_ca_feed():
    router, us, ca = _router()
    _call_every_port(router, "SHOP.TO")

    assert [c[0] for c in ca.calls] == _ALL_PORTS
    assert us.calls == []


def test_router_implements_all_time_high_provider():
    # The analysis context reads the injected provider as an AllTimeHighProvider — a router
    # missing it would silently drop the all-time high for US symbols too.
    router, _, _ = _router()
    assert isinstance(router, AllTimeHighProvider)


def test_routes_return_the_chosen_feeds_result():
    router, _, _ = _router()
    assert router.get_quote("SHOP.TO").symbol == "SHOP.TO/ca"
    assert router.get_quote("AAPL").symbol == "AAPL/us"


def test_candles_pass_the_window_through_unchanged():
    router, us, _ = _router()
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    series = router.get_candles("AAPL", Timeframe.WEEK_1, start=start, end=None)
    assert series.timeframe is Timeframe.WEEK_1  # the timeframe is forwarded to the feed
