"""Unit tests for the Wikipedia index-membership adapter.

No network: the httpx client is swapped for a fake returning canned per-URL page HTML (the same
``_http`` seam the Finnhub adapter test used). Verifies both rosters are parsed + normalized
(``BRK.B`` -> ``BRK-B``); the ticker column is chosen by header (``Symbol`` / ``Ticker``) so the
page's *changes* table is ignored; the larger roster wins when two candidate tables exist; a
single failing page degrades to empty (the other still syncs); and both failing raises the domain
error.
"""

from types import SimpleNamespace

import httpx
import pytest

from app.stocks.adapters import wikipedia_index_membership_adapter as wiki
from app.stocks.adapters.wikipedia_index_membership_adapter import (
    WikipediaIndexMembershipProvider,
)
from app.stocks.exceptions import StockDataUnavailable


def _roster_table(header: str, tickers) -> str:
    """A constituents table whose ticker column is headed ``header`` (``Symbol`` or ``Ticker``)."""
    rows = "".join(f"<tr><td>{t}</td><td>{t} Inc</td><td>Tech</td></tr>" for t in tickers)
    return (
        f"<table><tr><th>{header}</th><th>Security</th><th>Sector</th></tr>{rows}</table>"
    )


# A page's *changes* log: ticker values sit under Added/Removed headers, NOT a Symbol/Ticker one,
# so the adapter must never read them as members.
_CHANGES_TABLE = (
    "<table><tr><th>Date</th><th>Added</th><th>Removed</th><th>Reason</th></tr>"
    "<tr><td>2026-01</td><td>ADDED1</td><td>DROPPED1</td><td>x</td></tr></table>"
)


def _page(*tables: str) -> str:
    return f"<html><body>{''.join(tables)}</body></html>"


class FakeHttpClient:
    """Fake httpx client: returns canned page HTML per requested URL. Per URL you can supply the
    body, a status_code override, or a transport error. Records every URL fetched."""

    def __init__(self, *, pages=None, status=None, errors=()):
        self._pages = pages or {}  # url -> html
        self._status = status or {}  # url -> status_code
        self._errors = set(errors)  # urls raising a transport error
        self.requests: list[str] = []

    def get(self, url):
        self.requests.append(url)
        if url in self._errors:
            raise httpx.ConnectError("boom")
        return SimpleNamespace(
            status_code=self._status.get(url, 200), text=self._pages.get(url, "")
        )


def _provider(http) -> WikipediaIndexMembershipProvider:
    p = WikipediaIndexMembershipProvider()
    p._http = http
    return p


def test_reads_both_rosters_and_normalizes_tickers():
    http = FakeHttpClient(
        pages={
            # The S&P page carries a changes table before the roster — the roster must still win.
            wiki._SP500_URL: _page(
                _CHANGES_TABLE, _roster_table("Symbol", ["AAPL", "MSFT", "BRK.B"])
            ),
            wiki._NASDAQ100_URL: _page(_roster_table("Ticker", ["AAPL", "NVDA"])),
        }
    )
    snap = _provider(http).fetch()
    assert snap.sp500 == frozenset({"AAPL", "MSFT", "BRK-B"})  # dot -> dash
    assert snap.nasdaq100 == frozenset({"AAPL", "NVDA"})


def test_requests_both_index_pages():
    http = FakeHttpClient(
        pages={
            wiki._SP500_URL: _page(_roster_table("Symbol", ["AAPL"])),
            wiki._NASDAQ100_URL: _page(_roster_table("Ticker", ["NVDA"])),
        }
    )
    _provider(http).fetch()
    assert set(http.requests) == {wiki._SP500_URL, wiki._NASDAQ100_URL}


def test_the_changes_table_is_never_read_as_members():
    # Only the roster's Symbol/Ticker column is a member source; the changes log's Added/Removed
    # tickers must not leak in (the bug that sank the earlier scrape attempt).
    http = FakeHttpClient(
        pages={
            wiki._SP500_URL: _page(
                _CHANGES_TABLE, _roster_table("Symbol", ["AAPL", "MSFT"])
            ),
            wiki._NASDAQ100_URL: _page(_roster_table("Ticker", ["NVDA"])),
        }
    )
    snap = _provider(http).fetch()
    assert snap.sp500 == frozenset({"AAPL", "MSFT"})
    assert "ADDED1" not in snap.sp500 and "DROPPED1" not in snap.sp500


def test_the_larger_roster_wins_when_two_ticker_tables_exist():
    # A decoy 1-row table also headed "Ticker" must lose to the real roster (most tickers wins).
    http = FakeHttpClient(
        pages={
            wiki._SP500_URL: _page(_roster_table("Symbol", ["AAPL", "MSFT", "GOOGL"])),
            wiki._NASDAQ100_URL: _page(
                _roster_table("Ticker", ["DECOY"]),
                _roster_table("Ticker", ["NVDA", "AMZN", "META"]),
            ),
        }
    )
    snap = _provider(http).fetch()
    assert snap.nasdaq100 == frozenset({"NVDA", "AMZN", "META"})
    assert "DECOY" not in snap.nasdaq100


def test_a_transport_error_for_one_page_degrades_to_empty():
    http = FakeHttpClient(
        pages={wiki._SP500_URL: _page(_roster_table("Symbol", ["AAPL", "MSFT"]))},
        errors=[wiki._NASDAQ100_URL],
    )
    snap = _provider(http).fetch()
    assert snap.sp500 == frozenset({"AAPL", "MSFT"})
    assert snap.nasdaq100 == frozenset()


def test_a_non_200_for_one_page_degrades_to_empty():
    http = FakeHttpClient(
        pages={wiki._SP500_URL: _page(_roster_table("Symbol", ["AAPL"]))},
        status={wiki._NASDAQ100_URL: 503},
    )
    snap = _provider(http).fetch()
    assert snap.sp500 == frozenset({"AAPL"})
    assert snap.nasdaq100 == frozenset()


def test_an_unparseable_page_is_an_empty_set():
    # A body with no tables makes pandas.read_html raise; that degrades to empty, not a crash.
    http = FakeHttpClient(
        pages={
            wiki._SP500_URL: _page(_roster_table("Symbol", ["AAPL"])),
            wiki._NASDAQ100_URL: "<html><body><p>no tables here</p></body></html>",
        }
    )
    snap = _provider(http).fetch()
    assert snap.sp500 == frozenset({"AAPL"})
    assert snap.nasdaq100 == frozenset()


def test_both_pages_failing_raises_unavailable():
    http = FakeHttpClient(errors=[wiki._SP500_URL, wiki._NASDAQ100_URL])
    with pytest.raises(StockDataUnavailable):
        _provider(http).fetch()
