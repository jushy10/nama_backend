from datetime import date

import pytest

from app.domains.research.rate_limit_quota.interfaces import QuotaRepositoryAdapter
from app.domains.research.rate_limit_quota.use_cases import ConsumeGenerationQuota
from app.domains.shared.exceptions import QuotaExceeded

_TODAY = date(2026, 7, 23)


class _FakeRepo(QuotaRepositoryAdapter):
    def __init__(self, *, allow=True) -> None:
        self._allow = allow
        self.calls: list[tuple] = []

    def try_consume(self, pool, client_key, day, limit) -> bool:
        self.calls.append((pool, client_key, day, limit))
        return self._allow


def _use_case(repo, *, pool="analysis", limit=10):
    return ConsumeGenerationQuota(repo, pool=pool, daily_limit=limit, today=lambda: _TODAY)


def test_consumes_with_the_pool_day_and_limit():
    repo = _FakeRepo()
    _use_case(repo, pool="research", limit=5).execute("1.2.3.4")
    assert repo.calls == [("research", "1.2.3.4", _TODAY, 5)]


def test_an_exhausted_budget_raises_quota_exceeded():
    with pytest.raises(QuotaExceeded):
        _use_case(_FakeRepo(allow=False)).execute("1.2.3.4")


def test_no_client_id_is_a_no_op():
    # Non-HTTP callers (tests, crons) carry no client identity.
    repo = _FakeRepo(allow=False)  # would raise if ever consulted
    _use_case(repo).execute(None)
    assert repo.calls == []


def test_oversized_client_id_is_truncated_to_the_column_width():
    repo = _FakeRepo()
    _use_case(repo).execute("x" * 500)
    assert len(repo.calls[0][1]) == 64
