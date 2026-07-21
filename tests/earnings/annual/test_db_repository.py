from datetime import datetime, timedelta, timezone
from datetime import date

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.db import Base
from app.stocks.earnings.annual.db_repository import SqlAnnualEarningsRepository
from app.stocks.earnings.annual.entities import (
    AnnualEarnings,
    AnnualEarningsTimeline,
)
from app.stocks.earnings.annual.models import (
    StockAnnualEarningsRecord,
    StockRecord,
    get_or_create_stock,
)

_NOW = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        yield db


def repo(session) -> SqlAnnualEarningsRepository:
    return SqlAnnualEarningsRepository(session, now=lambda: _NOW)


def _reported(
    fy: int,
    eps: float,
    *,
    revenue_actual: float | None = None,
    net_income: float | None = None,
    eps_actual_consensus: float | None = None,
    fcf_per_share: float | None = None,
    ocf_per_share: float | None = None,
) -> AnnualEarnings:
    return AnnualEarnings(
        fiscal_year=fy,
        period_end=date(fy, 12, 31),
        eps_actual=eps,
        eps_estimate=None,
        revenue_actual=revenue_actual,
        revenue_estimate=None,
        net_income=net_income,
        eps_actual_consensus=eps_actual_consensus,
        fcf_per_share=fcf_per_share,
        ocf_per_share=ocf_per_share,
    )


def _upcoming(fy: int, eps_estimate: float, revenue: float | None) -> AnnualEarnings:
    return AnnualEarnings(
        fiscal_year=fy,
        period_end=date(fy, 12, 31),
        eps_actual=None,
        eps_estimate=eps_estimate,
        revenue_actual=None,
        revenue_estimate=revenue,
    )


def _timeline() -> AnnualEarningsTimeline:
    # Built out of order on purpose — the repository re-sorts to the canonical chronological
    # order on read.
    return AnnualEarningsTimeline(
        symbol="AAPL",
        years=(
            _reported(2024, 6.0, revenue_actual=400e9, net_income=100e9, eps_actual_consensus=6.4),
            _reported(2023, 5.5),
            _upcoming(2026, 7.0, 450e9),
            _upcoming(2025, 6.5, 420e9),
        ),
    )


def test_get_on_empty_table_is_a_miss(session):
    assert repo(session).get("AAPL") is None


def test_roundtrips_the_timeline(session):
    r = repo(session)
    r.upsert("AAPL", "Apple Inc.", _timeline())

    tl = r.get("AAPL")
    assert isinstance(tl, AnnualEarningsTimeline)
    # Canonical order: chronological — ascending by fiscal_year, oldest reported year through
    # furthest upcoming — regardless of the insert order.
    assert [y.fiscal_year for y in tl.years] == [2023, 2024, 2025, 2026]

    y2024 = next(y for y in tl.years if y.fiscal_year == 2024)
    assert y2024.eps_actual == 6.0
    assert y2024.revenue_actual == 400e9 and y2024.net_income == 100e9
    assert y2024.eps_actual_consensus == 6.4
    assert y2024.eps_estimate is None and y2024.revenue_estimate is None
    assert y2024.is_reported is True

    upcoming = tl.future[0]  # 2025, soonest upcoming
    assert upcoming.fiscal_year == 2025
    assert upcoming.eps_actual is None and upcoming.revenue_estimate == 420e9
    assert upcoming.revenue_actual is None and upcoming.net_income is None
    assert upcoming.eps_actual_consensus is None
    assert upcoming.is_reported is False


def test_upsert_stamps_the_fetch_time(session):
    # fetched_at isn't part of the read shape, but it's still written — the cron's
    # stalest-first refresh orders by it — so verify the stamp lands on the rows. SQLite hands
    # the timestamp back naive (Postgres keeps the zone); normalize to UTC.
    repo(session).upsert("AAPL", "Apple Inc.", _timeline())
    stamp = session.execute(select(StockAnnualEarningsRecord.fetched_at)).scalars().first()
    assert stamp.replace(tzinfo=timezone.utc) == _NOW


def test_upsert_replaces_the_whole_window(session):
    r = repo(session)
    r.upsert("AAPL", "Apple Inc.", _timeline())  # 4 years
    r.upsert(
        "AAPL",
        "Apple Inc.",
        AnnualEarningsTimeline("AAPL", (_reported(2025, 7.0),)),
    )  # now just 1

    tl = r.get("AAPL")
    assert [y.fiscal_year for y in tl.years] == [2025]
    rows = session.execute(
        select(func.count()).select_from(StockAnnualEarningsRecord)
    ).scalar_one()
    assert rows == 1  # old window cleared, not duplicated


def test_upsert_leaves_other_stocks_untouched(session):
    r = repo(session)
    r.upsert("AAPL", "Apple Inc.", _timeline())
    r.upsert("MSFT", "Microsoft", AnnualEarningsTimeline("MSFT", (_reported(2024, 11.0),)))

    r.upsert("AAPL", "Apple Inc.", AnnualEarningsTimeline("AAPL", (_reported(2025, 7.0),)))

    assert len(r.get("MSFT").years) == 1  # MSFT survived AAPL's rewrite


def test_creates_the_parent_stock_row(session):
    repo(session).upsert("AAPL", "Apple Inc.", _timeline())
    stock = session.execute(
        select(StockRecord).where(StockRecord.ticker == "AAPL")
    ).scalar_one()
    assert stock.name == "Apple Inc." and stock.id is not None


def test_fills_a_missing_name_but_never_clobbers_a_known_one(session):
    r = repo(session)
    r.upsert("AAPL", None, _timeline())
    assert (
        session.execute(
            select(StockRecord.name).where(StockRecord.ticker == "AAPL")
        ).scalar_one()
        is None
    )

    r.upsert("AAPL", "Apple Inc.", _timeline())
    r.upsert("AAPL", None, _timeline())  # a nameless refresh must not erase it
    assert (
        session.execute(
            select(StockRecord.name).where(StockRecord.ticker == "AAPL")
        ).scalar_one()
        == "Apple Inc."
    )


def _stock_growth(session, ticker: str = "AAPL"):
    return session.execute(
        select(StockRecord.revenue_growth_yoy, StockRecord.eps_growth_yoy).where(
            StockRecord.ticker == ticker
        )
    ).one()


def test_upsert_writes_the_latest_trailing_yoy_snapshot(session):
    # Two reported years with revenue + consensus EPS, so both trailing legs compute.
    repo(session).upsert(
        "AAPL",
        "Apple Inc.",
        AnnualEarningsTimeline(
            "AAPL",
            (
                _reported(2023, 5.0, revenue_actual=300e9, eps_actual_consensus=5.0),
                _reported(2024, 6.0, revenue_actual=360e9, eps_actual_consensus=6.0),
                _upcoming(2025, 6.5, 400e9),  # ignored: not reported
            ),
        ),
    )
    rev, eps = _stock_growth(session)
    # revenue (360-300)/300 = +20%; eps on the consensus basis (6.0-5.0)/5.0 = +20%
    assert rev == 20.0 and eps == 20.0


def _stock_cash(session, ticker: str = "AAPL"):
    return session.execute(
        select(
            StockRecord.fcf_per_share,
            StockRecord.ocf_per_share,
            StockRecord.fcf_growth_yoy,
        ).where(StockRecord.ticker == ticker)
    ).one()


def test_upsert_persists_cash_per_year_and_writes_the_anchor_snapshot(session):
    # Two reported years carrying per-share cash: the rows persist it, and the anchor gets the
    # newest year's figures plus the trailing FCF/share growth.
    r = repo(session)
    r.upsert(
        "AAPL",
        "Apple Inc.",
        AnnualEarningsTimeline(
            "AAPL",
            (
                _reported(2023, 5.5, fcf_per_share=2.0, ocf_per_share=3.0),
                _reported(2024, 6.0, fcf_per_share=2.5, ocf_per_share=3.6),
                _upcoming(2025, 6.5, 420e9),  # upcoming: no cash
            ),
        ),
    )
    # Per-year rows round-trip the cash figures.
    by_year = {y.fiscal_year: y for y in r.get("AAPL").past}
    assert (by_year[2024].fcf_per_share, by_year[2024].ocf_per_share) == (2.5, 3.6)
    assert (by_year[2023].fcf_per_share, by_year[2023].ocf_per_share) == (2.0, 3.0)
    # Anchor snapshot: newest reported year's per-share cash + trailing FCF/share growth.
    fcf_ps, ocf_ps, fcf_growth = _stock_cash(session)
    assert fcf_ps == 2.5
    assert ocf_ps == 3.6
    assert fcf_growth == 25.0  # (2.5 - 2.0) / 2.0 * 100


def test_upsert_overwrites_the_growth_snapshot_each_refresh(session):
    # Unlike the fill-once name/exchange, the snapshot moves: a later refresh whose newest
    # reported year rolled forward replaces the stored pair.
    r = repo(session)
    r.upsert(
        "AAPL",
        "Apple Inc.",
        AnnualEarningsTimeline(
            "AAPL",
            (
                _reported(2023, 5.0, revenue_actual=300e9, eps_actual_consensus=5.0),
                _reported(2024, 6.0, revenue_actual=360e9, eps_actual_consensus=6.0),
            ),
        ),
    )
    r.upsert(
        "AAPL",
        "Apple Inc.",
        AnnualEarningsTimeline(
            "AAPL",
            (
                _reported(2024, 6.0, revenue_actual=360e9, eps_actual_consensus=6.0),
                _reported(2025, 9.0, revenue_actual=450e9, eps_actual_consensus=9.0),
            ),
        ),
    )
    rev, eps = _stock_growth(session)
    # now 2025 vs 2024: revenue (450-360)/360 = +25%; eps (9-6)/6 = +50%
    assert rev == 25.0 and eps == 50.0


def test_growth_snapshot_is_null_without_two_reported_years(session):
    repo(session).upsert(
        "AAPL",
        "Apple Inc.",
        AnnualEarningsTimeline(
            "AAPL",
            (
                _reported(2024, 6.0, revenue_actual=360e9, eps_actual_consensus=6.0),
                _upcoming(2025, 6.5, 400e9),
            ),
        ),
    )
    rev, eps = _stock_growth(session)
    assert rev is None and eps is None


def test_eps_growth_snapshot_is_null_when_consensus_basis_missing(session):
    # Revenue still computes; EPS needs the consensus basis on both years, and it's
    # best-effort — here the prior year never filled it, so EPS growth is null.
    repo(session).upsert(
        "AAPL",
        "Apple Inc.",
        AnnualEarningsTimeline(
            "AAPL",
            (
                _reported(2023, 5.0, revenue_actual=300e9),  # no eps_actual_consensus
                _reported(2024, 6.0, revenue_actual=360e9, eps_actual_consensus=6.0),
            ),
        ),
    )
    rev, eps = _stock_growth(session)
    assert rev == 20.0 and eps is None


def test_eps_growth_snapshot_is_null_off_a_loss_year(session):
    # A non-positive prior-year EPS makes the percentage meaningless (guard: prior > 0).
    repo(session).upsert(
        "AAPL",
        "Apple Inc.",
        AnnualEarningsTimeline(
            "AAPL",
            (
                _reported(2023, -1.0, revenue_actual=300e9, eps_actual_consensus=-1.0),
                _reported(2024, 6.0, revenue_actual=360e9, eps_actual_consensus=6.0),
            ),
        ),
    )
    rev, eps = _stock_growth(session)
    assert rev == 20.0 and eps is None


def _stock_forward_growth(session, ticker: str = "AAPL"):
    return session.execute(
        select(
            StockRecord.forward_revenue_growth_yoy, StockRecord.forward_eps_growth_yoy
        ).where(StockRecord.ticker == ticker)
    ).one()


def test_upsert_writes_the_forward_yoy_snapshot(session):
    # Two upcoming years with EPS + revenue estimates, so both forward legs compute (FY1→FY2) —
    # the forward mirror of the trailing snapshot, off the consensus estimates.
    repo(session).upsert(
        "AAPL",
        "Apple Inc.",
        AnnualEarningsTimeline(
            "AAPL",
            (
                _reported(2024, 6.0, revenue_actual=360e9, eps_actual_consensus=6.0),
                _upcoming(2025, 6.0, 400e9),  # FY1
                _upcoming(2026, 7.5, 500e9),  # FY2
            ),
        ),
    )
    rev, eps = _stock_forward_growth(session)
    # revenue (500-400)/400 = +25%; eps (7.5-6.0)/6.0 = +25%
    assert rev == 25.0 and eps == 25.0


def test_forward_growth_snapshot_is_null_without_two_upcoming_years(session):
    # Only one upcoming year (Yahoo's common case) — no FY2, so forward growth can't compute,
    # even though there's plenty of reported history for the trailing snapshot.
    repo(session).upsert(
        "AAPL",
        "Apple Inc.",
        AnnualEarningsTimeline(
            "AAPL",
            (
                _reported(2023, 5.0, revenue_actual=300e9, eps_actual_consensus=5.0),
                _reported(2024, 6.0, revenue_actual=360e9, eps_actual_consensus=6.0),
                _upcoming(2025, 6.5, 400e9),  # lone FY1
            ),
        ),
    )
    assert _stock_forward_growth(session) == (None, None)


def test_forward_eps_growth_snapshot_is_null_off_a_nonpositive_first_year(session):
    # A non-positive FY1 EPS estimate makes the percentage meaningless (guard: first year > 0).
    # Revenue still computes off the two positive revenue estimates.
    repo(session).upsert(
        "AAPL",
        "Apple Inc.",
        AnnualEarningsTimeline(
            "AAPL",
            (
                _upcoming(2025, -0.5, 400e9),  # FY1 expected loss
                _upcoming(2026, 2.0, 500e9),  # FY2
            ),
        ),
    )
    rev, eps = _stock_forward_growth(session)
    assert rev == 25.0 and eps is None


def test_refresh_targets_orders_stalest_first_and_carries_the_name(session):
    # refresh_targets wraps the stalest-first query the cron walks; a stock's rows share a
    # fetch stamp, so an older upsert sorts ahead of a newer one, each paired with its name.
    older = SqlAnnualEarningsRepository(session, now=lambda: _NOW - timedelta(days=10))
    newer = SqlAnnualEarningsRepository(session, now=lambda: _NOW)
    older.upsert(
        "MSFT",
        "Microsoft",
        AnnualEarningsTimeline("MSFT", (_reported(2024, 11.0),)),
    )
    newer.upsert("AAPL", "Apple Inc.", _timeline())

    targets = newer.refresh_targets(10)
    assert [t.symbol for t in targets] == ["MSFT", "AAPL"]  # stalest first
    assert targets[0] == ("MSFT", "Microsoft")  # RefreshTarget carries the stored name
    assert newer.refresh_targets(1) == [("MSFT", "Microsoft")]  # limit respected


def test_refresh_targets_seeds_uncached_anchor_stocks_first(session):
    # A stock in the anchor with no year rows yet (e.g. added by the universe sync) is a
    # *seed* target — returned ahead of any cached stock so a sweep fills new coverage first.
    r = repo(session)
    r.upsert("MSFT", "Microsoft", AnnualEarningsTimeline("MSFT", (_reported(2024, 11.0),)))
    get_or_create_stock(session, "NEWCO", "New Co")  # anchor only, never fetched
    session.commit()

    targets = r.refresh_targets(None)  # None => every anchor stock
    assert [t.symbol for t in targets] == ["NEWCO", "MSFT"]  # un-cached seeded first
    assert dict(targets)["NEWCO"] == "New Co"  # carries the anchor name
