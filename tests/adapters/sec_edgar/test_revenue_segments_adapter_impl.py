from datetime import date
from types import SimpleNamespace

import httpx
import pytest

from app.stocks.adapters.sec_edgar import revenue_segments_adapter_impl as sec
from app.stocks.adapters.sec_edgar.revenue_segments_adapter_impl import (
    RevenueSegmentsAdapterImpl,
    _parse_revenue_segments,
)
from app.stocks.exceptions import StockDataUnavailable, StockNotFound
from app.stocks.company.revenue_segments.entities import SegmentAxis


# ── a hand-built XBRL instance ────────────────────────────────────────────────────────────
#
# FY2024 (+ one FY2023 row) with: the consolidated total (no member — must be ignored), two
# business segments (single-axis), two products nested under Segment A (product + segment axes),
# one product (Z) split across both segments (must be summed), two geographies (single-axis), a
# duplicate `Revenues` tag on Segment A (must lose to the more-specific concept), and a quarterly
# Segment A fact (must be excluded as non-annual).


def _ctx(cid: str, *, start: str, end: str, members=()) -> str:
    segment = ""
    if members:
        rows = "".join(
            f'<xbrldi:explicitMember dimension="{dim}">{mem}</xbrldi:explicitMember>'
            for dim, mem in members
        )
        segment = f"<xbrli:entity><xbrli:identifier>CIK</xbrli:identifier>" \
                  f"<xbrli:segment>{rows}</xbrli:segment></xbrli:entity>"
    else:
        segment = "<xbrli:entity><xbrli:identifier>CIK</xbrli:identifier></xbrli:entity>"
    return (
        f'<xbrli:context id="{cid}">{segment}'
        f"<xbrli:period><xbrli:startDate>{start}</xbrli:startDate>"
        f"<xbrli:endDate>{end}</xbrli:endDate></xbrli:period></xbrli:context>"
    )


_BIZ = "us-gaap:StatementBusinessSegmentsAxis"
_PROD = "us-gaap:ProductOrServiceAxis"
_GEO = "us-gaap:StatementGeographicalAxis"
_REV = "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax"


def _fact(tag: str, ctx: str, value: int) -> str:
    return f'<{tag} contextRef="{ctx}" unitRef="usd" decimals="-6">{value}</{tag}>'


_INSTANCE = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance" '
    'xmlns:xbrldi="http://xbrl.org/2006/xbrldi" '
    'xmlns:us-gaap="http://fasb.org/us-gaap/2024" '
    'xmlns:co="http://example.com/co">'
    # contexts
    + _ctx("fy24", start="2024-01-01", end="2024-12-31")
    + _ctx("fy24_segA", start="2024-01-01", end="2024-12-31", members=[(_BIZ, "co:SegmentAMember")])
    + _ctx("fy24_segB", start="2024-01-01", end="2024-12-31", members=[(_BIZ, "co:SegmentBMember")])
    + _ctx("fy24_prodX", start="2024-01-01", end="2024-12-31",
           members=[(_PROD, "co:ProductXMember"), (_BIZ, "co:SegmentAMember")])
    + _ctx("fy24_prodY", start="2024-01-01", end="2024-12-31",
           members=[(_PROD, "co:ProductYMember"), (_BIZ, "co:SegmentAMember")])
    + _ctx("fy24_prodZ_A", start="2024-01-01", end="2024-12-31",
           members=[(_PROD, "co:ProductZMember"), (_BIZ, "co:SegmentAMember")])
    + _ctx("fy24_prodZ_B", start="2024-01-01", end="2024-12-31",
           members=[(_PROD, "co:ProductZMember"), (_BIZ, "co:SegmentBMember")])
    + _ctx("fy24_geoUS", start="2024-01-01", end="2024-12-31", members=[(_GEO, "co:USMember")])
    + _ctx("fy24_geoIntl", start="2024-01-01", end="2024-12-31", members=[(_GEO, "co:IntlMember")])
    + _ctx("fy24_q4_segA", start="2024-10-01", end="2024-12-31", members=[(_BIZ, "co:SegmentAMember")])
    + _ctx("fy23_segA", start="2023-01-01", end="2023-12-31", members=[(_BIZ, "co:SegmentAMember")])
    # facts
    + _fact(_REV, "fy24", 200_000_000)  # consolidated total → ignored (no member)
    + _fact(_REV, "fy24_segA", 120_000_000)
    + _fact("us-gaap:Revenues", "fy24_segA", 999)  # duplicate, less-specific concept → loses
    + _fact(_REV, "fy24_segB", 80_000_000)
    + _fact(_REV, "fy24_prodX", 70_000_000)
    + _fact(_REV, "fy24_prodY", 50_000_000)
    + _fact(_REV, "fy24_prodZ_A", 10_000_000)
    + _fact(_REV, "fy24_prodZ_B", 20_000_000)
    + _fact(_REV, "fy24_geoUS", 150_000_000)
    + _fact(_REV, "fy24_geoIntl", 50_000_000)
    + _fact(_REV, "fy24_q4_segA", 31_000_000)  # quarterly → excluded (unique value)
    + _fact(_REV, "fy23_segA", 100_000_000)
    + "</xbrli:xbrl>"
).encode()


def _by(segments, axis):
    return {(s.fiscal_year, s.member): s.value for s in segments if s.axis == axis}


def test_parses_business_segments_single_axis():
    segs = _parse_revenue_segments(_INSTANCE)
    biz = _by(segs, SegmentAxis.BUSINESS)
    assert biz[(2024, "SegmentAMember")] == 120_000_000  # not 999 — specific concept wins
    assert biz[(2024, "SegmentBMember")] == 80_000_000
    assert biz[(2023, "SegmentAMember")] == 100_000_000  # prior year kept


def test_parses_products_nested_under_the_segment_axis():
    # The whole point: products carry BOTH the product axis and the segment axis, so a
    # single-axis filter would miss them entirely.
    prod = _by(_parse_revenue_segments(_INSTANCE), SegmentAxis.PRODUCT)
    assert prod[(2024, "ProductXMember")] == 70_000_000
    assert prod[(2024, "ProductYMember")] == 50_000_000


def test_sums_a_product_that_spans_segments():
    # Product Z is tagged in both Segment A (10M) and Segment B (20M); its total is the sum.
    prod = _by(_parse_revenue_segments(_INSTANCE), SegmentAxis.PRODUCT)
    assert prod[(2024, "ProductZMember")] == 30_000_000


def test_parses_geography_single_axis():
    geo = _by(_parse_revenue_segments(_INSTANCE), SegmentAxis.GEOGRAPHY)
    assert geo == {(2024, "USMember"): 150_000_000, (2024, "IntlMember"): 50_000_000}


def test_excludes_the_consolidated_total_and_quarterly_facts():
    segs = _parse_revenue_segments(_INSTANCE)
    # No segment carries the 200M consolidated total or the 31M quarterly figure.
    values = {s.value for s in segs}
    assert 200_000_000 not in values  # consolidated total (no member)
    assert 31_000_000 not in values  # the sub-annual (quarterly) Segment A fact
    # And Segment A's annual total is exactly its full-year figure, not inflated by the quarter.
    seg_a = _by(segs, SegmentAxis.BUSINESS)[(2024, "SegmentAMember")]
    assert seg_a == 120_000_000


def test_business_axis_excludes_segment_nested_product_facts():
    # A product×segment fact must not leak into the business breakdown (it'd double-count).
    biz = _by(_parse_revenue_segments(_INSTANCE), SegmentAxis.BUSINESS)
    assert set(m for (_y, m) in biz) == {"SegmentAMember", "SegmentBMember"}


def test_period_end_and_year_come_from_the_context():
    seg_a = next(
        s
        for s in _parse_revenue_segments(_INSTANCE)
        if s.axis is SegmentAxis.BUSINESS and s.fiscal_year == 2024 and s.member == "SegmentAMember"
    )
    assert seg_a.period_end == date(2024, 12, 31)


def test_bad_xml_is_empty_not_a_crash():
    assert _parse_revenue_segments(b"<not-xbrl>") == ()


# ── the SEC walk (fake httpx) ─────────────────────────────────────────────────────────────

_TICKERS = {"0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."}}
_SUBMISSIONS = {
    "filings": {
        "recent": {
            "form": ["8-K", "10-K", "10-Q"],
            "accessionNumber": ["0000320193-25-000001", "0000320193-24-000123", "0000320193-24-000050"],
            "primaryDocument": ["a8k.htm", "aapl-20240928.htm", "aapl-q3.htm"],
            "reportDate": ["2025-01-30", "2024-09-28", "2024-06-29"],
        }
    }
}
_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK0000320193.json"
_INSTANCE_URL = (
    "https://www.sec.gov/Archives/edgar/data/320193/000032019324000123/aapl-20240928_htm.xml"
)


class _Resp:
    def __init__(self, status_code=200, *, json_data=None, content=b""):
        self.status_code = status_code
        self._json = json_data
        self.content = content

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json


class FakeHttp:
    def __init__(self, responses, *, errors=()):
        self._responses = responses
        self._errors = set(errors)
        self.requests: list[str] = []

    def get(self, url):
        self.requests.append(url)
        if url in self._errors:
            raise httpx.ConnectError("boom")
        if url not in self._responses:
            return _Resp(404)
        return self._responses[url]


def _provider(http) -> RevenueSegmentsAdapterImpl:
    p = RevenueSegmentsAdapterImpl()
    p._http = http
    return p


def _happy_responses():
    return {
        _TICKERS_URL: _Resp(json_data=_TICKERS),
        _SUBMISSIONS_URL: _Resp(json_data=_SUBMISSIONS),
        _INSTANCE_URL: _Resp(content=_INSTANCE),
    }


def test_walks_ticker_to_cik_to_10k_to_instance():
    http = FakeHttp(_happy_responses())
    seg = _provider(http).get_revenue_segments("AAPL")
    assert not seg.is_empty
    assert seg.fiscal_years == (2024, 2023)
    # The three-step walk hit exactly the expected URLs (derived instance, no index fallback).
    assert http.requests == [_TICKERS_URL, _SUBMISSIONS_URL, _INSTANCE_URL]


def test_ticker_map_is_cached_across_calls():
    http = FakeHttp(_happy_responses())
    provider = _provider(http)
    provider.get_revenue_segments("AAPL")
    provider.get_revenue_segments("AAPL")
    assert http.requests.count(_TICKERS_URL) == 1  # fetched once, reused


def test_unmapped_ticker_is_stock_not_found():
    http = FakeHttp({_TICKERS_URL: _Resp(json_data=_TICKERS)})
    with pytest.raises(StockNotFound):
        _provider(http).get_revenue_segments("ZZZZ")


def test_no_10k_yields_an_empty_segmentation():
    subs = {"filings": {"recent": {"form": ["10-Q"], "accessionNumber": ["x"], "primaryDocument": ["y.htm"]}}}
    http = FakeHttp({_TICKERS_URL: _Resp(json_data=_TICKERS), _SUBMISSIONS_URL: _Resp(json_data=subs)})
    seg = _provider(http).get_revenue_segments("AAPL")
    assert seg.is_empty  # covered filer, no annual report to parse (e.g. a 20-F filer)


def test_transport_failure_is_stock_data_unavailable():
    http = FakeHttp(_happy_responses(), errors={_SUBMISSIONS_URL})
    with pytest.raises(StockDataUnavailable):
        _provider(http).get_revenue_segments("AAPL")


def test_non_200_on_a_required_read_is_stock_data_unavailable():
    responses = _happy_responses()
    responses[_SUBMISSIONS_URL] = _Resp(500)
    with pytest.raises(StockDataUnavailable):
        _provider(FakeHttp(responses)).get_revenue_segments("AAPL")


def test_falls_back_to_index_json_when_the_derived_instance_is_404():
    # The derived `_htm.xml` name isn't found (404), so the adapter consults the filing's
    # index.json to locate the real instance file.
    base = "https://www.sec.gov/Archives/edgar/data/320193/000032019324000123"
    real_instance = f"{base}/aapl_10k_htm.xml"
    responses = {
        _TICKERS_URL: _Resp(json_data=_TICKERS),
        _SUBMISSIONS_URL: _Resp(json_data=_SUBMISSIONS),
        _INSTANCE_URL: _Resp(404),  # derived name misses
        f"{base}/index.json": _Resp(
            json_data={"directory": {"item": [{"name": "aapl_10k_htm.xml"}, {"name": "x.jpg"}]}}
        ),
        real_instance: _Resp(content=_INSTANCE),
    }
    http = FakeHttp(responses)
    seg = _provider(http).get_revenue_segments("AAPL")
    assert not seg.is_empty
    assert f"{base}/index.json" in http.requests  # consulted the directory
    assert real_instance in http.requests  # then fetched the located instance
