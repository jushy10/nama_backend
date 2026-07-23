from datetime import date

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.db import Base
from app.domains.research.rate_limit_quota.models import GenerationUsageRecord
from app.domains.research.rate_limit_quota.db_repository import DbQuotaRepository

_TODAY = date(2026, 7, 23)


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def _consume(session, client="1.2.3.4", *, limit=3, pool="analysis", day=_TODAY) -> bool:
    return DbQuotaRepository(session).try_consume(pool, client, day, limit)


def test_consumes_up_to_the_limit_then_denies(session):
    assert [_consume(session, limit=3) for _ in range(5)] == [
        True,
        True,
        True,
        False,
        False,
    ]
    row = session.execute(select(GenerationUsageRecord)).scalar_one()
    assert row.count == 3  # a denied attempt consumes nothing


def test_each_client_gets_its_own_budget(session):
    assert _consume(session, "1.2.3.4", limit=1) is True
    assert _consume(session, "1.2.3.4", limit=1) is False
    assert _consume(session, "5.6.7.8", limit=1) is True


def test_pools_are_independent(session):
    assert _consume(session, limit=1, pool="analysis") is True
    assert _consume(session, limit=1, pool="analysis") is False
    # Spending the analysis pool leaves the research pool untouched.
    assert _consume(session, limit=1, pool="research") is True


def test_budget_resets_on_a_new_day(session):
    assert _consume(session, limit=1, day=date(2026, 7, 22)) is True
    assert _consume(session, limit=1, day=date(2026, 7, 22)) is False
    assert _consume(session, limit=1, day=date(2026, 7, 23)) is True


def test_zero_limit_denies_without_writing(session):
    assert _consume(session, limit=0) is False
    assert session.execute(select(GenerationUsageRecord)).first() is None


def test_fails_open_when_the_table_is_missing():
    # A broken counter (e.g. migrations not run) must let the request through, never 500.
    engine = create_engine("sqlite:///:memory:")  # no create_all
    with Session(engine) as session:
        assert (
            DbQuotaRepository(session).try_consume("analysis", "1.2.3.4", _TODAY, 1)
            is True
        )
