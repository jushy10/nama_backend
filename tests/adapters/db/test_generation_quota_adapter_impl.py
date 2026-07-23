from datetime import date

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.adapters.db.generation_quota_adapter_impl import GenerationQuotaAdapterImpl
from app.db import Base
from app.domains.research.quota.models import GenerationUsageRecord

_TODAY = date(2026, 7, 23)


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def _quota(session, limit=3, pool="analysis", today=_TODAY):
    return GenerationQuotaAdapterImpl(session, pool, limit, today=lambda: today)


def test_consumes_up_to_the_limit_then_denies(session):
    quota = _quota(session, limit=3)
    assert [quota.try_consume("1.2.3.4") for _ in range(5)] == [
        True,
        True,
        True,
        False,
        False,
    ]
    row = session.execute(select(GenerationUsageRecord)).scalar_one()
    assert row.count == 3  # a denied attempt consumes nothing


def test_each_client_gets_its_own_budget(session):
    quota = _quota(session, limit=1)
    assert quota.try_consume("1.2.3.4") is True
    assert quota.try_consume("1.2.3.4") is False
    assert quota.try_consume("5.6.7.8") is True


def test_pools_are_independent(session):
    analysis = _quota(session, limit=1, pool="analysis")
    research = _quota(session, limit=1, pool="research")
    assert analysis.try_consume("1.2.3.4") is True
    assert analysis.try_consume("1.2.3.4") is False
    # Spending the analysis pool leaves the research pool untouched.
    assert research.try_consume("1.2.3.4") is True


def test_budget_resets_on_a_new_day(session):
    assert _quota(session, limit=1, today=date(2026, 7, 22)).try_consume("1.2.3.4") is True
    assert _quota(session, limit=1, today=date(2026, 7, 22)).try_consume("1.2.3.4") is False
    assert _quota(session, limit=1, today=date(2026, 7, 23)).try_consume("1.2.3.4") is True


def test_zero_limit_denies_without_writing(session):
    quota = _quota(session, limit=0)
    assert quota.try_consume("1.2.3.4") is False
    assert session.execute(select(GenerationUsageRecord)).first() is None


def test_oversized_client_key_is_truncated_not_an_error(session):
    quota = _quota(session, limit=1)
    assert quota.try_consume("x" * 500) is True
    row = session.execute(select(GenerationUsageRecord)).scalar_one()
    assert len(row.client_key) == 64


def test_fails_open_when_the_table_is_missing():
    # A broken counter (e.g. migrations not run) must let the request through, never 500.
    engine = create_engine("sqlite:///:memory:")  # no create_all
    with Session(engine) as session:
        assert GenerationQuotaAdapterImpl(session, "analysis", 1).try_consume("1.2.3.4") is True
