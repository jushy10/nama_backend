import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.db import Base
from app.stocks.catalog.index_membership.db_repository import SqlIndexMembershipRepository
from app.stocks.catalog.index_membership.entities import IndexMembershipSnapshot
from app.stocks.catalog.anchor.models import StockRecord, get_or_create_stock


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        yield db


def _snap(*, sp500=(), nasdaq100=()) -> IndexMembershipSnapshot:
    return IndexMembershipSnapshot(
        sp500=frozenset(sp500), nasdaq100=frozenset(nasdaq100)
    )


def _row(session, ticker) -> StockRecord:
    return session.execute(
        select(StockRecord).where(StockRecord.ticker == ticker)
    ).scalar_one()


def test_reconcile_marks_members_and_creates_absent_anchors(session):
    counts = SqlIndexMembershipRepository(session).reconcile(
        _snap(sp500=("AAPL", "MSFT"), nasdaq100=("AAPL", "NVDA")),
        sync_sp500=True,
        sync_nasdaq100=True,
    )

    assert (counts.sp500_marked, counts.sp500_cleared) == (2, 0)
    assert (counts.nasdaq100_marked, counts.nasdaq100_cleared) == (2, 0)
    aapl = _row(session, "AAPL")
    assert (aapl.in_sp500, aapl.in_nasdaq100) == (True, True)  # in both
    msft = _row(session, "MSFT")
    assert (msft.in_sp500, msft.in_nasdaq100) == (True, False)  # S&P only
    nvda = _row(session, "NVDA")
    assert (nvda.in_sp500, nvda.in_nasdaq100) == (False, True)  # Nasdaq only


def test_reconcile_clears_dropouts(session):
    repo = SqlIndexMembershipRepository(session)
    repo.reconcile(_snap(sp500=("AAPL", "MSFT")), sync_sp500=True, sync_nasdaq100=False)
    # A later run no longer lists MSFT (removed from the index).
    counts = repo.reconcile(
        _snap(sp500=("AAPL",)), sync_sp500=True, sync_nasdaq100=False
    )

    # AAPL was already a member (not recounted); MSFT is cleared.
    assert (counts.sp500_marked, counts.sp500_cleared) == (0, 1)
    assert _row(session, "AAPL").in_sp500 is True
    assert _row(session, "MSFT").in_sp500 is False


def test_a_skipped_index_is_left_untouched(session):
    repo = SqlIndexMembershipRepository(session)
    repo.reconcile(_snap(sp500=("AAPL",)), sync_sp500=True, sync_nasdaq100=False)
    # sp500 came back degraded (empty) => sync_sp500 False: AAPL must NOT be cleared even
    # though it's absent from this (empty) set. Only the healthy nasdaq index is reconciled.
    counts = repo.reconcile(
        _snap(sp500=(), nasdaq100=("NVDA",)), sync_sp500=False, sync_nasdaq100=True
    )

    assert (counts.sp500_marked, counts.sp500_cleared) == (0, 0)
    assert _row(session, "AAPL").in_sp500 is True  # untouched despite the empty set
    assert _row(session, "NVDA").in_nasdaq100 is True


def test_reconcile_does_not_clobber_existing_anchor_facts(session):
    # A ticker the app already knows, with a stored name.
    get_or_create_stock(session, "AAPL", "Apple Inc.")
    session.commit()

    SqlIndexMembershipRepository(session).reconcile(
        _snap(sp500=("AAPL",)), sync_sp500=True, sync_nasdaq100=False
    )

    aapl = _row(session, "AAPL")
    assert aapl.name == "Apple Inc."  # marking (get_or_create with no name) never wipes it
    assert aapl.in_sp500 is True


def test_marked_counts_only_genuine_transitions(session):
    repo = SqlIndexMembershipRepository(session)
    first = repo.reconcile(
        _snap(sp500=("AAPL",)), sync_sp500=True, sync_nasdaq100=False
    )
    second = repo.reconcile(
        _snap(sp500=("AAPL",)), sync_sp500=True, sync_nasdaq100=False
    )

    assert first.sp500_marked == 1
    assert second.sp500_marked == 0  # already a member; a re-affirmation isn't counted
