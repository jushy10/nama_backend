"""Tests for the institutional-ownership use cases + the entity rules they lean on.

Offline: hand-written fakes for the provider and repository ports, so this exercises only the
orchestration — symbol normalization and pass-through on the read side; which targets are refreshed,
failure/empty handling, and the per-run limit on the sync side — plus the entity rules the slice's
responses derive (is_buyer/is_seller, share_change/value_change, the latest-snapshot flow rollup,
is_empty), independent of yfinance or the DB.
"""

from datetime import date

import pytest

from app.stocks.exceptions import StockDataUnavailable, StockNotFound
from app.stocks.institutional_ownership.entities import (
    HOLDER_TYPE_INSTITUTION,
    HOLDER_TYPE_MUTUAL_FUND,
    InstitutionalHolder,
    InstitutionalOwnership,
    OwnershipBreakdown,
)
from app.stocks.institutional_ownership.ports import InstitutionalOwnershipProvider
from app.stocks.institutional_ownership.repository import (
    InstitutionalOwnershipRepository,
    RefreshTarget,
)
from app.stocks.institutional_ownership.use_cases import (
    GetInstitutionalOwnership,
    InstitutionalOwnershipSyncReport,
    SyncInstitutionalOwnership,
)

_Q2 = date(2026, 6, 30)
_Q1 = date(2026, 3, 31)


def _holder(
    holder="Vanguard Group Inc",
    *,
    holder_type=HOLDER_TYPE_INSTITUTION,
    reported=_Q2,
    shares=1000.0,
    value=100000.0,
    pct_held=8.9,
    pct_change=None,
) -> InstitutionalHolder:
    return InstitutionalHolder(
        holder=holder,
        holder_type=holder_type,
        date_reported=reported,
        shares=shares,
        value=value,
        pct_held=pct_held,
        pct_change=pct_change,
    )


def _ownership(*holders, symbol="AAPL", breakdown=None) -> InstitutionalOwnership:
    return InstitutionalOwnership(
        symbol=symbol, breakdown=breakdown, holders=tuple(holders)
    )


# ───────────────────────────── entity rules ─────────────────────────────


def test_buyer_and_seller_read_the_percent_change():
    assert _holder(pct_change=5.0).is_buyer is True
    assert _holder(pct_change=5.0).is_seller is False
    assert _holder(pct_change=-3.0).is_seller is True
    assert _holder(pct_change=-3.0).is_buyer is False
    # No change / unknown is neither.
    assert _holder(pct_change=0.0).is_buyer is False
    assert _holder(pct_change=0.0).is_seller is False
    assert _holder(pct_change=None).is_buyer is False


def test_share_change_derives_the_absolute_delta_from_the_current_size():
    # A holder now at 1100 shares after a +10% quarter held 1000 before → +100 shares.
    holder = _holder(shares=1100.0, pct_change=10.0)
    assert holder.share_change == pytest.approx(100.0)
    # A seller: now 900 after -10% held 1000 before → -100 shares.
    seller = _holder(shares=900.0, pct_change=-10.0)
    assert seller.share_change == pytest.approx(-100.0)


def test_value_change_mirrors_share_change_on_the_dollar_value():
    holder = _holder(value=110000.0, pct_change=10.0)
    assert holder.value_change == pytest.approx(10000.0)


def test_change_is_none_when_inputs_are_missing():
    assert _holder(shares=None, pct_change=10.0).share_change is None
    assert _holder(shares=1000.0, pct_change=None).share_change is None
    assert _holder(value=None, pct_change=10.0).value_change is None


def test_latest_snapshot_and_report_date_pick_the_newest_quarter():
    q1 = _holder("Old Fund", reported=_Q1)
    q2a = _holder("Vanguard", reported=_Q2)
    q2b = _holder("BlackRock", reported=_Q2)
    ownership = _ownership(q1, q2a, q2b)
    assert ownership.latest_report_date == _Q2
    assert {h.holder for h in ownership.latest_holders} == {"Vanguard", "BlackRock"}


def test_flow_rolls_up_only_the_latest_snapshot():
    # Q1 holder must be ignored by the flow (it's not the latest snapshot), so the older quarter
    # can't double-count. Q2: one buyer (+100 sh / +$10k), one seller (-50 sh / -$5k).
    q1 = _holder("Old", reported=_Q1, shares=1.0, value=1.0, pct_change=999.0)
    buyer = _holder(
        "Buyer", reported=_Q2, shares=1100.0, value=110000.0, pct_change=10.0
    )
    seller = _holder(
        "Seller", reported=_Q2, shares=950.0, value=95000.0, pct_change=-5.0
    )
    flow = _ownership(q1, buyer, seller).flow
    assert (flow.buyers_count, flow.sellers_count) == (1, 1)
    assert flow.shares_bought == pytest.approx(100.0)
    assert flow.shares_sold == pytest.approx(50.0)  # magnitude, positive
    assert flow.value_bought == pytest.approx(10000.0)
    assert flow.value_sold == pytest.approx(5000.0)
    assert flow.net_share_change == pytest.approx(50.0)
    assert flow.net_value_change == pytest.approx(5000.0)


def test_is_empty_is_about_the_holders_feed():
    assert _ownership().is_empty is True
    # A lone breakdown with no holders is still empty (the feed is the primary payload).
    assert _ownership(breakdown=OwnershipBreakdown(60.0, 0.1, 61.0, 100)).is_empty is True
    assert _ownership(_holder()).is_empty is False


def test_breakdown_is_empty_when_every_field_is_none():
    assert OwnershipBreakdown(None, None, None, None).is_empty is True
    assert OwnershipBreakdown(60.0, None, None, None).is_empty is False


# ───────────────────────── GetInstitutionalOwnership ─────────────────────


class _FakeReadProvider(InstitutionalOwnershipProvider):
    def __init__(self, ownership: InstitutionalOwnership) -> None:
        self._ownership = ownership
        self.calls: list[str] = []

    def get_institutional_ownership(self, symbol: str) -> InstitutionalOwnership:
        self.calls.append(symbol)
        return self._ownership


def test_get_normalizes_the_symbol_before_calling_the_provider():
    ownership = _ownership()
    provider = _FakeReadProvider(ownership)

    out = GetInstitutionalOwnership(provider).execute("  aapl ")

    assert out is ownership
    assert provider.calls == ["AAPL"]  # trimmed + upper-cased once, at the edge


def test_get_rejects_obviously_invalid_symbols():
    provider = _FakeReadProvider(_ownership())
    for bad in ("   ", "123", "TOOLONG", "BR.K"):
        with pytest.raises(ValueError):
            GetInstitutionalOwnership(provider).execute(bad)
    assert provider.calls == []  # rejected before the provider is touched


# ───────────────────────── SyncInstitutionalOwnership ────────────────────


class _FakeRepo(InstitutionalOwnershipRepository):
    """Serves a fixed target list and records what got upserted."""

    def __init__(self, targets: list[RefreshTarget]) -> None:
        self._targets = list(targets)
        self.upserts: list[tuple[str, str | None]] = []
        self.refresh_limit: int | None = "unset"

    def get(self, symbol: str):  # unused here
        return None

    def upsert(self, symbol, name, ownership) -> None:
        self.upserts.append((symbol, name))

    def refresh_targets(self, limit) -> list[RefreshTarget]:
        self.refresh_limit = limit
        return self._targets if limit is None else self._targets[:limit]


class _FakeSyncProvider(InstitutionalOwnershipProvider):
    """Returns a canned ownership per symbol, an empty one, or raises."""

    def __init__(self, *, empty=(), errors=None) -> None:
        self._empty = set(empty)
        self._errors = errors or {}
        self.calls: list[str] = []

    def get_institutional_ownership(self, symbol: str) -> InstitutionalOwnership:
        self.calls.append(symbol)
        if symbol in self._errors:
            raise self._errors[symbol]
        if symbol in self._empty:
            return InstitutionalOwnership(symbol=symbol)
        return _ownership(_holder(), symbol=symbol)


def test_sync_refreshes_every_target_and_reports_counts():
    repo = _FakeRepo(
        [RefreshTarget("AAPL", "Apple Inc."), RefreshTarget("MSFT", None)]
    )
    provider = _FakeSyncProvider()

    report = SyncInstitutionalOwnership(provider, repo).execute(limit=10)

    assert isinstance(report, InstitutionalOwnershipSyncReport)
    assert (report.refreshed, report.failed, report.limit) == (2, 0, 10)
    assert provider.calls == ["AAPL", "MSFT"]  # stalest-first order
    assert repo.upserts == [("AAPL", "Apple Inc."), ("MSFT", None)]


def test_sync_counts_failures_and_keeps_going():
    repo = _FakeRepo(
        [RefreshTarget("AAPL", None), RefreshTarget("BAD", None), RefreshTarget("MSFT", None)]
    )
    provider = _FakeSyncProvider(
        errors={"BAD": StockDataUnavailable("BAD", "yahoo down")}
    )

    report = SyncInstitutionalOwnership(provider, repo).execute(limit=10)

    assert (report.refreshed, report.failed) == (2, 1)
    assert [s for s, _ in repo.upserts] == ["AAPL", "MSFT"]  # BAD skipped, not stored


def test_sync_not_found_is_a_failure_not_a_crash():
    repo = _FakeRepo([RefreshTarget("ZZZZ", None)])
    provider = _FakeSyncProvider(errors={"ZZZZ": StockNotFound("ZZZZ")})

    report = SyncInstitutionalOwnership(provider, repo).execute()

    assert (report.refreshed, report.failed) == (0, 1)
    assert repo.upserts == []


def test_sync_empty_live_result_is_skipped_not_stored():
    repo = _FakeRepo(
        [RefreshTarget("AAPL", "Apple Inc."), RefreshTarget("GONE", None)]
    )
    provider = _FakeSyncProvider(empty={"GONE"})

    report = SyncInstitutionalOwnership(provider, repo).execute(limit=10)

    assert (report.refreshed, report.failed) == (1, 1)
    assert repo.upserts == [("AAPL", "Apple Inc.")]  # GONE never upserted


def test_sync_defaults_to_unlimited_and_floors_a_nonpositive_limit():
    repo = _FakeRepo([])
    SyncInstitutionalOwnership(_FakeSyncProvider(), repo).execute()
    assert repo.refresh_limit is None  # None => process every anchor stock (seed + refresh)

    SyncInstitutionalOwnership(_FakeSyncProvider(), repo).execute(limit=5)
    assert repo.refresh_limit == 5

    SyncInstitutionalOwnership(_FakeSyncProvider(), repo).execute(limit=0)
    assert repo.refresh_limit == 1  # a non-positive cap is floored to one
