from datetime import date, datetime, timezone

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.db import Base
from app.stocks.brief.db_repository import SqlMarketBriefRepository
from app.stocks.brief.entities import (
    BriefTone,
    MarketBrief,
    MarketBriefSection,
)
from app.stocks.brief.models import MarketBriefRecord

_NOW = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        yield db


def repo(session) -> SqlMarketBriefRepository:
    return SqlMarketBriefRepository(session)


def _brief(day: date, *, summary="A calm day.", tone=BriefTone.MIXED) -> MarketBrief:
    return MarketBrief(
        brief_date=day,
        generated_at=_NOW,
        tone=tone,
        summary=summary,
        sections=(
            MarketBriefSection("Overview", "Markets were quiet."),
            MarketBriefSection("Sectors", "Little rotation today."),
        ),
        model="test-model",
    )


def test_get_on_empty_table_is_a_miss(session):
    assert repo(session).get(date(2026, 7, 14)) is None


def test_latest_on_empty_table_is_a_miss(session):
    assert repo(session).latest() is None


def test_roundtrips_a_brief(session):
    r = repo(session)
    r.upsert(_brief(date(2026, 7, 14), summary="Tech led a broad rally.", tone=BriefTone.RISK_ON))

    got = r.get(date(2026, 7, 14))
    assert got is not None
    assert got.brief_date == date(2026, 7, 14)
    assert got.tone is BriefTone.RISK_ON
    assert got.summary == "Tech led a broad rally."
    assert [s.heading for s in got.sections] == ["Overview", "Sectors"]
    assert got.sections[1].body == "Little rotation today."
    assert got.model == "test-model"


def test_latest_returns_the_newest_date(session):
    r = repo(session)
    r.upsert(_brief(date(2026, 7, 12)))
    r.upsert(_brief(date(2026, 7, 14), summary="Newest."))
    r.upsert(_brief(date(2026, 7, 13)))

    latest = r.latest()
    assert latest is not None
    assert latest.brief_date == date(2026, 7, 14)
    assert latest.summary == "Newest."


def test_upsert_overwrites_the_same_date(session):
    r = repo(session)
    r.upsert(_brief(date(2026, 7, 14), summary="First take."))
    r.upsert(_brief(date(2026, 7, 14), summary="Corrected take.", tone=BriefTone.RISK_OFF))

    # One row for the date (overwritten, not duplicated).
    count = session.execute(select(func.count()).select_from(MarketBriefRecord)).scalar_one()
    assert count == 1
    got = r.get(date(2026, 7, 14))
    assert got.summary == "Corrected take."
    assert got.tone is BriefTone.RISK_OFF


def test_reads_are_defensive_about_a_bad_stored_tone(session):
    # A stray tone slug on the row must not crash a read — it falls back to mixed.
    r = repo(session)
    r.upsert(_brief(date(2026, 7, 14)))
    row = session.execute(select(MarketBriefRecord)).scalar_one()
    row.tone = "nonsense"
    session.commit()
    assert r.get(date(2026, 7, 14)).tone is BriefTone.MIXED
