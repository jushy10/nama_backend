from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from app.db import Base
from app.stocks.company.insider_transactions import models
from app.stocks.company.insider_transactions.insider_transactions_repository_adapter_impl import (
    InsiderTransactionsRepositoryAdapterImpl,
    _MAX_STORED_TRANSACTIONS,
)
from app.stocks.company.insider_transactions.entities import (
    InsiderActivity,
    InsiderTransaction,
)
from app.stocks.company.insider_transactions.models import (
    StockInsiderTransactionRecord,
    StockRecord,
)

_NOW = datetime(2026, 7, 11, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        yield db


def repo(session, *, now=None) -> InsiderTransactionsRepositoryAdapterImpl:
    return InsiderTransactionsRepositoryAdapterImpl(session, now=now or (lambda: _NOW))


def _max_stamp(session, symbol: str) -> datetime | None:
    return session.execute(
        select(func.max(StockInsiderTransactionRecord.fetched_at))
        .join(StockRecord, StockInsiderTransactionRecord.stock_id == StockRecord.id)
        .where(StockRecord.ticker == symbol)
    ).scalar()


def _txn(
    *,
    accession="0000000000-26-000001",
    line_index=0,
    code="P",
    shares=100.0,
    price=10.0,
    txn_date=date(2026, 6, 15),
) -> InsiderTransaction:
    return InsiderTransaction(
        filing_date=date(2026, 6, 17),
        transaction_date=txn_date,
        insider_name="Jane Insider",
        officer_title="CEO",
        is_director=False,
        is_officer=True,
        is_ten_percent_owner=False,
        security_title="Common Stock",
        transaction_code=code,
        acquired_disposed="A" if code == "P" else "D",
        shares=shares,
        price_per_share=price,
        shares_owned_following=1000.0,
        accession_number=accession,
        line_index=line_index,
    )


def _activity(symbol, *txns) -> InsiderActivity:
    return InsiderActivity(symbol=symbol, transactions=tuple(txns))


def test_get_on_empty_table_is_a_miss(session):
    assert repo(session).get("AAPL") is None


def test_roundtrips_an_activity(session):
    r = repo(session)
    r.upsert("AAPL", "Apple Inc.", _activity("AAPL", _txn(code="P", shares=100, price=10)))
    activity = r.get("AAPL")
    assert isinstance(activity, InsiderActivity)
    assert len(activity.transactions) == 1
    txn = activity.transactions[0]
    assert txn.transaction_code == "P" and txn.value == 1000
    assert txn.role == "CEO"  # officer_title round-tripped and drives the derived role
    assert txn.is_open_market_buy


def test_upsert_stamps_the_fetch_time(session):
    repo(session).upsert("AAPL", None, _activity("AAPL", _txn()))
    assert _max_stamp(session, "AAPL").replace(tzinfo=timezone.utc) == _NOW


def test_merge_is_insert_only_and_keeps_existing_rows(session):
    r = repo(session)
    # First fetch: two transactions from one filing.
    r.upsert(
        "AAPL",
        "Apple Inc.",
        _activity(
            "AAPL",
            _txn(accession="acc-1", line_index=0, code="P"),
            _txn(accession="acc-1", line_index=1, code="S"),
        ),
    )
    # Next fetch: the same two (already stored) + one genuinely new transaction.
    r.upsert(
        "AAPL",
        "Apple Inc.",
        _activity(
            "AAPL",
            _txn(accession="acc-1", line_index=0, code="P"),
            _txn(accession="acc-1", line_index=1, code="S"),
            _txn(accession="acc-2", line_index=0, code="P"),
        ),
    )
    rows = session.execute(select(StockInsiderTransactionRecord)).scalars().all()
    keys = {(row.accession_number, row.line_index) for row in rows}
    assert keys == {("acc-1", 0), ("acc-1", 1), ("acc-2", 0)}  # only the new one added
    assert len(rows) == 3  # no duplicate insert of the two already-stored transactions


def test_touch_refreshes_the_whole_feed_stamp_even_with_no_new_rows(session):
    older = repo(session, now=lambda: _NOW - timedelta(days=2))
    older.upsert("AAPL", "Apple", _activity("AAPL", _txn(accession="acc-1", line_index=0)))
    # A later fetch that brings nothing new must still advance the fetch stamp so the sweep's
    # staleness order sees a quiet stock (confirmed with no new activity) as freshly refreshed
    # instead of leaving it stuck at the front of the stale queue.
    newer = repo(session, now=lambda: _NOW)
    newer.upsert("AAPL", "Apple", _activity("AAPL", _txn(accession="acc-1", line_index=0)))
    assert _max_stamp(session, "AAPL").replace(tzinfo=timezone.utc) == _NOW


def test_prune_keeps_only_the_newest_transactions(session):
    r = repo(session)
    total = _MAX_STORED_TRANSACTIONS + 5
    # One transaction per filing, each on a distinct (older -> newer) date.
    base = date(2020, 1, 1)
    txns = [
        _txn(accession=f"acc-{i:04d}", line_index=0, txn_date=base + timedelta(days=i))
        for i in range(total)
    ]
    r.upsert("AAPL", "Apple", _activity("AAPL", *txns))
    activity = r.get("AAPL")
    assert len(activity.transactions) == _MAX_STORED_TRANSACTIONS
    # The newest kept; the 5 oldest pruned off. Serving order is newest-first.
    newest_kept = activity.transactions[0].transaction_date
    oldest_kept = activity.transactions[-1].transaction_date
    assert newest_kept == base + timedelta(days=total - 1)
    assert oldest_kept == base + timedelta(days=total - _MAX_STORED_TRANSACTIONS)


def test_serves_newest_first(session):
    r = repo(session)
    r.upsert(
        "AAPL",
        "Apple",
        _activity(
            "AAPL",
            _txn(accession="a", line_index=0, txn_date=date(2026, 1, 1)),
            _txn(accession="b", line_index=0, txn_date=date(2026, 6, 1)),
            _txn(accession="c", line_index=0, txn_date=date(2026, 3, 1)),
        ),
    )
    dates = [t.transaction_date for t in r.get("AAPL").transactions]
    assert dates == [date(2026, 6, 1), date(2026, 3, 1), date(2026, 1, 1)]


def test_creates_the_parent_stock_and_fills_name_without_clobbering(session):
    r = repo(session)
    r.upsert("AAPL", None, _activity("AAPL", _txn()))
    assert (
        session.execute(select(StockRecord.name).where(StockRecord.ticker == "AAPL")).scalar_one()
        is None
    )
    r.upsert("AAPL", "Apple Inc.", _activity("AAPL", _txn()))
    r.upsert("AAPL", None, _activity("AAPL", _txn()))  # a nameless refresh must not erase it
    assert (
        session.execute(select(StockRecord.name).where(StockRecord.ticker == "AAPL")).scalar_one()
        == "Apple Inc."
    )


def test_prune_is_enforced_under_production_autoflush_false():
    # Regression: production `get_db` uses SessionLocal(autoflush=False). The prune's SELECT must
    # still see the just-added (pending) rows — the repo flushes before pruning. The module
    # `session` fixture uses a raw Session(engine), which defaults to autoflush=True and would mask
    # this: without the flush, the cap silently over-stores here.
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    prod_session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with prod_session() as db:
        r = InsiderTransactionsRepositoryAdapterImpl(db, now=lambda: _NOW)
        total = _MAX_STORED_TRANSACTIONS + 5
        base = date(2020, 1, 1)
        txns = [
            _txn(accession=f"acc-{i:04d}", line_index=0, txn_date=base + timedelta(days=i))
            for i in range(total)
        ]
        r.upsert("AAPL", "Apple", _activity("AAPL", *txns))
        assert len(r.get("AAPL").transactions) == _MAX_STORED_TRANSACTIONS


def test_serves_within_a_filing_in_document_order(session):
    # The serving order must match the SEC adapter's own sort exactly (so a live-served and a
    # cache-served response are identical): newest transaction first across filings, then
    # document order (line_index ascending) within a filing.
    r = repo(session)
    r.upsert(
        "AAPL",
        "Apple",
        _activity(
            "AAPL",
            _txn(accession="old", line_index=0, txn_date=date(2026, 6, 15)),
            _txn(accession="old", line_index=1, txn_date=date(2026, 6, 15)),
            _txn(accession="old", line_index=2, txn_date=date(2026, 6, 15)),
            _txn(accession="new", line_index=0, txn_date=date(2026, 7, 1)),  # newer filing
        ),
    )
    read = r.get("AAPL").transactions
    assert [(t.accession_number, t.line_index) for t in read] == [
        ("new", 0),  # newest transaction first
        ("old", 0),  # then the older filing, in document order
        ("old", 1),
        ("old", 2),
    ]


def test_upsert_leaves_other_stocks_untouched(session):
    r = repo(session)
    r.upsert("AAPL", "Apple", _activity("AAPL", _txn(accession="a")))
    r.upsert("MSFT", "Microsoft", _activity("MSFT", _txn(accession="b")))
    r.upsert("AAPL", "Apple", _activity("AAPL", _txn(accession="c")))
    assert len(r.get("MSFT").transactions) == 1  # MSFT survived AAPL's refresh


def test_refresh_targets_uncached_first_then_stalest(session):
    # A cached stock refreshed long ago, a cached stock refreshed recently, and an un-cached
    # anchor row (a screened stock with no insider rows yet). The sweep walks them un-cached
    # first (to seed), then least-recently-refreshed, each paired with its stored name.
    repo(session, now=lambda: _NOW - timedelta(days=10)).upsert(
        "AAPL", "Apple", _activity("AAPL", _txn(accession="a"))
    )
    repo(session, now=lambda: _NOW).upsert(
        "MSFT", "Microsoft", _activity("MSFT", _txn(accession="b"))
    )
    models.get_or_create_stock(session, "TSLA", "Tesla")  # anchor only, never cached
    session.commit()

    targets = repo(session).refresh_targets(None)

    assert [t.symbol for t in targets] == ["TSLA", "AAPL", "MSFT"]
    assert dict(targets) == {"TSLA": "Tesla", "AAPL": "Apple", "MSFT": "Microsoft"}


def test_refresh_targets_respects_the_limit(session):
    for sym in ("AAPL", "MSFT", "TSLA"):
        models.get_or_create_stock(session, sym, sym.title())
    session.commit()
    assert len(repo(session).refresh_targets(2)) == 2
