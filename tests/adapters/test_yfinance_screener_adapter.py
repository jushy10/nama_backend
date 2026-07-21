import pytest

from app.stocks.adapters.yfinance_screener_adapter import YfinanceScreenerProvider
from app.stocks.exceptions import StockDataUnavailable
from app.stocks.universe.entities import ScreenedStock


class FakePages:
    def __init__(self, pages=None, *, error=None, payload=None):
        self._pages = pages or []
        self._error = error
        self._payload = payload
        self.calls: list[tuple] = []

    def __call__(self, *, min_market_cap, offset, size, region="us"):
        self.calls.append((min_market_cap, offset, size, region))
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


def _q(
    symbol,
    *,
    exchange="NMS",
    market_cap=1e10,
    long=None,
    short=None,
    price=None,
    currency=None,
):
    q = {
        "symbol": symbol,
        "exchange": exchange,
        "marketCap": market_cap,
        "longName": long,
        "shortName": short,
    }
    if price is not None:
        q["regularMarketPrice"] = price
    if currency is not None:
        q["currency"] = currency
    return q


def test_maps_a_quote_to_an_entity():
    out = provider(
        [[_q("AAPL", exchange="NMS", market_cap=3.01e12, long="Apple Inc.", price=194.83)]]
    ).screen(min_market_cap=5_000_000_000)
    assert out == (
        ScreenedStock(
            ticker="AAPL",
            name="Apple Inc.",
            exchange="NASDAQ",
            market_cap=3.01e12,
            sector=None,  # yfinance's screen has no sector
            price=194.83,  # the regular-market price, kept for the P/E derivation
            country="US",  # default region
            currency="USD",  # quote carried none -> market default
        ),
    )


def test_keeps_a_positive_price_and_drops_a_bad_one():
    out = provider(
        [
            [
                _q("HASP", market_cap=1e10, price=194.83),
                _q("ZEROP", market_cap=1e10, price=0),  # non-positive -> None
                _q("NEGP", market_cap=1e10, price=-5.0),  # negative -> None
                _q("NOP", market_cap=1e10),  # absent -> None
                _q("STRP", market_cap=1e10, price="abc"),  # non-numeric -> None
            ]
        ]
    ).screen(min_market_cap=5_000_000_000)
    assert {s.ticker: s.price for s in out} == {
        "HASP": 194.83,
        "ZEROP": None,
        "NEGP": None,
        "NOP": None,
        "STRP": None,
    }


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
    assert [c[1] for c in pages.calls] == [0, 3]  # two pages fetched (offset is index 1)


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


def test_us_pass_stamps_us_usd_and_forwards_the_region():
    pages = FakePages([[_q("AAPL", market_cap=3e12)]])
    out = YfinanceScreenerProvider(screen_page=pages).screen(
        min_market_cap=5_000_000_000
    )
    assert (out[0].country, out[0].currency) == ("US", "USD")
    assert pages.calls[0][3] == "us"  # region forwarded to the fetch (index 3)


def test_ca_pass_stamps_ca_cad_and_maps_tsx_exchange_codes():
    # A Canadian screen stamps CA/CAD and maps the TSX/TSXV venue codes to friendly names.
    out = provider(
        [
            [
                _q("SHOP.TO", exchange="TOR", market_cap=1.2e11, long="Shopify"),
                _q("ABC.V", exchange="VAN", market_cap=2e9),
            ]
        ]
    ).screen(min_market_cap=1_000_000_000, region="ca")
    by_ticker = {s.ticker: s for s in out}
    assert (by_ticker["SHOP.TO"].country, by_ticker["SHOP.TO"].currency) == ("CA", "CAD")
    assert by_ticker["SHOP.TO"].exchange == "TSX"
    assert by_ticker["ABC.V"].exchange == "TSXV"


def test_ca_pass_keeps_a_usd_quoted_row_in_its_own_currency():
    # A rare USD-quoted TSX name keeps USD (the quote's own currency wins over the CA default),
    # so its market_cap carries the unit its floor was applied in.
    out = provider(
        [[_q("USDNAME.TO", exchange="TOR", market_cap=2e9, currency="USD")]]
    ).screen(min_market_cap=1_000_000_000, region="ca")
    assert (out[0].country, out[0].currency) == ("CA", "USD")


def test_ca_pass_forwards_the_region_to_the_fetch():
    pages = FakePages([[_q("SHOP.TO", exchange="TOR", market_cap=2e9)]])
    YfinanceScreenerProvider(screen_page=pages).screen(
        min_market_cap=1_000_000_000, region="ca"
    )
    assert pages.calls[0][3] == "ca"  # region is index 3 of the recorded call


def test_an_unknown_region_raises_unavailable():
    with pytest.raises(StockDataUnavailable):
        provider([[_q("AAPL")]]).screen(min_market_cap=1e9, region="zz")


def test_a_screen_failure_raises_unavailable():
    with pytest.raises(StockDataUnavailable):
        provider(error=RuntimeError("yahoo blocked")).screen(
            min_market_cap=5_000_000_000
        )


def test_a_non_dict_payload_raises_unavailable():
    with pytest.raises(StockDataUnavailable):
        provider(payload=["not", "a", "dict"]).screen(min_market_cap=5_000_000_000)


# --- The live page fetch's crumb-401 retry (exercises _live_screen_page, which the FakePages seam
#     bypasses — so yf.screen is monkeypatched instead of reaching Yahoo). ------------------------


def test_live_page_retries_a_blocked_screen_then_succeeds(monkeypatch):
    # A first payload with no `total` is the swallowed-401 signature, so the shared crumb-401 retry
    # drops the crumb and re-fetches once; the retry's well-formed page is what's returned.
    import yfinance as yf

    from app.stocks.adapters.yfinance_screener_adapter import _live_screen_page

    calls = {"n": 0}

    def fake_screen(query, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return {}  # swallowed 401 — no `total`
        return {"quotes": [{"symbol": "AAPL"}], "total": 1}

    monkeypatch.setattr(yf, "screen", fake_screen)
    page = _live_screen_page(min_market_cap=5_000_000_000, offset=0, size=250)

    assert calls["n"] == 2  # retried once with a fresh crumb
    assert page == {"quotes": [{"symbol": "AAPL"}], "total": 1}


def test_live_page_scopes_by_region_for_ca_and_by_exchange_for_us(monkeypatch):
    # The CA pass filters by region==ca; the US pass filters by explicit exchange codes.
    import yfinance as yf

    from app.stocks.adapters.yfinance_screener_adapter import _live_screen_page

    captured = {}

    def fake_screen(query, **kwargs):
        captured["query"] = str(query)
        return {"quotes": [], "total": 0}

    monkeypatch.setattr(yf, "screen", fake_screen)

    _live_screen_page(min_market_cap=1e9, offset=0, size=250, region="ca")
    assert "'region', 'ca'" in captured["query"]
    assert "exchange" not in captured["query"]

    _live_screen_page(min_market_cap=1e9, offset=0, size=250, region="us")
    assert "exchange" in captured["query"]
    assert "region" not in captured["query"]


def test_live_page_does_not_retry_a_legit_empty_tail(monkeypatch):
    # A past-the-end page still carries `total` (only `quotes` is empty), so it is NOT mistaken for
    # a block — the pagination terminator isn't wasted on a retry.
    import yfinance as yf

    from app.stocks.adapters.yfinance_screener_adapter import _live_screen_page

    calls = {"n": 0}

    def fake_screen(query, **kwargs):
        calls["n"] += 1
        return {"quotes": [], "total": 9000}

    monkeypatch.setattr(yf, "screen", fake_screen)
    page = _live_screen_page(min_market_cap=5_000_000_000, offset=999_999, size=250)

    assert calls["n"] == 1  # no retry
    assert page == {"quotes": [], "total": 9000}
