from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.db import Base
from app.stocks.ai.analysis.db_repository import SqlInvestmentAnalysisCache
from app.stocks.ai.analysis.models import AnalysisCacheRecord
from app.stocks.ai.analysis.entities import Confidence, InvestmentAnalysis, Recommendation

_NOW = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        yield db


def an_analysis(symbol: str = "AAPL", **overrides) -> InvestmentAnalysis:
    base = dict(
        symbol=symbol,
        recommendation=Recommendation.BUY,
        confidence=Confidence.HIGH,
        thesis="Solid franchise trading a touch rich.",
        strengths=("Consistent beats", "Strong margins"),
        risks=("Rich multiple",),
        model="claude-haiku-4-5",
        generated_at=_NOW,
    )
    base.update(overrides)
    return InvestmentAnalysis(**base)


def test_get_on_empty_is_a_miss(session):
    assert SqlInvestmentAnalysisCache(session, "stock").get("AAPL") is None


def test_put_then_get_round_trips_every_field(session):
    cache = SqlInvestmentAnalysisCache(session, "stock")
    cache.put(an_analysis())
    got = cache.get("AAPL")
    assert got == an_analysis()  # frozen dataclass equality covers every field
    # And the timestamp comes back tz-aware UTC even though SQLite has no tz type.
    assert got.generated_at.tzinfo is not None
    assert got.generated_at == _NOW


def test_kind_isolates_a_shared_ticker(session):
    # A stock and a fund can share a ticker; the two caches must not collide.
    SqlInvestmentAnalysisCache(session, "stock").put(an_analysis())
    assert SqlInvestmentAnalysisCache(session, "etf").get("AAPL") is None
    assert SqlInvestmentAnalysisCache(session, "stock").get("AAPL") is not None


def test_put_overwrites_in_place(session):
    cache = SqlInvestmentAnalysisCache(session, "stock")
    cache.put(an_analysis(recommendation=Recommendation.BUY))
    cache.put(an_analysis(recommendation=Recommendation.SELL, thesis="Turned sour."))
    got = cache.get("AAPL")
    assert got.recommendation is Recommendation.SELL
    assert got.thesis == "Turned sour."
    # One row per (kind, symbol) — the second put replaced, not appended.
    count = session.execute(
        select(func.count()).select_from(AnalysisCacheRecord)
    ).scalar_one()
    assert count == 1


def test_corrupt_enum_row_is_treated_as_a_miss(session):
    # A row written by an older build could carry an enum value this build no longer
    # knows; get must degrade to a miss (so the caller regenerates), never raise.
    session.add(
        AnalysisCacheRecord(
            kind="stock",
            symbol="AAPL",
            recommendation="mega_buy",  # not a Recommendation value
            confidence="high",
            thesis="x",
            strengths=[],
            risks=[],
            model="m",
            generated_at=_NOW,
        )
    )
    session.commit()
    assert SqlInvestmentAnalysisCache(session, "stock").get("AAPL") is None


def test_get_is_best_effort_on_a_broken_session():
    # A read failure (here: a session with no schema at all) is a miss, not a raise.
    engine = create_engine("sqlite:///:memory:")  # no create_all -> table missing
    with Session(engine) as db:
        assert SqlInvestmentAnalysisCache(db, "stock").get("AAPL") is None


def test_put_is_best_effort_and_never_raises():
    # A write failure (no schema) must be swallowed — the caller already has its answer.
    engine = create_engine("sqlite:///:memory:")
    with Session(engine) as db:
        SqlInvestmentAnalysisCache(db, "stock").put(an_analysis())  # no exception


def test_naive_stored_stamp_reads_back_as_utc(session):
    # Belt-and-braces for the tz re-attachment: store a naive stamp directly and
    # confirm get hands back an aware UTC datetime.
    session.add(
        AnalysisCacheRecord(
            kind="stock",
            symbol="MSFT",
            recommendation="hold",
            confidence="medium",
            thesis="x",
            strengths=[],
            risks=[],
            model="m",
            generated_at=datetime(2026, 7, 1, 12, 0),  # naive
        )
    )
    session.commit()
    got = SqlInvestmentAnalysisCache(session, "stock").get("MSFT")
    assert got.generated_at == _NOW
