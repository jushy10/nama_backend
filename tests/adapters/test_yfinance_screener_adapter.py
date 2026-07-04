"""Unit tests for the yfinance screener adapter (yf.screen + EquityQuery).

No network: the per-page screen fetch is swapped for a fake returning canned pages. Verifies
quotes map to ``ScreenedStock`` (exchange code -> friendly name, name fallback, sector always
None), the market-cap floor + bad rows filter, pagination stops at ``total``, and yfinance /
payload failures become domain errors.
"""

import pytest

from app.stocks.adapters.yfinance_screener_adapter import YfinanceScreenerProvider
from app.stocks.exceptions import StockDataUnavailable
from app.stocks.universe.entities import ScreenedStock


class FakePages:
    """A fake screen-page fetch: returns canned pages by offset (or a fixed payload / error),
    and records the (min_market_cap, offset, size) of every call."""

    def __init__(self, pages=None, *, error=None, payload=None):
        self._pages = pages or []
        self._error = error
        self._payload = payload
        self.calls: list[tuple] = []

    def __call__(self, *, min_market_cap, offset, size):
        self.calls.append((min_market_cap, offset, size))
        if self._error is not None:
            raise self._error
        if self._payload is not None:
            return self._payload
        total = sum(len(p) for p in self._pages)
        seen = 0
        for page in self._pages:
            if seen == offset:
                return {"quotes": page, "total": total}
            seen += len(page)
        return {"quotes": [], "total": total}


def provider(pages=None, **kw) -> YfinanceScreenerProvider:
    return YfinanceScreenerProvider(screen_page=FakePages(pages, **kw))


def _q(symbol, *, exchange="NMS", market_cap=1e10, long=None, short=None):
    return {
        "symbol": symbol,
        "exchange": exchange,
        "marketCap": market_cap,
        "longName": long,
        "shortName": short,
    }


def test_maps_a_quote_to_an_entity():
    out = provider(
        [[_q("AAPL", exchange="NMS", market_cap=3.01e12, long="Apple Inc.")]]
    ).screen(min_market_cap=5_000_000_000)
    assert out == (
        ScreenedStock(
            ticker="AAPL",
            name="Apple Inc.",
            exchange="NASDAQ",
            market_cap=3.01e12,
            sector=None,  # yfinance's screen has no sector
        ),
    )


def test_maps_exchange_codes_to_friendly_names():
    out = provider(
        [
            [
                _q("AAPL", exchange="NMS"),
                _q("WAT", exchange="NGM"),
                _q("CAP", exchange="NCM"),
                _q("BRK", exchange="NYQ"),
                _q("XYZ", exchange="ASE"),
                _q("CBOE", exchange="BTS"),
                _q("HUH", exchange="ZZZ"),  # unknown code -> None, row still kept
            ]
        ]
    ).screen(min_market_cap=5_000_000_000)
    assert {s.ticker: s.exchange for s in out} == {
        "AAPL": "NASDAQ",
        "WAT": "NASDAQ",
        "CAP": "NASDAQ",
        "BRK": "NYSE",
        "XYZ": "AMEX",
        "CBOE": "BATS",
        "HUH": None,
    }


def test_name_prefers_long_then_short_then_none():
    out = provider(
        [
            [
                _q("A", long="Long Co", short="Short Co"),
                _q("B", long=None, short="Short Only"),
                _q("C", long=None, short=None),
            ]
        ]
    ).screen(min_market_cap=5_000_000_000)
    assert {s.ticker: s.name for s in out} == {
        "A": "Long Co",
        "B": "Short Only",
        "C": None,
    }


def test_filters_below_floor_and_bad_market_cap():
    out = provider(
        [
            [
                _q("BIG", market_cap=6e9),
                _q("SMALL", market_cap=4e9),  # below the floor
                _q("NOMC", market_cap=None),  # missing
                _q("STR", market_cap="1e10"),  # a string, not a number
            ]
        ]
    ).screen(min_market_cap=5_000_000_000)
    assert [s.ticker for s in out] == ["BIG"]
    assert out[0].market_cap == 6e9


def test_skips_bad_symbols_and_upcases_the_ticker():
    out = provider(
        [
            [
                _q("", market_cap=1e10),  # blank symbol
                {"marketCap": 1e10, "exchange": "NMS"},  # missing symbol
                _q("HAS SPACE", market_cap=1e10),  # space in symbol
                "not-a-dict",  # junk row
                _q("aapl", market_cap=1e10),  # lower-cased -> upper
            ]
        ]
    ).screen(min_market_cap=5_000_000_000)
    assert [s.ticker for s in out] == ["AAPL"]


def test_paginates_until_total_reached():
    pages = FakePages(
        [
            [_q(f"T{i}", market_cap=1e10) for i in range(3)],
            [_q("T3", market_cap=1e10)],
        ]
    )
    out = YfinanceScreenerProvider(screen_page=pages).screen(
        min_market_cap=5_000_000_000
    )
    assert [s.ticker for s in out] == ["T0", "T1", "T2", "T3"]
    assert [offset for _, offset, _ in pages.calls] == [0, 3]  # two pages fetched


def test_stops_on_an_empty_first_page():
    pages = FakePages([[]])
    out = YfinanceScreenerProvider(screen_page=pages).screen(
        min_market_cap=5_000_000_000
    )
    assert out == ()
    assert len(pages.calls) == 1


def test_passes_the_floor_through_to_the_fetch():
    pages = FakePages([[_q("AAPL")]])
    YfinanceScreenerProvider(screen_page=pages).screen(min_market_cap=5_000_000_000)
    assert pages.calls[0][0] == 5_000_000_000


def test_a_screen_failure_raises_unavailable():
    with pytest.raises(StockDataUnavailable):
        provider(error=RuntimeError("yahoo blocked")).screen(
            min_market_cap=5_000_000_000
        )


def test_a_non_dict_payload_raises_unavailable():
    with pytest.raises(StockDataUnavailable):
        provider(payload=["not", "a", "dict"]).screen(min_market_cap=5_000_000_000)
