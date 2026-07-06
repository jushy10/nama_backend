"""Unit tests for the yfinance ETF screener adapter (yf.screen predefined 'top_etfs_us').

No network: the per-page screen fetch is swapped for a fake returning canned pages. Verifies
quotes map to ``ScreenedEtf`` (exchange code -> friendly name incl. PCX -> NYSEARCA, name
fallback, numeric coercion), bad rows are dropped, duplicate tickers across page seams are
deduped, pagination stops at ``total``, and yfinance / payload failures become domain errors.
"""

import pytest

from app.stocks.adapters.yfinance_etf_screener_adapter import (
    YfinanceEtfScreenerProvider,
)
from app.stocks.etfs.entities import ScreenedEtf
from app.stocks.exceptions import StockDataUnavailable


class FakePages:
    """A fake screen-page fetch: returns canned pages by offset (or a fixed payload / error),
    and records the (offset, size) of every call."""

    def __init__(self, pages=None, *, error=None, payload=None):
        self._pages = pages or []
        self._error = error
        self._payload = payload
        self.calls: list[tuple] = []

    def __call__(self, *, offset, size):
        self.calls.append((offset, size))
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


def provider(pages=None, **kw) -> YfinanceEtfScreenerProvider:
    return YfinanceEtfScreenerProvider(screen_page=FakePages(pages, **kw))


def _q(symbol, *, exchange="PCX", net_assets=1e10, expense=0.2, long=None, short=None):
    return {
        "symbol": symbol,
        "exchange": exchange,
        "netAssets": net_assets,
        "netExpenseRatio": expense,
        "longName": long,
        "shortName": short,
    }


def test_maps_a_quote_to_an_entity():
    out = provider(
        [[_q("SPY", exchange="PCX", net_assets=5e11, expense=0.09, long="SPDR S&P 500 ETF Trust")]]
    ).screen()
    assert out == (
        ScreenedEtf(
            ticker="SPY",
            name="SPDR S&P 500 ETF Trust",
            exchange="NYSEARCA",
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
                _q("NYSE", exchange="NYQ"),
                _q("CBOE", exchange="BTS"),
                _q("HUH", exchange="ZZZ"),  # unknown code -> None, row still kept
            ]
        ]
    ).screen()
    assert {e.ticker: e.exchange for e in out} == {
        "ARCA": "NYSEARCA",
        "NAS": "NASDAQ",
        "NYSE": "NYSE",
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
    ).screen()
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
    ).screen()
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
    ).screen()
    assert [e.ticker for e in out] == ["SPY"]


def test_dedupes_tickers_across_page_seams():
    pages = FakePages(
        [
            [_q("SPY"), _q("QQQ")],
            [_q("QQQ"), _q("VOO")],  # QQQ repeats at the seam
        ]
    )
    out = YfinanceEtfScreenerProvider(screen_page=pages).screen()
    assert [e.ticker for e in out] == ["SPY", "QQQ", "VOO"]  # QQQ once


def test_paginates_until_total_reached():
    pages = FakePages(
        [
            [_q(f"E{i}") for i in range(3)],
            [_q("E3")],
        ]
    )
    out = YfinanceEtfScreenerProvider(screen_page=pages).screen()
    assert [e.ticker for e in out] == ["E0", "E1", "E2", "E3"]
    assert [offset for offset, _ in pages.calls] == [0, 3]  # two pages fetched


def test_stops_on_an_empty_first_page():
    pages = FakePages([[]])
    out = YfinanceEtfScreenerProvider(screen_page=pages).screen()
    assert out == ()
    assert len(pages.calls) == 1


def test_a_screen_failure_raises_unavailable():
    with pytest.raises(StockDataUnavailable):
        provider(error=RuntimeError("yahoo blocked")).screen()


def test_a_non_dict_payload_raises_unavailable():
    with pytest.raises(StockDataUnavailable):
        provider(payload=["not", "a", "dict"]).screen()
