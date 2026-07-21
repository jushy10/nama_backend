from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.db import Base
from app.stocks.analysis.ai_analysis_cache_repository import (
    earnings_analysis_cache,
    fundamentals_analysis_cache,
    market_summary_cache,
    ratings_analysis_cache,
    sector_analysis_cache,
)
from app.stocks.analysis.entities import (
    Confidence,
    EarningsAnalysis,
    EarningsTrend,
    FundamentalsAnalysis,
    FundamentalsVerdict,
    MarketIndexReturn,
    MarketPeriod,
    MarketPeriodHighlight,
    MarketSummary,
    MarketTone,
    RatingsAnalysis,
    RatingsVerdict,
    SectorAnalysis,
    SectorHighlight,
)
from app.stocks.analysis.models import AnalysisCacheRecord

_NOW = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)
_MARKET = "_MARKET_"  # the sentinel key the market-wide reads use


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        yield db


def an_earnings(symbol="AAPL", **o) -> EarningsAnalysis:
    base = dict(
        symbol=symbol,
        summary="Earnings are accelerating on strong demand.",
        trend=EarningsTrend.ACCELERATING,
        highlights=("Beat four straight quarters", "Revenue up 12% YoY"),
        model="claude-haiku-4-5",
        generated_at=_NOW,
    )
    base.update(o)
    return EarningsAnalysis(**base)


def a_ratings(symbol="NVDA", **o) -> RatingsAnalysis:
    base = dict(
        symbol=symbol,
        verdict=RatingsVerdict.BULLISH,
        confidence=Confidence.HIGH,
        summary="Analysts are overwhelmingly positive.",
        findings=("95% rate it buy or better", "Targets rising"),
        model="claude-haiku-4-5",
        generated_at=_NOW,
    )
    base.update(o)
    return RatingsAnalysis(**base)


def a_fundamentals(symbol="MSFT", **o) -> FundamentalsAnalysis:
    base = dict(
        symbol=symbol,
        verdict=FundamentalsVerdict.STRONG,
        confidence=Confidence.MEDIUM,
        summary="Fat margins, steady growth, a fair multiple.",
        findings=("Net margin 36%", "Forward P/E in line with peers"),
        model="claude-haiku-4-5",
        generated_at=_NOW,
    )
    base.update(o)
    return FundamentalsAnalysis(**base)


def a_sector(**o) -> SectorAnalysis:
    base = dict(
        summary="Growth-sensitive corners led while defensives lagged.",
        tone=MarketTone.RISK_ON,
        leaders=(SectorHighlight("Technology", "XLK", 1.8, "Chips powered the tape."),),
        laggards=(SectorHighlight("Utilities", "XLU", -0.9, "Safe corners sold off."),),
        model="claude-opus-4-8",
        generated_at=_NOW,
    )
    base.update(o)
    return SectorAnalysis(**base)


def a_market(**o) -> MarketSummary:
    base = dict(
        summary="The market has climbed over the year, easing lately.",
        tone=MarketTone.RISK_ON,
        periods=(
            MarketPeriodHighlight(
                MarketPeriod.YEAR,
                "Both indexes are well up.",
                (
                    MarketIndexReturn("S&P 500", "SPY", 18.4),
                    MarketIndexReturn("Nasdaq", "QQQ", 24.1),
                ),
            ),
            # An empty-index period exercises the nested-list edge (round-trips to ()).
            MarketPeriodHighlight(MarketPeriod.WEEK, "A slight pullback.", ()),
        ),
        model="claude-opus-4-8",
        generated_at=_NOW,
    )
    base.update(o)
    return MarketSummary(**base)


# --- round-trips (frozen-dataclass equality covers every field) --------------------


def test_earnings_round_trips_every_field(session):
    cache = earnings_analysis_cache(session)
    cache.put("AAPL", an_earnings())
    got = cache.get("AAPL")
    assert got == an_earnings()
    assert got.generated_at.tzinfo is not None  # aware UTC despite SQLite's no-tz type


def test_ratings_round_trips_every_field(session):
    cache = ratings_analysis_cache(session)
    cache.put("NVDA", a_ratings())
    assert cache.get("NVDA") == a_ratings()


def test_fundamentals_round_trips_every_field(session):
    cache = fundamentals_analysis_cache(session)
    cache.put("MSFT", a_fundamentals())
    assert cache.get("MSFT") == a_fundamentals()


def test_sector_round_trips_nested_highlights(session):
    cache = sector_analysis_cache(session)
    cache.put(_MARKET, a_sector())
    assert cache.get(_MARKET) == a_sector()


def test_market_round_trips_nested_periods(session):
    cache = market_summary_cache(session)
    cache.put(_MARKET, a_market())
    assert cache.get(_MARKET) == a_market()


def test_earnings_writes_generic_columns_and_leaves_stock_columns_null(session):
    earnings_analysis_cache(session).put("AAPL", an_earnings())
    row = session.execute(select(AnalysisCacheRecord)).scalar_one()
    assert row.kind == "earnings"
    assert row.verdict == "accelerating"
    assert row.findings == ["Beat four straight quarters", "Revenue up 12% YoY"]
    assert row.thesis == an_earnings().summary
    # The stock/ETF-only columns are left null (they were relaxed to nullable in 0030).
    assert row.recommendation is None
    assert row.confidence is None
    assert row.strengths is None
    assert row.risks is None
    assert row.sections is None
    assert row.details is None


def test_ratings_reuses_the_confidence_column(session):
    ratings_analysis_cache(session).put("NVDA", a_ratings())
    row = session.execute(select(AnalysisCacheRecord)).scalar_one()
    assert row.verdict == "bullish"
    assert row.confidence == "high"  # ratings reuses the existing confidence column


def test_sector_stores_leaders_and_laggards_under_details(session):
    sector_analysis_cache(session).put(_MARKET, a_sector())
    row = session.execute(select(AnalysisCacheRecord)).scalar_one()
    assert row.symbol == _MARKET
    assert set(row.details) == {"leaders", "laggards"}
    assert row.details["leaders"][0]["symbol"] == "XLK"


def test_kind_isolates_a_shared_symbol(session):
    # earnings and ratings can both cover a symbol; the two kinds must not collide.
    earnings_analysis_cache(session).put("AAPL", an_earnings())
    assert ratings_analysis_cache(session).get("AAPL") is None
    assert earnings_analysis_cache(session).get("AAPL") is not None


def test_kind_isolates_the_shared_market_sentinel(session):
    # sector and market share the sentinel key, told apart only by kind.
    sector_analysis_cache(session).put(_MARKET, a_sector())
    assert market_summary_cache(session).get(_MARKET) is None
    assert sector_analysis_cache(session).get(_MARKET) is not None


def test_put_overwrites_in_place(session):
    cache = earnings_analysis_cache(session)
    cache.put("AAPL", an_earnings(trend=EarningsTrend.ACCELERATING))
    cache.put("AAPL", an_earnings(trend=EarningsTrend.SLOWING, summary="Cooling now."))
    got = cache.get("AAPL")
    assert got.trend is EarningsTrend.SLOWING
    assert got.summary == "Cooling now."
    count = session.execute(
        select(func.count()).select_from(AnalysisCacheRecord)
    ).scalar_one()
    assert count == 1  # one row per (kind, key) — the second put replaced, not appended


def test_get_on_empty_is_a_miss(session):
    assert earnings_analysis_cache(session).get("AAPL") is None


def test_corrupt_enum_row_is_treated_as_a_miss(session):
    # A row whose stored enum this build no longer knows degrades to a miss, never a raise.
    session.add(
        AnalysisCacheRecord(
            kind="earnings",
            symbol="AAPL",
            thesis="x",
            verdict="skyrocketing",  # not an EarningsTrend value
            findings=[],
            model="m",
            generated_at=_NOW,
        )
    )
    session.commit()
    assert earnings_analysis_cache(session).get("AAPL") is None


def test_naive_stored_stamp_reads_back_as_utc(session):
    session.add(
        AnalysisCacheRecord(
            kind="earnings",
            symbol="MSFT",
            thesis="x",
            verdict="steady",
            findings=["ok"],
            model="m",
            generated_at=datetime(2026, 7, 1, 12, 0),  # naive
        )
    )
    session.commit()
    got = earnings_analysis_cache(session).get("MSFT")
    assert got.generated_at == _NOW


def test_reads_and_writes_are_best_effort_on_a_broken_session():
    engine = create_engine("sqlite:///:memory:")  # no create_all -> table missing
    with Session(engine) as db:
        assert earnings_analysis_cache(db).get("AAPL") is None  # miss, not a raise
        earnings_analysis_cache(db).put("AAPL", an_earnings())  # swallowed, no raise
