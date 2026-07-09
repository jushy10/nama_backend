"""Tests for the revenue-segments entities: member humanization + the segmentation views.

Pure and offline — no I/O. Covers ``humanize_member`` (the raw XBRL member -> display label),
the ``label`` property that rides it, and the ``RevenueSegmentation`` views a client reads
(``fiscal_years`` / ``latest_fiscal_year`` / ``for_axis`` / ``latest_for_axis``).
"""

from datetime import date

import pytest

from app.stocks.revenue_segments.entities import (
    RevenueSegment,
    RevenueSegmentation,
    SegmentAxis,
    humanize_member,
)


@pytest.mark.parametrize(
    "member, expected",
    [
        ("GoogleServicesMember", "Google Services"),
        ("GoogleCloudMember", "Google Cloud"),
        ("AllOtherSegmentsMember", "All Other Segments"),
        ("UnitedStatesMember", "United States"),
        ("EMEAMember", "EMEA"),  # a bare acronym stays intact
        ("US", "US"),  # no Member suffix, nothing to split
        ("AsiaPacificMember", "Asia Pacific"),
    ],
)
def test_humanize_member(member, expected):
    assert humanize_member(member) == expected


def _seg(year, axis, member, value) -> RevenueSegment:
    return RevenueSegment(
        fiscal_year=year,
        period_end=date(year, 12, 31),
        axis=axis,
        member=member,
        value=value,
    )


def test_label_property_rides_humanize():
    assert _seg(2024, SegmentAxis.PRODUCT, "GoogleCloudMember", 1.0).label == "Google Cloud"


def test_is_empty():
    assert RevenueSegmentation("GOOGL", ()).is_empty
    assert not RevenueSegmentation("GOOGL", (_seg(2024, SegmentAxis.BUSINESS, "A", 1),)).is_empty


def test_fiscal_years_are_distinct_and_descending():
    seg = RevenueSegmentation(
        "GOOGL",
        (
            _seg(2023, SegmentAxis.BUSINESS, "A", 1),
            _seg(2024, SegmentAxis.BUSINESS, "A", 2),
            _seg(2024, SegmentAxis.GEOGRAPHY, "US", 3),  # same year, different axis
            _seg(2022, SegmentAxis.BUSINESS, "A", 4),
        ),
    )
    assert seg.fiscal_years == (2024, 2023, 2022)
    assert seg.latest_fiscal_year == 2024


def test_latest_fiscal_year_is_none_when_empty():
    assert RevenueSegmentation("GOOGL", ()).latest_fiscal_year is None


def test_for_axis_filters_and_sorts_year_then_value():
    seg = RevenueSegmentation(
        "GOOGL",
        (
            _seg(2024, SegmentAxis.BUSINESS, "Services", 300),
            _seg(2024, SegmentAxis.BUSINESS, "Cloud", 60),
            _seg(2023, SegmentAxis.BUSINESS, "Services", 250),
            _seg(2024, SegmentAxis.GEOGRAPHY, "US", 200),  # different axis, excluded
        ),
    )
    rows = seg.for_axis(SegmentAxis.BUSINESS)
    # newest year first, then largest value first
    assert [(r.fiscal_year, r.member) for r in rows] == [
        (2024, "Services"),
        (2024, "Cloud"),
        (2023, "Services"),
    ]


def test_latest_for_axis_returns_only_the_newest_year_of_that_axis():
    seg = RevenueSegmentation(
        "GOOGL",
        (
            _seg(2024, SegmentAxis.PRODUCT, "Search", 220),
            _seg(2024, SegmentAxis.PRODUCT, "YouTube", 40),
            _seg(2023, SegmentAxis.PRODUCT, "Search", 180),
            # geography lags a year — its own latest is 2023
            _seg(2023, SegmentAxis.GEOGRAPHY, "US", 150),
        ),
    )
    latest_product = seg.latest_for_axis(SegmentAxis.PRODUCT)
    assert [r.member for r in latest_product] == ["Search", "YouTube"]  # 2024 only, value desc
    latest_geo = seg.latest_for_axis(SegmentAxis.GEOGRAPHY)
    assert [(r.fiscal_year, r.member) for r in latest_geo] == [(2023, "US")]  # its own newest


def test_latest_for_axis_empty_when_axis_absent():
    seg = RevenueSegmentation("GOOGL", (_seg(2024, SegmentAxis.BUSINESS, "A", 1),))
    assert seg.latest_for_axis(SegmentAxis.PRODUCT) == ()
