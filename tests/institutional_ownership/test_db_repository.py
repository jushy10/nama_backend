from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.db import Base
from app.stocks.institutional_ownership.db_repository import (
    _MAX_STORED_HOLDERS,
    SqlInstitutionalOwnershipRepository,
)
from app.stocks.institutional_ownership.entities import (
    HOLDER_TYPE_INSTITUTION,
    HOLDER_TYPE_MUTUAL_FUND,
    InstitutionalHolder,
    InstitutionalOwnership,
    OwnershipBreakdown,
)
from app.stocks.institutional_ownership.models import (
    StockInstitutionalHolderRecord,
    StockOwnershipSummaryRecord,
    StockRecord,
    get_or_create_stock,
)

_NOW = datetime(2026, 7, 12, 12, 0, tzinfo=timezone.utc)
_Q2 = date(2026, 6, 30)
_Q1 = date(2026, 3, 31)


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        yield db


def repo(session, *, now=_NOW) -> SqlInstitutionalOwnershipRepository:
    return SqlInstitutionalOwnershipRepository(session, now=lambda: now)


def _holder(
    holder,
    *,
    holder_type=HOLDER_TYPE_INSTITUTION,
    reported=_Q2,
    shares=1000.0,
    value=100000.0,
    pct_held=8.9,
    pct_change=None,
) -> InstitutionalHolder:
    return InstitutionalHolder(
        holder=holder,
        holder_type=holder_type,
        date_reported=reported,
        shares=shares,
        value=value,
        pct_held=pct_held,
        pct_change=pct_change,
    )


def _ownership(*holders, symbol="AAPL", breakdown=None) -> InstitutionalOwnership:
    return InstitutionalOwnership(
        symbol=symbol, breakdown=breakdown, holders=tuple(holders)
    )


def test_get_on_empty_table_is_a_miss(session):
    assert repo(session).get("AAPL") is None


def test_roundtrips_holders_newest_quarter_then_largest_first(session):
    r = repo(session)
    r.upsert(
        "AAPL",
        "Apple Inc.",
        _ownership(
            _holder("Old Fund", reported=_Q1, value=50000.0),
            _holder("Small Q2", reported=_Q2, value=20000.0),
            _holder("Big Q2", reported=_Q2, value=900000.0),
        ),
    )

    ownership = r.get("AAPL")
    assert isinstance(ownership, InstitutionalOwnership)
    # Q2 before Q1 (newest quarter first); within Q2, largest value first.
    assert [h.holder for h in ownership.holders] == ["Big Q2", "Small Q2", "Old Fund"]
    assert ownership.latest_report_date == _Q2


def test_preserves_all_holder_fields_and_the_breakdown(session):
    r = repo(session)
    r.upsert(
        "AAPL",
        "Apple Inc.",
        _ownership(
            _holder(
                "Vanguard",
                holder_type=HOLDER_TYPE_MUTUAL_FUND,
                shares=1234.0,
                value=567000.0,
                pct_held=8.9,
                pct_change=10.0,
            ),
            breakdown=OwnershipBreakdown(62.3, 0.07, 63.0, 5321),
        ),
    )
    ownership = r.get("AAPL")
    h = ownership.holders[0]
    assert (h.holder, h.holder_type) == ("Vanguard", HOLDER_TYPE_MUTUAL_FUND)
    assert (h.shares, h.value, h.pct_held, h.pct_change) == (1234.0, 567000.0, 8.9, 10.0)
    assert h.share_change == pytest.approx(1234.0 * 10.0 / 110.0)  # derived on the entity
    b = ownership.breakdown
    assert (b.institutions_pct_held, b.insiders_pct_held) == (62.3, 0.07)
    assert (b.institutions_float_pct_held, b.institutions_count) == (63.0, 5321)


def test_upsert_stamps_the_fetch_time(session):
    repo(session).upsert("AAPL", "Apple Inc.", _ownership(_holder("V")))
    stamp = session.execute(
        select(StockInstitutionalHolderRecord.fetched_at)
    ).scalars().first()
    assert stamp.replace(tzinfo=timezone.utc) == _NOW


def test_merge_accumulates_new_quarters_and_replaces_a_reserved_snapshot(session):
    r = repo(session)
    # Q1 snapshot.
    r.upsert("AAPL", "Apple Inc.", _ownership(_holder("A", reported=_Q1)))
    # A later quarter (Q2) — must ADD, keeping Q1.
    r.upsert(
        "AAPL",
        "Apple Inc.",
        _ownership(
            _holder("A", reported=_Q2, pct_change=1.0),
            _holder("C", reported=_Q2),
        ),
    )
    # Re-serve Q2 with A revised and C dropped from the snapshot.
    r.upsert("AAPL", "Apple Inc.", _ownership(_holder("A", reported=_Q2, pct_change=9.0)))

    ownership = r.get("AAPL")
    by_key = {(h.holder, h.date_reported): h for h in ownership.holders}
    assert set(by_key) == {("A", _Q1), ("A", _Q2)}  # Q1 kept; C dropped from Q2 snapshot
    assert by_key[("A", _Q2)].pct_change == 9.0  # revised, not duplicated
    rows = session.execute(
        select(func.count()).select_from(StockInstitutionalHolderRecord)
    ).scalar_one()
    assert rows == 2


def test_merge_does_not_cross_holder_types_in_the_same_quarter(session):
    # An institution and a fund reported for the same quarter are distinct snapshots; re-serving the
    # institutions must not wipe the funds.
    r = repo(session)
    r.upsert(
        "AAPL",
        "Apple Inc.",
        _ownership(
            _holder("Inst", holder_type=HOLDER_TYPE_INSTITUTION),
            _holder("Fund", holder_type=HOLDER_TYPE_MUTUAL_FUND),
        ),
    )
    r.upsert(
        "AAPL",
        "Apple Inc.",
        _ownership(_holder("Inst", holder_type=HOLDER_TYPE_INSTITUTION, pct_change=2.0)),
    )
    holders = {h.holder for h in r.get("AAPL").holders}
    assert holders == {"Inst", "Fund"}  # the fund snapshot survived the institution refresh


def test_upsert_prunes_the_history_to_the_retention_cap(session):
    # One fetch carrying more than the cap: the store keeps only the newest N, dropping the rest.
    overflow = _MAX_STORED_HOLDERS + 5
    holders = [
        _holder(f"H{i:03d}", value=float(i)) for i in range(overflow)
    ]
    repo(session).upsert("AAPL", "Apple Inc.", _ownership(*holders))

    ownership = repo(session).get("AAPL")
    assert len(ownership.holders) == _MAX_STORED_HOLDERS
    kept = {h.holder for h in ownership.holders}
    assert f"H{overflow - 1:03d}" in kept  # largest value kept
    assert "H000" not in kept  # smallest pruned


def test_breakdown_is_a_single_row_overwritten_each_refresh(session):
    r = repo(session)
    r.upsert("AAPL", "Apple Inc.", _ownership(_holder("A"), breakdown=OwnershipBreakdown(60.0, 0.1, 61.0, 100)))
    r.upsert("AAPL", "Apple Inc.", _ownership(_holder("A"), breakdown=OwnershipBreakdown(65.0, 0.2, 66.0, 120)))

    assert r.get("AAPL").breakdown.institutions_pct_held == 65.0
    count = session.execute(
        select(func.count()).select_from(StockOwnershipSummaryRecord)
    ).scalar_one()
    assert count == 1  # overwritten, not accumulated


def test_a_none_breakdown_clears_the_stored_figures(session):
    r = repo(session)
    r.upsert("AAPL", "Apple Inc.", _ownership(_holder("A"), breakdown=OwnershipBreakdown(60.0, 0.1, 61.0, 100)))
    r.upsert("AAPL", "Apple Inc.", _ownership(_holder("A"), breakdown=None))
    assert r.get("AAPL").breakdown is None  # figures cleared → the row reads as no-breakdown


def test_upsert_leaves_other_stocks_untouched(session):
    r = repo(session)
    r.upsert("AAPL", "Apple Inc.", _ownership(_holder("A")))
    r.upsert("MSFT", "Microsoft", _ownership(_holder("M"), symbol="MSFT"))

    r.upsert("AAPL", "Apple Inc.", _ownership(_holder("A", pct_change=3.0)))

    assert [h.holder for h in r.get("MSFT").holders] == ["M"]  # MSFT survived AAPL's refresh


def test_creates_the_parent_stock_row_and_fills_name_without_clobbering(session):
    r = repo(session)
    r.upsert("AAPL", None, _ownership(_holder("A")))
    assert session.execute(
        select(StockRecord.name).where(StockRecord.ticker == "AAPL")
    ).scalar_one() is None

    r.upsert("AAPL", "Apple Inc.", _ownership(_holder("A")))
    r.upsert("AAPL", None, _ownership(_holder("A")))  # nameless refresh must not erase it
    assert session.execute(
        select(StockRecord.name).where(StockRecord.ticker == "AAPL")
    ).scalar_one() == "Apple Inc."


def test_refresh_targets_orders_by_last_refresh_and_seeds_uncached_first(session):
    # The merge keeps old quarters' stamps forever, so staleness must read the *newest* stamp: AAPL
    # holds an ancient Q1 row but was refreshed (a fresh Q2 row) after MSFT, so MSFT is staler.
    ancient = repo(session, now=_NOW - timedelta(days=120))
    mid = repo(session, now=_NOW - timedelta(days=10))
    fresh = repo(session, now=_NOW)

    ancient.upsert("AAPL", "Apple Inc.", _ownership(_holder("A", reported=_Q1)))
    mid.upsert("MSFT", "Microsoft", _ownership(_holder("M", reported=_Q1), symbol="MSFT"))
    fresh.upsert("AAPL", "Apple Inc.", _ownership(_holder("A", reported=_Q2)))
    # An anchor stock never fetched — seeded ahead of any cached stock.
    get_or_create_stock(session, "NEWCO", "New Co")
    session.commit()

    targets = fresh.refresh_targets(None)
    assert [t.symbol for t in targets] == ["NEWCO", "MSFT", "AAPL"]
    assert dict(targets)["NEWCO"] == "New Co"  # carries the anchor name
    assert fresh.refresh_targets(1) == [("NEWCO", "New Co")]  # limit respected
