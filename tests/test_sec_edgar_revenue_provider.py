"""Unit tests for the SEC EDGAR revenue adapter.

No network: the httpx client is swapped for a fake that routes by URL — one
response for the ticker->CIK map, others per us-gaap revenue tag. Verifies CIK
resolution, the quarterly/annual duration split, Q4 derivation from the annual
figure, tag fallback, and HTTP/lookup failures mapping to domain errors.
"""

from datetime import date
from types import SimpleNamespace

import httpx
import pytest

from app.stocks.exceptions import StockDataUnavailable, StockNotFound
from app.stocks.sec_edgar_revenue_provider import SecEdgarRevenueProvider

_REVENUE_TAG = "RevenueFromContractWithCustomerExcludingAssessedTax"
_REVENUE_TAG_INCL = "RevenueFromContractWithCustomerIncludingAssessedTax"

# A ticker->CIK file shaped like SEC's company_tickers.json (an object of rows).
_TICKERS = {"0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."}}


class FakeHttp:
    """Routes get(url) by URL substring to a queued (status, payload).

    A payload that is an Exception is raised (transport error); a dict/list is
    returned as JSON; a str is returned as body text with a failing json().
    Anything unrouted is a 404, mirroring EDGAR's response for an absent tag.
    """

    def __init__(self, routes):
        self._routes = routes  # list of (substring, status_code, payload)
        self.requests: list[str] = []

    def get(self, url, **kwargs):
        self.requests.append(url)
        for sub, status, payload in self._routes:
            if sub in url:
                if isinstance(payload, Exception):
                    raise payload

                def _json(p=payload):
                    if not isinstance(p, (dict, list)):
                        raise ValueError("not json")
                    return p

                text = payload if isinstance(payload, str) else ""
                return SimpleNamespace(status_code=status, text=text, json=_json)
        return SimpleNamespace(status_code=404, text="not found", json=lambda: {})


def provider_with(routes) -> SecEdgarRevenueProvider:
    p = SecEdgarRevenueProvider()
    p._http = FakeHttp(routes)
    return p


def _fact(start, end, val, filed="2025-10-30"):
    return {"start": start, "end": end, "val": val, "filed": filed, "form": "10-Q"}


def _units(*facts) -> dict:
    return {"units": {"USD": list(facts)}}


def test_resolves_cik_and_returns_quarterly_revenue_with_derived_q4():
    # Three reported quarters + a full-year 10-K figure; Q4 is the remainder.
    rows = _units(
        _fact("2024-09-29", "2024-12-28", 120e9),  # Q1
        _fact("2024-12-29", "2025-03-29", 95e9),   # Q2
        _fact("2025-03-30", "2025-06-28", 85e9),   # Q3
        _fact("2024-09-29", "2025-03-29", 215e9),  # YTD (6mo) — must be ignored
        _fact("2024-09-29", "2025-09-27", 390e9, filed="2025-11-01"),  # FY (10-K)
    )
    p = provider_with([("company_tickers", 200, _TICKERS), (_REVENUE_TAG, 200, rows)])
    revenue = p.get_quarterly_revenue("AAPL")
    assert revenue == {
        date(2024, 12, 28): 120e9,
        date(2025, 3, 29): 95e9,
        date(2025, 6, 28): 85e9,
        date(2025, 9, 27): 90e9,  # derived: 390 - (120 + 95 + 85)
    }


def test_resolves_cik_case_insensitively():
    rows = _units(_fact("2024-12-29", "2025-03-29", 95e9))
    p = provider_with([("company_tickers", 200, _TICKERS), (_REVENUE_TAG, 200, rows)])
    assert p.get_quarterly_revenue("aapl") == {date(2025, 3, 29): 95e9}


def test_no_annual_means_no_derived_q4():
    rows = _units(
        _fact("2024-09-29", "2024-12-28", 120e9),
        _fact("2024-12-29", "2025-03-29", 95e9),
    )
    p = provider_with([("company_tickers", 200, _TICKERS), (_REVENUE_TAG, 200, rows)])
    revenue = p.get_quarterly_revenue("AAPL")
    assert revenue == {date(2024, 12, 28): 120e9, date(2025, 3, 29): 95e9}


def test_falls_back_to_next_revenue_tag_on_404():
    # The filer doesn't report the first tag (404); the adapter tries the next.
    rows = _units(_fact("2024-12-29", "2025-03-29", 95e9))
    p = provider_with(
        [
            ("company_tickers", 200, _TICKERS),
            (_REVENUE_TAG, 404, "not found"),
            ("Revenues", 200, rows),
        ]
    )
    assert p.get_quarterly_revenue("AAPL") == {date(2025, 3, 29): 95e9}
    # Both the first (missing) and second (present) tag URLs were tried.
    assert any(_REVENUE_TAG in u for u in p._http.requests)
    assert any("Revenues.json" in u for u in p._http.requests)


def test_merges_revenue_across_tags_when_a_filer_switches():
    # A filer can move revenue from one tag to another over time (Alphabet did);
    # the union must include the newer quarters, not just the first tag's stale data.
    old = _units(_fact("2023-12-30", "2024-03-30", 80e9, filed="2024-04-25"))
    new = _units(
        _fact("2024-12-28", "2025-03-29", 90e9, filed="2025-04-24"),
        _fact("2025-03-30", "2025-06-28", 96e9, filed="2025-07-24"),
    )
    p = provider_with(
        [
            ("company_tickers", 200, _TICKERS),
            (_REVENUE_TAG, 200, old),
            ("Revenues", 200, new),
        ]
    )
    assert p.get_quarterly_revenue("AAPL") == {
        date(2024, 3, 30): 80e9,  # only in the (now-stale) first tag
        date(2025, 3, 29): 90e9,  # only in the second tag
        date(2025, 6, 28): 96e9,
    }


def test_merges_revenue_when_filer_switches_to_including_assessed_tax():
    # Lumentum (LITE) reported revenue under the "Excluding assessed tax" concept
    # through FY2025, then switched to the "Including" sibling starting FY2026.
    # Both variants must be fetched and merged, or the newer quarters vanish
    # behind the now-stale Excluding tag (the symptom that prompted adding it).
    excl = _units(_fact("2025-03-30", "2025-06-28", 481e6, filed="2025-08-19"))
    incl = _units(
        _fact("2025-06-29", "2025-09-27", 534e6, filed="2025-11-05"),
        _fact("2025-09-28", "2025-12-27", 666e6, filed="2026-02-04"),
    )
    p = provider_with(
        [
            ("company_tickers", 200, _TICKERS),
            (_REVENUE_TAG, 200, excl),
            (_REVENUE_TAG_INCL, 200, incl),
        ]
    )
    assert p.get_quarterly_revenue("AAPL") == {
        date(2025, 6, 28): 481e6,  # last quarter under the Excluding tag
        date(2025, 9, 27): 534e6,  # first two under the Including tag
        date(2025, 12, 27): 666e6,
    }
    # The Including-variant URL was actually requested, not just assumed present.
    assert any(_REVENUE_TAG_INCL in u for u in p._http.requests)


def test_overlapping_period_across_tags_takes_the_latest_filing():
    # The same quarter under two tags resolves to the most-recently-filed value.
    first = _units(_fact("2024-12-28", "2025-03-29", 90e9, filed="2025-04-24"))
    restated = _units(_fact("2024-12-28", "2025-03-29", 91e9, filed="2025-08-01"))
    p = provider_with(
        [
            ("company_tickers", 200, _TICKERS),
            (_REVENUE_TAG, 200, first),
            ("Revenues", 200, restated),
        ]
    )
    assert p.get_quarterly_revenue("AAPL") == {date(2025, 3, 29): 91e9}


def test_no_revenue_tag_covered_returns_empty():
    # Every revenue tag 404s -> best-effort empty, not an error.
    p = provider_with([("company_tickers", 200, _TICKERS)])
    assert p.get_quarterly_revenue("AAPL") == {}


def test_unknown_ticker_raises_not_found():
    p = provider_with([("company_tickers", 200, {})])
    with pytest.raises(StockNotFound):
        p.get_quarterly_revenue("ZZZZ")


def test_concept_non_200_raises_unavailable():
    p = provider_with(
        [("company_tickers", 200, _TICKERS), (_REVENUE_TAG, 500, "server error")]
    )
    with pytest.raises(StockDataUnavailable):
        p.get_quarterly_revenue("AAPL")


def test_tickers_fetch_failure_raises_unavailable():
    p = provider_with([("company_tickers", 503, "unavailable")])
    with pytest.raises(StockDataUnavailable):
        p.get_quarterly_revenue("AAPL")


def test_transport_error_raises_unavailable():
    p = provider_with([("company_tickers", 200, httpx.ConnectError("boom"))])
    with pytest.raises(StockDataUnavailable):
        p.get_quarterly_revenue("AAPL")


def test_cik_map_fetched_once_across_symbols():
    rows = _units(_fact("2024-12-29", "2025-03-29", 95e9))
    p = provider_with([("company_tickers", 200, _TICKERS), (_REVENUE_TAG, 200, rows)])
    p.get_quarterly_revenue("AAPL")
    p.get_quarterly_revenue("AAPL")
    assert sum("company_tickers" in u for u in p._http.requests) == 1
