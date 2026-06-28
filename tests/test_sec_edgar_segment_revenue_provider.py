"""Unit tests for the SEC EDGAR segment-revenue adapter.

No network: the httpx client is swapped for a fake that routes by URL — one
response for the ticker->CIK map, one for the submissions list, and one per
filing document (a hand-built inline-XBRL snippet). Verifies CIK resolution, the
standalone-quarter filter, the segment/product classification (including the
reconciliation-axis-allowed and double-cut/geography-excluded cases), latest
filing wins, lenient per-filing fetch, and failure -> domain error mapping.
"""

from datetime import date
from types import SimpleNamespace

import httpx
import pytest

from app.stocks.entities import RevenueBreakdown, RevenueComponent
from app.stocks.exceptions import StockDataUnavailable, StockNotFound
from app.stocks.sec_edgar_segment_revenue_provider import (
    SecEdgarSegmentRevenueProvider,
    _humanize_member,
)

_REVENUE_TAG = "RevenueFromContractWithCustomerExcludingAssessedTax"
_TICKERS = {"0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."}}

_SEG_AXIS = "us-gaap:StatementBusinessSegmentsAxis"
_PROD_AXIS = "srt:ProductOrServiceAxis"
_GEO_AXIS = "us-gaap:StatementGeographicalAxis"
_CONSOL_AXIS = "us-gaap:ConsolidationItemsAxis"
_OPERATING = "us-gaap:OperatingSegmentsMember"


class FakeHttp:
    """Routes get(url) by URL substring to a queued (status, payload).

    An Exception payload is raised (transport error); a dict is returned as JSON
    (for the ticker map / submissions); a str is returned as body text with a
    failing json() (for filing documents). Anything unrouted is a 404.
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


def provider_with(routes) -> SecEdgarSegmentRevenueProvider:
    p = SecEdgarSegmentRevenueProvider()
    p._http = FakeHttp(routes)
    return p


def _submissions(*docs, form="10-Q") -> dict:
    """A submissions payload listing the given (document, filing-date) rows."""
    return {
        "filings": {
            "recent": {
                "form": [form] * len(docs),
                "accessionNumber": [f"0000320193-26-{i:06d}" for i in range(len(docs))],
                "primaryDocument": [doc for doc, _ in docs],
                "filingDate": [filed for _, filed in docs],
            }
        }
    }


def _ctx(cid, start, end, members=()) -> str:
    dims = "".join(
        f'<xbrldi:explicitMember dimension="{axis}">{member}</xbrldi:explicitMember>'
        for axis, member in members
    )
    segment = f"<xbrli:segment>{dims}</xbrli:segment>" if dims else ""
    return (
        f'<xbrli:context id="{cid}"><xbrli:entity>'
        f"<xbrli:identifier>0000320193</xbrli:identifier>{segment}</xbrli:entity>"
        f"<xbrli:period><xbrli:startDate>{start}</xbrli:startDate>"
        f"<xbrli:endDate>{end}</xbrli:endDate></xbrli:period></xbrli:context>"
    )


def _fact(cid, displayed, tag=_REVENUE_TAG, scale="6") -> str:
    return (
        f'<ix:nonFraction name="us-gaap:{tag}" contextRef="{cid}" unitRef="usd" '
        f'scale="{scale}" decimals="-6" format="ixt:num-dot-decimal">{displayed}'
        f"</ix:nonFraction>"
    )


# A filing whose latest quarter (ending 2026-03-31) is disclosed both by segment
# and by product, alongside facts that must be excluded.
_QUARTER = ("2026-01-01", "2026-03-31")
_FILING = "<html><body>" + "".join(
    [
        _ctx("c_total", *_QUARTER),  # consolidated total, no members -> excluded
        _ctx("c_na", *_QUARTER, [(_SEG_AXIS, "aapl:NorthAmericaSegmentMember")]),
        # International also carries the (neutral) reconciliation axis -> still a segment
        _ctx(
            "c_intl",
            *_QUARTER,
            [(_CONSOL_AXIS, _OPERATING), (_SEG_AXIS, "aapl:InternationalSegmentMember")],
        ),
        _ctx("c_aws", *_QUARTER, [(_SEG_AXIS, "aapl:AWSSegmentMember")]),
        _ctx("c_online", *_QUARTER, [(_PROD_AXIS, "aapl:OnlineStoresMember")]),
        _ctx("c_ads", *_QUARTER, [(_PROD_AXIS, "aapl:AdvertisingServicesMember")]),
        # year-to-date (272d) segment fact -> excluded by the quarter band
        _ctx("c_ytd", "2025-07-03", "2026-03-31", [(_SEG_AXIS, "aapl:AWSSegmentMember")]),
        # segment crossed with geography -> a sub-cell, excluded
        _ctx(
            "c_geo",
            *_QUARTER,
            [(_SEG_AXIS, "aapl:InternationalSegmentMember"), (_GEO_AXIS, "country:US")],
        ),
        # corporate reconciliation bucket (no segment member) -> excluded
        _ctx("c_corp", *_QUARTER, [(_CONSOL_AXIS, "us-gaap:CorporateNonSegmentMember")]),
        _fact("c_total", "200,000"),
        _fact("c_na", "100,000"),
        _fact("c_intl", "35,000"),
        _fact("c_aws", "30,000"),
        _fact("c_online", "60,000"),
        _fact("c_ads", "40,000"),
        _fact("c_ytd", "250,000"),
        _fact("c_geo", "99,000"),
        _fact("c_corp", "5,000"),
    ]
) + "</body></html>"


def test_parses_segment_and_product_breakdown_for_the_quarter():
    p = provider_with(
        [
            ("company_tickers", 200, _TICKERS),
            ("submissions", 200, _submissions(("aapl-20260331.htm", "2026-05-01"))),
            ("aapl-20260331.htm", 200, _FILING),
        ]
    )
    result = p.get_quarterly_segment_revenue("AAPL")
    assert result == {
        date(2026, 3, 31): RevenueBreakdown(
            # ordered largest first; consolidated/ytd/geo/corporate all excluded
            by_segment=(
                RevenueComponent("North America", 100e9),
                RevenueComponent("International", 35e9),
                RevenueComponent("AWS", 30e9),
            ),
            by_product=(
                RevenueComponent("Online Stores", 60e9),
                RevenueComponent("Advertising Services", 40e9),
            ),
        )
    }


def test_resolves_cik_case_insensitively():
    p = provider_with(
        [
            ("company_tickers", 200, _TICKERS),
            ("submissions", 200, _submissions(("aapl-20260331.htm", "2026-05-01"))),
            ("aapl-20260331.htm", 200, _FILING),
        ]
    )
    assert p.get_quarterly_segment_revenue("aapl")  # lowercase resolves to CIK 320193


def test_latest_filing_wins_for_an_overlapping_period():
    # Two filings disclose the same quarter; the later-filed value must win.
    restated = "<html>" + _ctx(
        "c1", *_QUARTER, [(_SEG_AXIS, "aapl:AWSSegmentMember")]
    ) + _fact("c1", "31,000") + "</html>"
    original = "<html>" + _ctx(
        "c1", *_QUARTER, [(_SEG_AXIS, "aapl:AWSSegmentMember")]
    ) + _fact("c1", "30,000") + "</html>"
    p = provider_with(
        [
            ("company_tickers", 200, _TICKERS),
            (
                "submissions",
                200,
                _submissions(  # newest first
                    ("restated.htm", "2026-08-01"), ("original.htm", "2026-05-01")
                ),
            ),
            ("restated.htm", 200, restated),
            ("original.htm", 200, original),
        ]
    )
    result = p.get_quarterly_segment_revenue("AAPL")
    assert result[date(2026, 3, 31)].by_segment == (RevenueComponent("AWS", 31e9),)


def test_skips_an_unreadable_filing_and_keeps_the_rest():
    good = "<html>" + _ctx(
        "c1", *_QUARTER, [(_SEG_AXIS, "aapl:AWSSegmentMember")]
    ) + _fact("c1", "30,000") + "</html>"
    p = provider_with(
        [
            ("company_tickers", 200, _TICKERS),
            (
                "submissions",
                200,
                _submissions(("missing.htm", "2026-08-01"), ("good.htm", "2026-05-01")),
            ),
            ("missing.htm", 404, "gone"),  # first filing unreadable -> skipped
            ("good.htm", 200, good),
        ]
    )
    result = p.get_quarterly_segment_revenue("AAPL")
    assert result[date(2026, 3, 31)].by_segment == (RevenueComponent("AWS", 30e9),)


def test_no_periodic_filings_returns_empty():
    p = provider_with(
        [
            ("company_tickers", 200, _TICKERS),
            ("submissions", 200, _submissions(("aapl-8k.htm", "2026-05-01"), form="8-K")),
        ]
    )
    assert p.get_quarterly_segment_revenue("AAPL") == {}


def test_filing_with_no_disaggregation_returns_empty():
    only_total = "<html>" + _ctx("c_total", *_QUARTER) + _fact("c_total", "200,000") + "</html>"
    p = provider_with(
        [
            ("company_tickers", 200, _TICKERS),
            ("submissions", 200, _submissions(("aapl-20260331.htm", "2026-05-01"))),
            ("aapl-20260331.htm", 200, only_total),
        ]
    )
    assert p.get_quarterly_segment_revenue("AAPL") == {}


def test_drops_coarse_product_service_split_when_detail_is_present():
    # Amazon tags both the us-gaap goods/services split and its specific lines on
    # the same axis; the coarse pair is redundant (each sums to the total), so the
    # detailed lines win and by_product stays a single clean partition.
    html = "<html>" + "".join(
        [
            _ctx("c_prod", *_QUARTER, [(_PROD_AXIS, "us-gaap:ProductMember")]),
            _ctx("c_svc", *_QUARTER, [(_PROD_AXIS, "us-gaap:ServiceMember")]),
            _ctx("c_online", *_QUARTER, [(_PROD_AXIS, "aapl:OnlineStoresMember")]),
            _ctx("c_ads", *_QUARTER, [(_PROD_AXIS, "aapl:AdvertisingServicesMember")]),
            _fact("c_prod", "71,000"),
            _fact("c_svc", "110,000"),
            _fact("c_online", "120,000"),
            _fact("c_ads", "61,000"),
        ]
    ) + "</html>"
    p = provider_with(
        [
            ("company_tickers", 200, _TICKERS),
            ("submissions", 200, _submissions(("f.htm", "2026-05-01"))),
            ("f.htm", 200, html),
        ]
    )
    bd = p.get_quarterly_segment_revenue("AAPL")[date(2026, 3, 31)]
    assert bd.by_product == (
        RevenueComponent("Online Stores", 120e9),
        RevenueComponent("Advertising Services", 61e9),
    )


def test_keeps_coarse_product_service_split_when_it_is_the_only_cut():
    # A filer that reports only the goods/services split keeps it — dropping the
    # generic members would lose its only product disaggregation.
    html = "<html>" + "".join(
        [
            _ctx("c_prod", *_QUARTER, [(_PROD_AXIS, "us-gaap:ProductMember")]),
            _ctx("c_svc", *_QUARTER, [(_PROD_AXIS, "us-gaap:ServiceMember")]),
            _fact("c_prod", "71,000"),
            _fact("c_svc", "110,000"),
        ]
    ) + "</html>"
    p = provider_with(
        [
            ("company_tickers", 200, _TICKERS),
            ("submissions", 200, _submissions(("f.htm", "2026-05-01"))),
            ("f.htm", 200, html),
        ]
    )
    bd = p.get_quarterly_segment_revenue("AAPL")[date(2026, 3, 31)]
    assert bd.by_product == (
        RevenueComponent("Service", 110e9),
        RevenueComponent("Product", 71e9),
    )


def test_unknown_ticker_raises_not_found():
    p = provider_with([("company_tickers", 200, {})])
    with pytest.raises(StockNotFound):
        p.get_quarterly_segment_revenue("ZZZZ")


def test_submissions_failure_raises_unavailable():
    p = provider_with(
        [("company_tickers", 200, _TICKERS), ("submissions", 500, "server error")]
    )
    with pytest.raises(StockDataUnavailable):
        p.get_quarterly_segment_revenue("AAPL")


def test_tickers_fetch_failure_raises_unavailable():
    p = provider_with([("company_tickers", 503, "unavailable")])
    with pytest.raises(StockDataUnavailable):
        p.get_quarterly_segment_revenue("AAPL")


def test_transport_error_raises_unavailable():
    p = provider_with([("company_tickers", 200, httpx.ConnectError("boom"))])
    with pytest.raises(StockDataUnavailable):
        p.get_quarterly_segment_revenue("AAPL")


def test_cik_map_fetched_once_across_symbols():
    sub = _submissions(("aapl-20260331.htm", "2026-05-01"))
    p = provider_with(
        [
            ("company_tickers", 200, _TICKERS),
            ("submissions", 200, sub),
            ("aapl-20260331.htm", 200, _FILING),
        ]
    )
    p.get_quarterly_segment_revenue("AAPL")
    p.get_quarterly_segment_revenue("AAPL")
    assert sum("company_tickers" in u for u in p._http.requests) == 1


@pytest.mark.parametrize(
    "member, expected",
    [
        ("NorthAmericaSegmentMember", "North America"),
        ("AWSSegmentMember", "AWS"),
        ("InternationalSegmentMember", "International"),
        ("DRAMProductsMember", "DRAM Products"),
        ("OnlineStoresMember", "Online Stores"),
        ("ThirdPartySellerServicesMember", "Third Party Seller Services"),
        ("CMBUMember", "CMBU"),
        ("AllOtherSegmentsMember", "All Other Segments"),
    ],
)
def test_humanize_member_labels(member, expected):
    assert _humanize_member(member) == expected
