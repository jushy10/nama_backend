"""Tests for the ETF use cases: SyncEtfs (write side) + SearchEtfs (read side).

Offline: hand-written fakes for the screener and repository ports, so this exercises only the
orchestration — the upsert-vs-skip decision for the sync, and the edge normalization
(trim/clamp) and criteria pass-through for the search — independent of Yahoo or the DB.
"""

import pytest

from app.stocks.etfs.entities import (
    EtfSearchPage,
    EtfSearchResult,
    EtfSort,
    ScreenedEtf,
    SortDirection,
)
from app.stocks.etfs.ports import EtfScreener
from app.stocks.etfs.repository import (
    EtfRepository,
    EtfSearchRepository,
    EtfSyncCounts,
)
from app.stocks.etfs.use_cases import EtfSyncReport, SearchEtfs, SyncEtfs
from app.stocks.exceptions import StockDataUnavailable


def _etf(ticker, *, net_assets=1e10):
    return ScreenedEtf(ticker=ticker, net_assets=net_assets)


def _a_screen(n: int) -> tuple[ScreenedEtf, ...]:
    """A plausible screen of ``n`` distinct funds."""
    return tuple(_etf(f"E{i:04d}", net_assets=1e9 + i) for i in range(n))


class _FakeScreener(EtfScreener):
    """Returns a canned screen, or raises the given error."""

    def __init__(self, etfs=(), *, error=None) -> None:
        self._etfs = tuple(etfs)
        self._error = error
        self.calls = 0

    def screen(self):
        self.calls += 1
        if self._error is not None:
            raise self._error
        return self._etfs


class _FakeRepo(EtfRepository):
    """Records the upsert input; serves canned counts."""

    def __init__(self, *, counts=EtfSyncCounts(0, 0)) -> None:
        self._counts = counts
        self.upserted: tuple[ScreenedEtf, ...] | None = None

    def upsert_screen(self, etfs):
        self.upserted = tuple(etfs)
        return self._counts


def test_sync_upserts_a_healthy_screen_and_reports_counts():
    screen = _a_screen(SyncEtfs.MIN_PLAUSIBLE_SCREEN)  # exactly at the sanity floor
    screener = _FakeScreener(screen)
    repo = _FakeRepo(counts=EtfSyncCounts(added=5, updated=45))

    report = SyncEtfs(screener, repo).execute()

    assert isinstance(report, EtfSyncReport)
    assert screener.calls == 1
    assert repo.upserted == screen  # the whole screen reached the upsert
    assert (report.screened, report.added, report.updated) == (len(screen), 5, 45)
    assert report.skipped is False


def test_sync_skips_an_empty_screen_without_touching_the_store():
    repo = _FakeRepo()
    report = SyncEtfs(_FakeScreener(()), repo).execute()

    assert report.skipped is True
    assert (report.screened, report.added, report.updated) == (0, 0, 0)
    assert repo.upserted is None  # upsert never called — the store is left intact


def test_sync_skips_an_implausibly_small_screen():
    # Below the sanity floor => treat as truncated/blocked and don't write a partial set.
    repo = _FakeRepo()
    report = SyncEtfs(
        _FakeScreener(_a_screen(SyncEtfs.MIN_PLAUSIBLE_SCREEN - 1)), repo
    ).execute()

    assert report.skipped is True
    assert repo.upserted is None


def test_sync_propagates_a_hard_screen_failure():
    repo = _FakeRepo()
    with pytest.raises(StockDataUnavailable):
        SyncEtfs(
            _FakeScreener(error=StockDataUnavailable("*", "yahoo blocked")), repo
        ).execute()
    assert repo.upserted is None  # nothing written on a hard failure


# --- SearchEtfs (the read side) ------------------------------------------------------------

_RESULT = EtfSearchResult(
    ticker="SPY",
    name="SPDR S&P 500 ETF Trust",
    exchange="NYSEARCA",
    net_assets=5e11,
    expense_ratio=0.09,
    ytd_return=6.5,
)


class _FakeSearchRepo(EtfSearchRepository):
    """Records the criteria it was handed and returns a canned page."""

    def __init__(self, *, page=None) -> None:
        self._page = page or EtfSearchPage(results=(), total=0, limit=0, offset=0)
        self.criteria = None

    def search(self, criteria):
        self.criteria = criteria
        return self._page


def test_search_normalizes_inputs_and_passes_clean_criteria():
    repo = _FakeSearchRepo()
    SearchEtfs(repo).execute(
        query="  Gold ",
        sort=EtfSort.YTD_RETURN,
        direction=SortDirection.ASC,
        limit=10,
        offset=20,
    )
    c = repo.criteria
    # Trimmed but NOT lower-cased — the SQL match is case-insensitive, so the raw case is kept.
    assert c.query == "Gold"
    assert (c.sort, c.direction) == (EtfSort.YTD_RETURN, SortDirection.ASC)
    assert (c.limit, c.offset) == (10, 20)


def test_search_blank_text_becomes_none():
    repo = _FakeSearchRepo()
    SearchEtfs(repo).execute(query="   ")
    assert repo.criteria.query is None


def test_search_defaults_to_net_assets_desc_and_the_default_page():
    repo = _FakeSearchRepo()
    SearchEtfs(repo).execute()
    c = repo.criteria
    assert (c.sort, c.direction) == (EtfSort.NET_ASSETS, SortDirection.DESC)
    assert (c.limit, c.offset) == (SearchEtfs.DEFAULT_LIMIT, 0)
    assert c.query is None


@pytest.mark.parametrize(
    "given, expected",
    [
        (0, 1),
        (-5, 1),
        (1, 1),
        (50, 50),
        (SearchEtfs.MAX_LIMIT, SearchEtfs.MAX_LIMIT),
        (SearchEtfs.MAX_LIMIT + 1, SearchEtfs.MAX_LIMIT),
        (10_000, SearchEtfs.MAX_LIMIT),
    ],
)
def test_search_clamps_limit_into_range(given, expected):
    repo = _FakeSearchRepo()
    SearchEtfs(repo).execute(limit=given)
    assert repo.criteria.limit == expected


def test_search_floors_a_negative_offset():
    repo = _FakeSearchRepo()
    SearchEtfs(repo).execute(offset=-3)
    assert repo.criteria.offset == 0


def test_search_returns_the_repository_page_unchanged():
    page = EtfSearchPage(results=(_RESULT,), total=1, limit=25, offset=0)
    repo = _FakeSearchRepo(page=page)
    assert SearchEtfs(repo).execute(query="spy") is page
