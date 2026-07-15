"""Tests for the database-backed CongressTradesRepository.

Offline: an in-memory SQLite database stands in for the real table. Verifies the round-trip
(entities -> rows -> entities), the insert-only merge (a refresh adds only new trades and never
rewrites a stored one, de-duping within a batch too), the prune to the newest N, the parent
``stocks`` row + name fill-but-don't-clobber, the fetch stamp, the ``refresh_targets`` staleness
order the sweep walks, the market-wide windowed read, and a clean miss.
"""

from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from app.db import Base
from app.stocks.congress import models
from app.stocks.congress.db_repository import (
    SqlCongressTradesRepository,
    _MAX_STORED_TRADES,
)
from app.stocks.congress.entities import CongressActivity, CongressTrade
from app.stocks.congress.models import StockCongressTradeRecord, StockRecord

_NOW = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        yield db


def repo(session, *, now=None) -> SqlCongressTradesRepository:
    return SqlCongressTradesRepository(session, now=now or (lambda: _NOW))


def _max_stamp(session, symbol: str) -> datetime | None:
    return session.execute(
        select(func.max(StockCongressTradeRecord.fetched_at))
        .join(StockRecord, StockCongressTradeRecord.stock_id == StockRecord.id)
        .where(StockRecord.ticker == symbol)
    ).scalar()


def _trade(
    *,
    ticker="NVDA",
    member="Nancy Pelosi",
    chamber="House",
    tx_type="Purchase",
    amount="$1,001 - $15,000",
    txn_date=date(2026, 6, 20),
    disc_date=date(2026, 7, 2),
) -> CongressTrade:
    return CongressTrade(
        member=member,
        chamber=chamber,
        party=None,
        ticker=ticker,
        company_name="NVIDIA Corporation",
        tx_type=tx_type,
        amount_range=amount,
        transaction_date=txn_date,
        disclosure_date=disc_date,
        owner="Self",
        source_url="http://example/1",
    )


def _activity(symbol, *trades) -> CongressActivity:
    return CongressActivity(symbol=symbol, trades=tuple(trades))


def test_get_on_empty_table_is_a_miss(session):
    assert repo(session).get("NVDA") is None


def test_roundtrips_an_activity(session):
    r = repo(session)
    r.upsert("NVDA", "NVIDIA Corporation", _activity("NVDA", _trade()))
    activity = r.get("NVDA")
    assert isinstance(activity, CongressActivity)
    assert len(activity.trades) == 1
    trade = activity.trades[0]
    assert trade.member == "Nancy Pelosi" and trade.chamber == "House"
    assert trade.is_buy and not trade.is_sell
    # company_name comes from the anchor row, not a stored column.
    assert trade.company_name == "NVIDIA Corporation"
    assert trade.amount_midpoint == 8000.5


def test_summary_rolls_up_buys_and_sells(session):
    r = repo(session)
    r.upsert(
        "NVDA",
        "NVIDIA",
        _activity(
            "NVDA",
            _trade(member="A", tx_type="Purchase", amount="$1,001 - $15,000"),
            _trade(member="B", tx_type="Sale", amount="$15,001 - $50,000"),
            _trade(member="C", tx_type="Exchange", amount="$1,001 - $15,000"),
        ),
    )
    summary = r.get("NVDA").summary
    assert summary.buy_count == 1 and summary.sell_count == 1
    assert summary.buy_value == 8000.5 and summary.sell_value == 32500.5
    assert summary.net_value == 8000.5 - 32500.5


def test_upsert_stamps_the_fetch_time(session):
    repo(session).upsert("NVDA", None, _activity("NVDA", _trade()))
    assert _max_stamp(session, "NVDA").replace(tzinfo=timezone.utc) == _NOW


def test_merge_is_insert_only_and_keeps_existing_rows(session):
    r = repo(session)
    r.upsert("NVDA", "NVIDIA", _activity("NVDA", _trade(member="Pelosi")))
    # A refresh with the same trade + one genuinely new one adds only the new one.
    r.upsert(
        "NVDA",
        "NVIDIA",
        _activity(
            "NVDA",
            _trade(member="Pelosi"),
            _trade(member="Tuberville", chamber="Senate", tx_type="Sale"),
        ),
    )
    rows = session.execute(select(StockCongressTradeRecord)).scalars().all()
    assert len(rows) == 2
    assert {row.member for row in rows} == {"Pelosi", "Tuberville"}


def test_merge_dedups_within_a_single_batch(session):
    # Two identical disclosures in one fetch (same identity key) must not both insert.
    r = repo(session)
    r.upsert("NVDA", "NVIDIA", _activity("NVDA", _trade(member="Pelosi"), _trade(member="Pelosi")))
    rows = session.execute(select(StockCongressTradeRecord)).scalars().all()
    assert len(rows) == 1


def test_prune_keeps_only_the_newest_trades(session):
    r = repo(session)
    total = _MAX_STORED_TRADES + 5
    base = date(2020, 1, 1)
    trades = [
        _trade(member=f"Member {i:04d}", disc_date=base + timedelta(days=i))
        for i in range(total)
    ]
    r.upsert("NVDA", "NVIDIA", _activity("NVDA", *trades))
    activity = r.get("NVDA")
    assert len(activity.trades) == _MAX_STORED_TRADES
    # Newest kept, oldest pruned; serving order newest-first.
    assert activity.trades[0].disclosure_date == base + timedelta(days=total - 1)
    assert activity.trades[-1].disclosure_date == base + timedelta(
        days=total - _MAX_STORED_TRADES
    )


def test_prune_is_enforced_under_production_autoflush_false():
    # Regression: production `get_db` uses SessionLocal(autoflush=False). The prune's SELECT must
    # still see the just-added (pending) rows — the repo flushes before pruning. The module
    # `session` fixture uses a raw Session(engine) (autoflush=True), which would mask this.
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    prod_session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with prod_session() as db:
        r = SqlCongressTradesRepository(db, now=lambda: _NOW)
        total = _MAX_STORED_TRADES + 5
        base = date(2020, 1, 1)
        trades = [
            _trade(member=f"Member {i:04d}", disc_date=base + timedelta(days=i))
            for i in range(total)
        ]
        r.upsert("NVDA", "NVIDIA", _activity("NVDA", *trades))
        assert len(r.get("NVDA").trades) == _MAX_STORED_TRADES


def test_serves_newest_first_by_activity_date(session):
    r = repo(session)
    r.upsert(
        "NVDA",
        "NVIDIA",
        _activity(
            "NVDA",
            _trade(member="A", disc_date=date(2026, 1, 1)),
            _trade(member="B", disc_date=date(2026, 6, 1)),
            _trade(member="C", disc_date=date(2026, 3, 1)),
        ),
    )
    dates = [t.disclosure_date for t in r.get("NVDA").trades]
    assert dates == [date(2026, 6, 1), date(2026, 3, 1), date(2026, 1, 1)]


def test_activity_date_falls_back_to_transaction_date(session):
    # A trade with no disclosure date orders by its transaction date.
    r = repo(session)
    r.upsert(
        "NVDA",
        "NVIDIA",
        _activity(
            "NVDA",
            _trade(member="Has disclosure", txn_date=date(2026, 5, 1), disc_date=date(2026, 5, 20)),
            _trade(member="No disclosure", txn_date=date(2026, 6, 1), disc_date=None),
        ),
    )
    members = [t.member for t in r.get("NVDA").trades]
    assert members == ["No disclosure", "Has disclosure"]  # 06-01 txn > 05-20 disclosure


def test_upsert_leaves_other_stocks_untouched(session):
    r = repo(session)
    r.upsert("NVDA", "NVIDIA", _activity("NVDA", _trade(member="A")))
    r.upsert("AAPL", "Apple", _activity("AAPL", _trade(ticker="AAPL", member="B")))
    r.upsert("NVDA", "NVIDIA", _activity("NVDA", _trade(member="C")))
    assert len(r.get("AAPL").trades) == 1


def test_creates_the_parent_stock_and_fills_name_without_clobbering(session):
    r = repo(session)
    r.upsert("NVDA", None, _activity("NVDA", _trade()))
    assert (
        session.execute(select(StockRecord.name).where(StockRecord.ticker == "NVDA")).scalar_one()
        is None
    )
    r.upsert("NVDA", "NVIDIA Corporation", _activity("NVDA", _trade()))
    r.upsert("NVDA", None, _activity("NVDA", _trade()))  # a nameless refresh must not erase it
    assert (
        session.execute(select(StockRecord.name).where(StockRecord.ticker == "NVDA")).scalar_one()
        == "NVIDIA Corporation"
    )


def test_refresh_targets_uncached_first_then_stalest(session):
    repo(session, now=lambda: _NOW - timedelta(days=10)).upsert(
        "NVDA", "NVIDIA", _activity("NVDA", _trade())
    )
    repo(session, now=lambda: _NOW).upsert(
        "AAPL", "Apple", _activity("AAPL", _trade(ticker="AAPL"))
    )
    models.get_or_create_stock(session, "TSLA", "Tesla")  # anchor only, never cached
    session.commit()

    targets = repo(session).refresh_targets(None)
    assert [t.symbol for t in targets] == ["TSLA", "NVDA", "AAPL"]
    assert dict(targets) == {"TSLA": "Tesla", "NVDA": "NVIDIA", "AAPL": "Apple"}


def test_refresh_targets_respects_the_limit(session):
    for sym in ("NVDA", "AAPL", "TSLA"):
        models.get_or_create_stock(session, sym, sym.title())
    session.commit()
    assert len(repo(session).refresh_targets(2)) == 2


def test_recent_market_activity_windows_and_paginates(session):
    r = repo(session)
    r.upsert(
        "NVDA",
        "NVIDIA",
        _activity(
            "NVDA",
            _trade(member="Recent", disc_date=date(2026, 7, 1)),
            _trade(member="Old", disc_date=date(2020, 1, 1)),
        ),
    )
    r.upsert("AAPL", "Apple", _activity("AAPL", _trade(ticker="AAPL", member="Mid", disc_date=date(2026, 6, 15))))

    # No window: all three, newest first, each carrying its ticker + anchor name.
    trades, total = r.recent_market_activity(since=None, limit=10, offset=0)
    assert total == 3
    assert [t.member for t in trades] == ["Recent", "Mid", "Old"]
    assert trades[0].ticker == "NVDA" and trades[0].company_name == "NVIDIA"

    # Windowed to 2026-06-01: drops the 2020 row.
    trades, total = r.recent_market_activity(since=date(2026, 6, 1), limit=10, offset=0)
    assert total == 2 and [t.member for t in trades] == ["Recent", "Mid"]

    # Pagination: limit=1, offset=1 -> the second newest.
    page, total = r.recent_market_activity(since=None, limit=1, offset=1)
    assert total == 3 and len(page) == 1 and page[0].member == "Mid"


def test_market_trades_in_window_returns_the_whole_unpaged_window(session):
    r = repo(session)
    r.upsert(
        "NVDA",
        "NVIDIA",
        _activity(
            "NVDA",
            _trade(member="Recent", disc_date=date(2026, 7, 1)),
            _trade(member="Old", disc_date=date(2020, 1, 1)),
        ),
    )
    r.upsert("AAPL", "Apple", _activity("AAPL", _trade(ticker="AAPL", member="Mid", disc_date=date(2026, 6, 15))))

    # No window: every stored trade, newest first, each carrying its ticker + anchor name — and no
    # limit/offset (the leaderboard aggregates the whole set).
    trades = r.market_trades_in_window(since=None)
    assert [t.member for t in trades] == ["Recent", "Mid", "Old"]
    assert trades[0].ticker == "NVDA" and trades[0].company_name == "NVIDIA"

    # Windowed to 2026-06-01: drops the 2020 row.
    trades = r.market_trades_in_window(since=date(2026, 6, 1))
    assert [t.member for t in trades] == ["Recent", "Mid"]
