from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.db import Base
from app.stocks.company.revenue_segments.db_repository import (
    SqlRevenueSegmentsRepository,
    _MAX_STORED_YEARS,
)
from app.stocks.company.revenue_segments.entities import (
    RevenueSegment,
    RevenueSegmentation,
    SegmentAxis,
)
from app.stocks.company.revenue_segments.models import (
    StockRevenueSegmentRecord,
    StockRecord,
    get_or_create_stock,
)

_NOW = datetime(2026, 7, 9, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        yield db


def repo(session, *, now=None) -> SqlRevenueSegmentsRepository:
    return SqlRevenueSegmentsRepository(session, now=now or (lambda: _NOW))


def _seg(year, axis, member, value) -> RevenueSegment:
    return RevenueSegment(
        fiscal_year=year,
        period_end=date(year, 12, 31),
        axis=axis,
        member=member,
        value=value,
    )


def _segmentation(symbol, *segments) -> RevenueSegmentation:
    return RevenueSegmentation(symbol=symbol, segments=tuple(segments))


def test_get_on_empty_table_is_a_miss(session):
    assert repo(session).get("GOOGL") is None


def test_roundtrips_a_segmentation(session):
    r = repo(session)
    r.upsert(
        "GOOGL",
        "Alphabet Inc.",
        _segmentation(
            "GOOGL",
            _seg(2024, SegmentAxis.BUSINESS, "GoogleCloudMember", 58.7e9),
            _seg(2024, SegmentAxis.PRODUCT, "GoogleSearchOtherMember", 224.5e9),
            _seg(2024, SegmentAxis.GEOGRAPHY, "US", 194.2e9),
        ),
    )
    seg = r.get("GOOGL")
    assert isinstance(seg, RevenueSegmentation)
    assert seg.fiscal_years == (2024,)
    cloud = seg.latest_for_axis(SegmentAxis.BUSINESS)[0]
    assert cloud.member == "GoogleCloudMember" and cloud.value == 58.7e9
    assert cloud.axis is SegmentAxis.BUSINESS  # the slug round-tripped back to the enum
    assert cloud.label == "Google Cloud"  # derived, not stored


def test_upsert_stamps_the_fetch_time(session):
    repo(session).upsert(
        "GOOGL", None, _segmentation("GOOGL", _seg(2024, SegmentAxis.BUSINESS, "A", 1e9))
    )
    stamp = session.execute(select(StockRevenueSegmentRecord.fetched_at)).scalars().first()
    assert stamp.replace(tzinfo=timezone.utc) == _NOW


def test_merge_replaces_restated_years_and_keeps_earlier_ones(session):
    r = repo(session)
    # First filing covers 2022 + 2023.
    r.upsert(
        "GOOGL",
        "Alphabet Inc.",
        _segmentation(
            "GOOGL",
            _seg(2022, SegmentAxis.BUSINESS, "A", 100e9),
            _seg(2023, SegmentAxis.BUSINESS, "A", 110e9),
        ),
    )
    # Next filing covers 2023 + 2024: 2023 is restated, 2024 is new, 2022 is untouched.
    r.upsert(
        "GOOGL",
        "Alphabet Inc.",
        _segmentation(
            "GOOGL",
            _seg(2023, SegmentAxis.BUSINESS, "A", 111e9),  # restated value
            _seg(2024, SegmentAxis.BUSINESS, "A", 120e9),
        ),
    )
    seg = r.get("GOOGL")
    assert seg.fiscal_years == (2024, 2023, 2022)  # 2022 retained (merge, not rewrite)
    by_year = {s.fiscal_year: s.value for s in seg.segments}
    assert by_year == {2022: 100e9, 2023: 111e9, 2024: 120e9}  # 2023 took the fresh value


def test_prune_keeps_only_the_newest_years(session):
    r = repo(session)
    # Seed more distinct years than the cap, one at a time so each is a separate "filing".
    for year in range(2018, 2018 + _MAX_STORED_YEARS + 3):
        r.upsert(
            "GOOGL",
            "Alphabet Inc.",
            _segmentation("GOOGL", _seg(year, SegmentAxis.BUSINESS, "A", year)),
        )
    seg = r.get("GOOGL")
    assert len(seg.fiscal_years) == _MAX_STORED_YEARS
    newest = 2018 + _MAX_STORED_YEARS + 3 - 1
    assert seg.fiscal_years[0] == newest
    assert min(seg.fiscal_years) == newest - _MAX_STORED_YEARS + 1  # oldest pruned off


def test_upsert_leaves_other_stocks_untouched(session):
    r = repo(session)
    r.upsert("GOOGL", "Alphabet", _segmentation("GOOGL", _seg(2024, SegmentAxis.BUSINESS, "A", 1e9)))
    r.upsert("MSFT", "Microsoft", _segmentation("MSFT", _seg(2024, SegmentAxis.BUSINESS, "B", 2e9)))

    r.upsert("GOOGL", "Alphabet", _segmentation("GOOGL", _seg(2025, SegmentAxis.BUSINESS, "A", 3e9)))

    assert len(r.get("MSFT").segments) == 1  # MSFT survived GOOGL's merge


def test_creates_the_parent_stock_row(session):
    repo(session).upsert(
        "GOOGL", "Alphabet Inc.", _segmentation("GOOGL", _seg(2024, SegmentAxis.BUSINESS, "A", 1e9))
    )
    stock = session.execute(
        select(StockRecord).where(StockRecord.ticker == "GOOGL")
    ).scalar_one()
    assert stock.name == "Alphabet Inc." and stock.id is not None


def test_fills_a_missing_name_but_never_clobbers_a_known_one(session):
    r = repo(session)
    seg = _segmentation("GOOGL", _seg(2024, SegmentAxis.BUSINESS, "A", 1e9))
    r.upsert("GOOGL", None, seg)
    assert session.execute(
        select(StockRecord.name).where(StockRecord.ticker == "GOOGL")
    ).scalar_one() is None

    r.upsert("GOOGL", "Alphabet Inc.", seg)
    r.upsert("GOOGL", None, seg)  # a nameless refresh must not erase it
    assert (
        session.execute(
            select(StockRecord.name).where(StockRecord.ticker == "GOOGL")
        ).scalar_one()
        == "Alphabet Inc."
    )


def test_refresh_targets_orders_stalest_first_and_carries_the_name(session):
    older = repo(session, now=lambda: _NOW - timedelta(days=30))
    newer = repo(session, now=lambda: _NOW)
    older.upsert("MSFT", "Microsoft", _segmentation("MSFT", _seg(2024, SegmentAxis.BUSINESS, "B", 1e9)))
    newer.upsert("GOOGL", "Alphabet", _segmentation("GOOGL", _seg(2024, SegmentAxis.BUSINESS, "A", 1e9)))

    targets = newer.refresh_targets(10)
    assert [t.symbol for t in targets] == ["MSFT", "GOOGL"]  # stalest first
    assert targets[0] == ("MSFT", "Microsoft")  # RefreshTarget carries the stored name
    assert newer.refresh_targets(1) == [("MSFT", "Microsoft")]  # limit respected


def test_refresh_targets_seeds_uncached_anchor_stocks_first(session):
    r = repo(session)
    r.upsert("MSFT", "Microsoft", _segmentation("MSFT", _seg(2024, SegmentAxis.BUSINESS, "B", 1e9)))
    get_or_create_stock(session, "NEWCO", "New Co")  # anchor only, never fetched
    session.commit()

    targets = r.refresh_targets(None)  # None => every anchor stock
    assert [t.symbol for t in targets] == ["NEWCO", "MSFT"]  # un-cached seeded first
    assert dict(targets)["NEWCO"] == "New Co"


def test_merge_stamps_only_the_refreshed_years(session):
    # A stock's rows can carry different fetch stamps (the merge keeps old years' stamps); the
    # newest stamp is what stalest ordering uses.
    r = repo(session)
    old = SqlRevenueSegmentsRepository(session, now=lambda: _NOW - timedelta(days=10))
    old.upsert("GOOGL", "Alphabet", _segmentation("GOOGL", _seg(2023, SegmentAxis.BUSINESS, "A", 1e9)))
    r.upsert("GOOGL", "Alphabet", _segmentation("GOOGL", _seg(2024, SegmentAxis.BUSINESS, "A", 2e9)))

    stamps = session.execute(select(StockRevenueSegmentRecord.fetched_at)).scalars().all()
    normalized = {s.replace(tzinfo=timezone.utc) for s in stamps}
    assert normalized == {_NOW - timedelta(days=10), _NOW}  # 2023 kept its older stamp
