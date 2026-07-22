import pytest

from app.adapters.yfinance.etf_screener_adapter_impl import (
    EtfScreenerAdapterImpl,
)
from app.domains.etfs.entities import ScreenedEtf
from app.domains.shared.exceptions import StockDataUnavailable

_FLOOR = 1_000_000.0  # the AUM floor the sync passes; the fake pages ignore it but record it


class FakePages:
    def __init__(self, pages=None, *, error=None, payload=None):
        self._pages = pages or []
        self._error = error
        self._payload = payload
        self.calls: list[tuple] = []
        self.min_net_assets: float | None = None  # the AUM floor of the last call

    def __call__(self, *, min_net_assets, offset, size):
        self.calls.append((offset, size))
        self.min_net_assets = min_net_assets
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


def provider(pages=None, **kw) -> EtfScreenerAdapterImpl:
    return EtfScreenerAdapterImpl(screen_page=FakePages(pages, **kw))


def _q(symbol, *, exchange="PCX", net_assets=1e10, expense=0.2, long=None, short=None,
       quote_type=None):
    quote = {
        "symbol": symbol,
        "exchange": exchange,
        "netAssets": net_assets,
        "netExpenseRatio": expense,
        "longName": long,
        "shortName": short,
    }
    if quote_type is not None:  # omit the key entirely by default (the "no tag" path)
        quote["quoteType"] = quote_type
    return quote


def test_maps_a_quote_to_an_entity():
    out = provider(
        [[_q("SPY", exchange="PCX", net_assets=5e11, expense=0.09, long="SPDR S&P 500 ETF Trust")]]
    ).screen(min_net_assets=_FLOOR)
    assert out == (
        ScreenedEtf(
            ticker="SPY",
            name="SPDR S&P 500 ETF Trust",
            exchange="NYSE",
            net_assets=5e11,
            expense_ratio=0.09,
        ),
    )


def test_maps_exchange_codes_to_friendly_names():
    out = provider(
        [
            [
                _q("ARCA", exchange="PCX"),  # NYSE Arca — the ETF venue the stock screen never sees
                _q("NAS", exchange="NMS"),
                _q("BIGBOARD", exchange="NYQ"),
                _q("CBOE", exchange="BTS"),
                _q("HUH", exchange="ZZZ"),  # unknown code -> None, row still kept
            ]
        ]
    ).screen(min_net_assets=_FLOOR)
    assert {e.ticker: e.exchange for e in out} == {
        "ARCA": "NYSE",  # PCX (Arca) folds into its parent NYSE
        "NAS": "NASDAQ",
        "BIGBOARD": "NYSE",
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
    ).screen(min_net_assets=_FLOOR)
    assert {e.ticker: e.name for e in out} == {
        "A": "Long Co",
        "B": "Short Only",
        "C": None,
    }


def test_coerces_missing_or_bad_numbers_to_none():
    out = provider(
        [
            [
                {
                    "symbol": "X",
                    "exchange": "PCX",
                    "netAssets": "1e10",  # a string, not a number
                    "netExpenseRatio": True,  # a bool is not a valid figure
                }
            ]
        ]
    ).screen(min_net_assets=_FLOOR)
    (etf,) = out
    assert (etf.net_assets, etf.expense_ratio) == (None, None)


def test_skips_bad_symbols_and_upcases_the_ticker():
    out = provider(
        [
            [
                _q(""),  # blank symbol
                {"netAssets": 1e10, "exchange": "PCX"},  # missing symbol
                _q("HAS SPACE"),  # space in symbol
                "not-a-dict",  # junk row
                _q("spy"),  # lower-cased -> upper
            ]
        ]
    ).screen(min_net_assets=_FLOOR)
    assert [e.ticker for e in out] == ["SPY"]


def test_dedupes_tickers_across_page_seams():
    pages = FakePages(
        [
            [_q("SPY"), _q("QQQ")],
            [_q("QQQ"), _q("VOO")],  # QQQ repeats at the seam
        ]
    )
    out = EtfScreenerAdapterImpl(screen_page=pages).screen(min_net_assets=_FLOOR)
    assert [e.ticker for e in out] == ["SPY", "QQQ", "VOO"]  # QQQ once


def test_paginates_until_total_reached():
    pages = FakePages(
        [
            [_q(f"E{i}") for i in range(3)],
            [_q("E3")],
        ]
    )
    out = EtfScreenerAdapterImpl(screen_page=pages).screen(min_net_assets=_FLOOR)
    assert [e.ticker for e in out] == ["E0", "E1", "E2", "E3"]
    assert [offset for offset, _ in pages.calls] == [0, 3]  # two pages fetched


def test_stops_on_an_empty_first_page():
    pages = FakePages([[]])
    out = EtfScreenerAdapterImpl(screen_page=pages).screen(min_net_assets=_FLOOR)
    assert out == ()
    assert len(pages.calls) == 1


def test_a_screen_failure_raises_unavailable():
    with pytest.raises(StockDataUnavailable):
        provider(error=RuntimeError("yahoo blocked")).screen(min_net_assets=_FLOOR)


def test_a_non_dict_payload_raises_unavailable():
    with pytest.raises(StockDataUnavailable):
        provider(payload=["not", "a", "dict"]).screen(min_net_assets=_FLOOR)


def test_drops_non_fund_rows_by_quote_type():
    # The broad `region == us` ETF screen can return a stray non-fund row; drop it. An explicit
    # ETF is kept, and so is a row with no quoteType tag at all (absence isn't a non-fund signal).
    out = provider(
        [
            [
                _q("SPY", quote_type="ETF"),
                _q("AAPL", quote_type="EQUITY"),  # a stray equity — dropped
                _q("VOO"),  # no quoteType field — kept (best-effort)
            ]
        ]
    ).screen(min_net_assets=_FLOOR)
    assert [e.ticker for e in out] == ["SPY", "VOO"]


def test_threads_the_aum_floor_to_the_page_fetch():
    # The floor the caller passes reaches the (live) page fetch, where it becomes the query's
    # `fundnetassets >= floor` filter.
    pages = FakePages([[_q("SPY")]])
    EtfScreenerAdapterImpl(screen_page=pages).screen(min_net_assets=2.5e9)
    assert pages.min_net_assets == 2.5e9


# --- The live page fetch's crumb-401 retry (exercises _live_screen_page, which the FakePages seam
#     bypasses — so yf.screen is monkeypatched instead of reaching Yahoo). ------------------------


def test_live_page_retries_a_blocked_screen_then_succeeds(monkeypatch):
    # A first payload with no `total` is the swallowed-401 signature, so the shared crumb-401 retry
    # drops the crumb and re-fetches once; the retry's well-formed page is what's returned.
    import yfinance as yf

    from app.adapters.yfinance.etf_screener_adapter_impl import _live_screen_page

    calls = {"n": 0}

    def fake_screen(query, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return {}  # swallowed 401 — no `total`
        return {"quotes": [{"symbol": "SPY"}], "total": 1}

    monkeypatch.setattr(yf, "screen", fake_screen)
    page = _live_screen_page(min_net_assets=_FLOOR, offset=0, size=250)

    assert calls["n"] == 2  # retried once with a fresh crumb
    assert page == {"quotes": [{"symbol": "SPY"}], "total": 1}


def test_live_page_does_not_retry_a_legit_empty_tail(monkeypatch):
    # A past-the-end page still carries `total` (only `quotes` is empty), so it is NOT mistaken for
    # a block — the pagination terminator isn't wasted on a retry.
    import yfinance as yf

    from app.adapters.yfinance.etf_screener_adapter_impl import _live_screen_page

    calls = {"n": 0}

    def fake_screen(query, **kwargs):
        calls["n"] += 1
        return {"quotes": [], "total": 5000}

    monkeypatch.setattr(yf, "screen", fake_screen)
    page = _live_screen_page(min_net_assets=_FLOOR, offset=999_999, size=250)

    assert calls["n"] == 1  # no retry
    assert page == {"quotes": [], "total": 5000}
