from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.db import Base
from app.stocks.ai.analysis.entities import (
    Confidence,
    Recommendation,
    ScorecardSection,
    SectionMetric,
    SectionStance,
    StockScorecard,
)
from app.stocks.ai.analysis.models import AnalysisCacheRecord
from app.stocks.ai.analysis.db_stock_scorecard_cache import SqlStockScorecardCache

_NOW = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        yield db


def a_scorecard(symbol: str = "AAPL", **overrides) -> StockScorecard:
    base = dict(
        symbol=symbol,
        recommendation=Recommendation.BUY,
        confidence=Confidence.HIGH,
        thesis="Solid franchise trading a touch rich.",
        sections=(
            ScorecardSection(
                key="business_quality",
                title="Business quality",
                stance=SectionStance.POSITIVE,
                label="Exceptional",
                summary="Highly profitable and cash-generative.",
                metrics=(
                    SectionMetric("Net margin", "25.00%"),
                    SectionMetric("Return on equity", "147.40%"),
                ),
            ),
            ScorecardSection(
                key="valuation",
                title="Valuation",
                stance=SectionStance.NEGATIVE,
                label="Expensive",
                summary="Priced well above its peers.",
                metrics=(SectionMetric("P/E (trailing)", "28.50"),),
            ),
        ),
        model="claude-haiku-4-5",
        generated_at=_NOW,
    )
    base.update(overrides)
    return StockScorecard(**base)


def test_get_on_empty_is_a_miss(session):
    assert SqlStockScorecardCache(session).get("AAPL") is None


def test_put_then_get_round_trips_every_field(session):
    cache = SqlStockScorecardCache(session)
    cache.put(a_scorecard())
    got = cache.get("AAPL")
    assert got == a_scorecard()  # frozen dataclass equality covers sections + chips
    # And the timestamp comes back tz-aware UTC even though SQLite has no tz type.
    assert got.generated_at.tzinfo is not None
    assert got.generated_at == _NOW


def test_kind_isolates_from_the_etf_cache(session):
    # The scorecard (kind="stock") shares the table with the ETF analysis (kind="etf");
    # a read under the other kind must miss.
    SqlStockScorecardCache(session, "stock").put(a_scorecard())
    assert SqlStockScorecardCache(session, "etf").get("AAPL") is None
    assert SqlStockScorecardCache(session, "stock").get("AAPL") is not None


def test_put_overwrites_in_place(session):
    cache = SqlStockScorecardCache(session)
    cache.put(a_scorecard(recommendation=Recommendation.BUY))
    cache.put(a_scorecard(recommendation=Recommendation.SELL, thesis="Turned sour."))
    got = cache.get("AAPL")
    assert got.recommendation is Recommendation.SELL
    assert got.thesis == "Turned sour."
    # One row per (kind, symbol) — the second put replaced, not appended.
    count = session.execute(
        select(func.count()).select_from(AnalysisCacheRecord)
    ).scalar_one()
    assert count == 1


def test_corrupt_enum_row_is_treated_as_a_miss(session):
    session.add(
        AnalysisCacheRecord(
            kind="stock",
            symbol="AAPL",
            recommendation="mega_buy",  # not a Recommendation value
            confidence="high",
            thesis="x",
            sections=[],
            model="m",
            generated_at=_NOW,
        )
    )
    session.commit()
    assert SqlStockScorecardCache(session).get("AAPL") is None


def test_null_sections_column_reads_as_no_sections(session):
    # A row with a null sections column (e.g. an ETF-shaped row, or a pre-migration
    # remnant) reads back with an empty section tuple, not a crash.
    session.add(
        AnalysisCacheRecord(
            kind="stock",
            symbol="MSFT",
            recommendation="hold",
            confidence="medium",
            thesis="x",
            sections=None,
            model="m",
            generated_at=_NOW,
        )
    )
    session.commit()
    got = SqlStockScorecardCache(session).get("MSFT")
    assert got is not None
    assert got.sections == ()


def test_malformed_section_entry_is_skipped(session):
    # A section missing fields / carrying an off-enum stance degrades rather than
    # sinking the read: the good section survives, the junk one is dropped or neutered.
    session.add(
        AnalysisCacheRecord(
            kind="stock",
            symbol="NVDA",
            recommendation="buy",
            confidence="high",
            thesis="x",
            sections=[
                {"key": "valuation", "stance": "off", "label": "L", "summary": "S"},
                "not-a-dict",
            ],
            model="m",
            generated_at=_NOW,
        )
    )
    session.commit()
    got = SqlStockScorecardCache(session).get("NVDA")
    assert [s.key for s in got.sections] == ["valuation"]  # the junk entry dropped
    assert got.sections[0].stance is SectionStance.NEUTRAL  # off-enum stance neutered


def test_get_is_best_effort_on_a_broken_session():
    engine = create_engine("sqlite:///:memory:")  # no create_all -> table missing
    with Session(engine) as db:
        assert SqlStockScorecardCache(db).get("AAPL") is None


def test_put_is_best_effort_and_never_raises():
    engine = create_engine("sqlite:///:memory:")
    with Session(engine) as db:
        SqlStockScorecardCache(db).put(a_scorecard())  # no exception
