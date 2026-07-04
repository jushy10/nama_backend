"""Tests for the universe sync use case: SyncUniverse.

Offline: hand-written fakes for the screener, classifier, and repository ports, so this
exercises only the orchestration — the upsert-vs-skip decision, the enrichment pass over
still-unclassified stocks, and count pass-through — independent of Yahoo or the DB.
"""

import pytest

from app.stocks.exceptions import StockDataUnavailable
from app.stocks.universe.entities import CompanyClassification, ScreenedStock
from app.stocks.universe.ports import CompanyClassificationProvider, StockScreener
from app.stocks.universe.repository import UniverseRepository, UniverseSyncCounts
from app.stocks.universe.use_cases import SyncUniverse, UniverseSyncReport


def _stock(ticker, *, market_cap=1e10, name=None, exchange=None, sector=None):
    return ScreenedStock(
        ticker=ticker,
        name=name,
        exchange=exchange,
        market_cap=market_cap,
        sector=sector,
    )


def _a_screen(n: int) -> tuple[ScreenedStock, ...]:
    """A plausible screen of ``n`` distinct names, each above the floor."""
    return tuple(_stock(f"T{i:04d}", market_cap=5e9 + i) for i in range(n))


class _FakeScreener(StockScreener):
    """Returns a canned screen, or raises the given error."""

    def __init__(self, stocks=(), *, error=None) -> None:
        self._stocks = tuple(stocks)
        self._error = error
        self.calls: list[float] = []

    def screen(self, *, min_market_cap):
        self.calls.append(min_market_cap)
        if self._error is not None:
            raise self._error
        return self._stocks


class _FakeClassifier(CompanyClassificationProvider):
    """Maps ticker -> classification; raises StockDataUnavailable for tickers in ``errors``."""

    def __init__(self, mapping=None, *, errors=()) -> None:
        self._mapping = dict(mapping or {})
        self._errors = set(errors)
        self.calls: list[str] = []

    def get_classification(self, symbol):
        self.calls.append(symbol)
        if symbol in self._errors:
            raise StockDataUnavailable(symbol, "yahoo blocked")
        return self._mapping.get(symbol, CompanyClassification())


class _FakeRepo(UniverseRepository):
    """Records the upsert input and the classifications written; serves a canned work-list."""

    def __init__(self, *, counts=UniverseSyncCounts(0, 0), missing=()) -> None:
        self._counts = counts
        self._missing = tuple(missing)
        self.upserted: tuple[ScreenedStock, ...] | None = None
        self.classified: list[tuple[str, CompanyClassification]] = []
        self.missing_limit: int | None = None

    def upsert_screen(self, stocks):
        self.upserted = tuple(stocks)
        return self._counts

    def tickers_missing_industry(self, limit):
        self.missing_limit = limit
        return self._missing

    def set_classification(self, ticker, classification):
        self.classified.append((ticker, classification))


def test_sync_upserts_a_healthy_screen_and_reports_counts():
    screen = _a_screen(SyncUniverse.MIN_PLAUSIBLE_SCREEN)  # exactly at the sanity floor
    screener = _FakeScreener(screen)
    repo = _FakeRepo(counts=UniverseSyncCounts(added=3, updated=7))

    report = SyncUniverse(screener, repo, _FakeClassifier()).execute()

    assert isinstance(report, UniverseSyncReport)
    assert screener.calls == [SyncUniverse.MIN_MARKET_CAP]  # the floor is passed through
    assert repo.upserted == screen  # the whole screen reached the upsert
    assert (report.screened, report.added, report.updated) == (len(screen), 3, 7)
    assert report.skipped is False
    assert (report.enriched, report.enrich_failed) == (0, 0)  # nothing missing to classify


def test_sync_skips_an_empty_screen_without_touching_the_store():
    screener = _FakeScreener(())
    repo = _FakeRepo()
    classifier = _FakeClassifier()

    report = SyncUniverse(screener, repo, classifier).execute()

    assert report.skipped is True
    assert (report.screened, report.added, report.updated) == (0, 0, 0)
    assert (report.enriched, report.enrich_failed) == (0, 0)
    assert repo.upserted is None  # upsert never called — the store is left intact
    # The enrichment pass is skipped too — a blocked bulk screen means blocked .info calls.
    assert repo.missing_limit is None
    assert classifier.calls == []


def test_sync_skips_an_implausibly_small_screen():
    # Below the sanity floor => treat as truncated/blocked and don't write a partial set.
    screener = _FakeScreener(_a_screen(SyncUniverse.MIN_PLAUSIBLE_SCREEN - 1))
    repo = _FakeRepo()

    report = SyncUniverse(screener, repo, _FakeClassifier()).execute()

    assert report.skipped is True
    assert repo.upserted is None
    assert repo.missing_limit is None  # enrichment not reached


def test_sync_propagates_a_hard_screen_failure():
    screener = _FakeScreener(error=StockDataUnavailable("*", "yahoo blocked"))
    repo = _FakeRepo()

    with pytest.raises(StockDataUnavailable):
        SyncUniverse(screener, repo, _FakeClassifier()).execute()
    assert repo.upserted is None  # nothing written on a hard failure
    assert repo.missing_limit is None


def test_sync_enriches_stocks_missing_an_industry():
    screen = _a_screen(SyncUniverse.MIN_PLAUSIBLE_SCREEN)
    repo = _FakeRepo(missing=("AAPL", "MSFT"))
    classifier = _FakeClassifier(
        {
            "AAPL": CompanyClassification("technology", "consumer_electronics"),
            "MSFT": CompanyClassification("technology", "software_infrastructure"),
        }
    )

    report = SyncUniverse(_FakeScreener(screen), repo, classifier).execute()

    assert classifier.calls == ["AAPL", "MSFT"]
    assert repo.classified == [
        ("AAPL", CompanyClassification("technology", "consumer_electronics")),
        ("MSFT", CompanyClassification("technology", "software_infrastructure")),
    ]
    assert (report.enriched, report.enrich_failed) == (2, 0)


def test_enrichment_counts_a_source_failure_and_keeps_going():
    screen = _a_screen(SyncUniverse.MIN_PLAUSIBLE_SCREEN)
    repo = _FakeRepo(missing=("AAPL", "BADX", "MSFT"))
    classifier = _FakeClassifier(
        {
            "AAPL": CompanyClassification(industry="consumer_electronics"),
            "MSFT": CompanyClassification(industry="software_infrastructure"),
        },
        errors=("BADX",),
    )

    report = SyncUniverse(_FakeScreener(screen), repo, classifier).execute()

    # BADX raised, so it isn't written — but the sweep continued to MSFT.
    assert [ticker for ticker, _ in repo.classified] == ["AAPL", "MSFT"]
    assert (report.enriched, report.enrich_failed) == (2, 1)


def test_enrichment_leaves_an_unclassifiable_symbol_for_later():
    screen = _a_screen(SyncUniverse.MIN_PLAUSIBLE_SCREEN)
    repo = _FakeRepo(missing=("ETF",))
    # The source reached the symbol but has no sector/industry for it (both None).
    classifier = _FakeClassifier({"ETF": CompanyClassification()})

    report = SyncUniverse(_FakeScreener(screen), repo, classifier).execute()

    assert repo.classified == []  # nothing written
    # Neither enriched nor failed — nothing went wrong, it's just left for a later run.
    assert (report.enriched, report.enrich_failed) == (0, 0)


def test_enrichment_limit_defaults_then_overrides():
    screen = _a_screen(SyncUniverse.MIN_PLAUSIBLE_SCREEN)

    repo = _FakeRepo()
    SyncUniverse(_FakeScreener(screen), repo, _FakeClassifier()).execute()
    assert repo.missing_limit == SyncUniverse.DEFAULT_LIMIT

    repo = _FakeRepo()
    SyncUniverse(_FakeScreener(screen), repo, _FakeClassifier()).execute(limit=25)
    assert repo.missing_limit == 25
