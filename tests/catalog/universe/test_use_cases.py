import pytest

from app.stocks.company.earnings.quarterly.entities import (
    QuarterlyEarnings,
    QuarterlyEarningsTimeline,
)
from app.stocks.company.earnings.quarterly.repository import QuarterlyEarningsRepository
from app.stocks.exceptions import StockDataUnavailable
from app.stocks.catalog.universe.entities import (
    Classifications,
    CompanyClassification,
    MarketCapTier,
    PeerCompany,
    ScreenedStock,
    ScreenIntent,
    SortDirection,
    StockSearchCriteria,
    StockSearchPage,
    StockSearchResult,
    StockSort,
)
from app.stocks.catalog.universe.ports import (
    CompanyClassificationProvider,
    ScreenerQueryTranslator,
    StockScreener,
)
from app.stocks.catalog.universe.repository import (
    StockSearchRepository,
    UniverseRepository,
    UniverseSyncCounts,
)
from app.stocks.catalog.universe.use_cases import (
    AiScreenStocks,
    GetIndustryValuation,
    GetPeerComparison,
    ListClassifications,
    SearchStocks,
    SyncUniverse,
    UniverseSyncReport,
)


def _stock(
    ticker,
    *,
    market_cap=1e10,
    name=None,
    exchange=None,
    sector=None,
    price=None,
    country=None,
    currency=None,
):
    return ScreenedStock(
        ticker=ticker,
        name=name,
        exchange=exchange,
        market_cap=market_cap,
        sector=sector,
        price=price,
        country=country,
        currency=currency,
    )


def _a_screen(n: int) -> tuple[ScreenedStock, ...]:
    return tuple(_stock(f"T{i:04d}", market_cap=5e9 + i) for i in range(n))


def _four_quarter_timeline(symbol: str, ttm_eps: float) -> QuarterlyEarningsTimeline:
    per_q = ttm_eps / 4
    quarters = tuple(
        QuarterlyEarnings(
            fiscal_year=2025,
            fiscal_quarter=q,
            period_end=None,
            report_date=None,
            eps_actual=per_q,
            eps_estimate=None,
            eps_surprise=None,
            eps_surprise_percent=None,
            revenue_estimate=None,
            revenue_actual=None,
        )
        for q in range(1, 5)
    )
    return QuarterlyEarningsTimeline(symbol=symbol, quarters=quarters)


class _FakeQuarterlyRepo(QuarterlyEarningsRepository):
    def __init__(self, ttm_by_ticker=None) -> None:
        self._ttm = dict(ttm_by_ticker or {})
        self.gets: list[str] = []

    def get(self, symbol):
        self.gets.append(symbol)
        if symbol not in self._ttm:
            return None
        return _four_quarter_timeline(symbol, self._ttm[symbol])

    def upsert(self, symbol, name, timeline):  # unused by the valuation pass
        raise NotImplementedError

    def refresh_targets(self, limit):  # unused by the valuation pass
        raise NotImplementedError


class _FakeScreener(StockScreener):
    def __init__(self, stocks=(), *, error=None) -> None:
        self._stocks = tuple(stocks)
        self._error = error
        self.calls: list[float] = []
        self.regions: list[str] = []

    def screen(self, *, min_market_cap, region="us"):
        self.calls.append(min_market_cap)
        self.regions.append(region)
        if self._error is not None:
            raise self._error
        return self._stocks


class _FakeClassifier(CompanyClassificationProvider):
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
    def __init__(
        self,
        *,
        counts=UniverseSyncCounts(0, 0),
        missing=(),
        fcf_per_share=None,
        ev_components=None,
        us_domiciled_names=(),
    ) -> None:
        self._counts = counts
        self._missing = tuple(missing)
        # Raw names of US-domiciled US rows the CA pass matches CDRs against.
        self._us_domiciled_names = frozenset(us_domiciled_names)
        # Tickers the CA pass asked to delete (previously-stored CDRs).
        self.deleted: list[str] = []
        # The stored {ticker: fcf_per_share} the valuation pass reads (annual slice's write).
        self._fcf_per_share = dict(fcf_per_share or {})
        # The stored {ticker: (ebitda, total_debt, cash)} the valuation pass reads for EV/EBITDA
        # (fundamentals slice's write).
        self._ev_components = dict(ev_components or {})
        self.upserted: tuple[ScreenedStock, ...] | None = None
        self.classified: list[tuple[str, CompanyClassification]] = []
        self.missing_limit: int | None = None
        # The {ticker: pe} / {ticker: fcf_yield} / {ticker: ev_ebitda} maps handed to the
        # setters — None until the valuation pass runs.
        self.pe_written: dict[str, float | None] | None = None
        self.fcf_yield_written: dict[str, float | None] | None = None
        self.ev_ebitda_written: dict[str, float | None] | None = None

    def upsert_screen(self, stocks):
        self.upserted = tuple(stocks)
        return self._counts

    def us_domiciled_company_names(self):
        return self._us_domiciled_names

    def delete_stocks(self, tickers):
        tickers = list(tickers)
        self.deleted.extend(tickers)
        return len(tickers)

    def tickers_missing_classification(self, limit):
        self.missing_limit = limit
        return self._missing

    def set_classification(self, ticker, classification):
        self.classified.append((ticker, classification))

    def set_pe_ratios(self, pe_by_ticker):
        self.pe_written = dict(pe_by_ticker)
        return sum(1 for pe in self.pe_written.values() if pe is not None)

    def fcf_per_share_by_ticker(self):
        return dict(self._fcf_per_share)

    def set_fcf_yields(self, fcf_yield_by_ticker):
        self.fcf_yield_written = dict(fcf_yield_by_ticker)
        return sum(1 for y in self.fcf_yield_written.values() if y is not None)

    def ev_components_by_ticker(self):
        return dict(self._ev_components)

    def set_ev_ebitda(self, ev_ebitda_by_ticker):
        self.ev_ebitda_written = dict(ev_ebitda_by_ticker)
        return sum(1 for v in self.ev_ebitda_written.values() if v is not None)


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
    quarterly = _FakeQuarterlyRepo()

    report = SyncUniverse(screener, repo, classifier, quarterly).execute()

    assert report.skipped is True
    assert (report.screened, report.added, report.updated) == (0, 0, 0)
    assert (report.enriched, report.enrich_failed, report.valued) == (0, 0, 0)
    assert repo.upserted is None  # upsert never called — the store is left intact
    # The enrichment AND valuation passes are skipped too — a blocked bulk screen means
    # blocked .info calls, and there's nothing fresh to value.
    assert repo.missing_limit is None
    assert classifier.calls == []
    assert repo.pe_written is None  # valuation pass never ran
    assert quarterly.gets == []


def test_sync_skips_an_implausibly_small_screen():
    # Below the sanity floor => treat as truncated/blocked and don't write a partial set.
    screener = _FakeScreener(_a_screen(SyncUniverse.MIN_PLAUSIBLE_SCREEN - 1))
    repo = _FakeRepo()

    report = SyncUniverse(screener, repo, _FakeClassifier(), _FakeQuarterlyRepo()).execute()

    assert report.skipped is True
    assert repo.upserted is None
    assert repo.missing_limit is None  # enrichment not reached
    assert repo.pe_written is None  # valuation not reached either
    assert report.valued == 0


def test_sync_ca_region_screens_canada_and_carries_the_market_facts():
    # region="ca" is forwarded to the screener, and the ScreenedStocks' CA/CAD facts reach the
    # upsert untouched (the sync doesn't derive or override them — the adapter stamped them).
    screen = tuple(
        _stock(f"C{i:04d}.TO", market_cap=2e9 + i, country="CA", currency="CAD")
        for i in range(SyncUniverse._MIN_PLAUSIBLE_BY_REGION["ca"])
    )
    screener = _FakeScreener(screen)
    repo = _FakeRepo(counts=UniverseSyncCounts(added=len(screen), updated=0))

    report = SyncUniverse(screener, repo, _FakeClassifier(), region="ca").execute()

    assert screener.regions == ["ca"]
    assert report.skipped is False
    assert repo.upserted == screen
    assert {s.country for s in repo.upserted} == {"CA"}
    assert {s.currency for s in repo.upserted} == {"CAD"}


def test_sync_drops_cboe_canada_ne_cdrs_before_the_upsert():
    # The CA screen returns Cboe Canada (.NE) CDRs alongside genuine TSX (.TO) companies. The sync
    # filters the .NE rows out up front, so a CDR is never written onto the anchor in the first
    # place (not merely hidden at read time). Enough .TO names remain to clear the CA floor.
    cdrs = (
        _stock("INTC.NE", market_cap=6e11, country="CA", currency="CAD"),
        _stock("ZAAP.NE", market_cap=3e12, country="CA", currency="CAD"),
    )
    tsx = tuple(
        _stock(f"C{i:04d}.TO", market_cap=2e9 + i, country="CA", currency="CAD")
        for i in range(SyncUniverse._MIN_PLAUSIBLE_BY_REGION["ca"])
    )
    repo = _FakeRepo(counts=UniverseSyncCounts(added=len(tsx), updated=0))

    report = SyncUniverse(
        _FakeScreener(cdrs + tsx), repo, _FakeClassifier(), region="ca"
    ).execute()

    upserted = {s.ticker for s in repo.upserted}
    assert not any(t.endswith(".NE") for t in upserted)  # no CDR reached the anchor
    assert {s.ticker for s in tsx} <= upserted  # every TSX company still landed
    assert report.screened == len(tsx)  # the reported size is the post-filter CA universe


def test_sync_drops_and_purges_to_cdrs_of_us_companies():
    # A .TO CDR of a US company (AAPL.TO / MSFT.TO — same name as the US-domiciled AAPL / MSFT) is
    # dropped from the upsert AND purged from the anchor. A genuinely Canadian company dual-listed
    # in the US (SHOP.TO — US SHOP is CA-domiciled, so its name isn't in the US-domiciled index) is
    # kept, as is a ticker collision (CNR.TO Canadian National, whose name doesn't match US Core
    # Natural Resources).
    cdrs = (
        _stock("AAPL.TO", name="Apple Inc.", market_cap=3e12, country="CA", currency="CAD"),
        _stock("MSFT.TO", name="Microsoft Corporation", market_cap=3e12, country="CA", currency="CAD"),
    )
    canadian = (
        _stock("SHOP.TO", name="Shopify Inc.", market_cap=1.2e11, country="CA", currency="CAD"),
        _stock("CNR.TO", name="Canadian National Railway Company", market_cap=1e11, country="CA", currency="CAD"),
    )
    pad = _a_screen(SyncUniverse._MIN_PLAUSIBLE_BY_REGION["ca"])
    repo = _FakeRepo(
        counts=UniverseSyncCounts(added=len(canadian) + len(pad), updated=0),
        # The US universe (post-enrichment) — Apple/Microsoft are US-domiciled; Shopify is NOT
        # (it's Canadian, so its US listing is CA-domiciled and absent here), Core Natural
        # Resources is a different name than Canadian National.
        us_domiciled_names=("Apple Inc.", "Microsoft Corporation", "Core Natural Resources, Inc."),
    )

    SyncUniverse(
        _FakeScreener(cdrs + canadian + pad), repo, _FakeClassifier(), region="ca"
    ).execute()

    assert set(repo.deleted) == {"AAPL.TO", "MSFT.TO"}  # existing CDR copies purged
    upserted = {s.ticker for s in repo.upserted}
    assert "AAPL.TO" not in upserted and "MSFT.TO" not in upserted  # not (re-)ingested
    assert {"SHOP.TO", "CNR.TO"} <= upserted  # the real Canadian companies stay


def test_us_pass_never_purges_by_name():
    # The name-match CDR purge is CA-only: the US pass must not match US rows against their own
    # US-domiciled names (which would delete/skip them). region="us" skips the whole step.
    screen = _a_screen(SyncUniverse.MIN_PLAUSIBLE_SCREEN)
    repo = _FakeRepo(
        counts=UniverseSyncCounts(added=len(screen), updated=0),
        us_domiciled_names=(f"T{i:04d}" for i in range(5)),
    )

    SyncUniverse(_FakeScreener(screen), repo, _FakeClassifier(), region="us").execute()

    assert repo.deleted == []  # nothing purged on the US pass
    assert {s.ticker for s in repo.upserted} == {s.ticker for s in screen}


def test_ca_plausibility_floor_is_lower_than_us():
    # A modest Canadian screen (below the US floor, above the CA one) is a healthy CA result —
    # written for region="ca", but the same count under the US default would be treated as
    # truncated and skipped.
    count = SyncUniverse._MIN_PLAUSIBLE_BY_REGION["ca"]  # 40: >= CA floor, < US floor (100)
    screen = tuple(
        _stock(f"C{i:04d}.TO", market_cap=2e9 + i, country="CA", currency="CAD")
        for i in range(count)
    )

    ca_repo = _FakeRepo(counts=UniverseSyncCounts(added=count, updated=0))
    ca_report = SyncUniverse(_FakeScreener(screen), ca_repo, _FakeClassifier(), region="ca").execute()
    assert ca_report.skipped is False
    assert ca_repo.upserted == screen

    us_repo = _FakeRepo()
    us_report = SyncUniverse(_FakeScreener(screen), us_repo, _FakeClassifier(), region="us").execute()
    assert us_report.skipped is True  # same count, but under the US floor -> skipped
    assert us_repo.upserted is None


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
            # The domicile rides the same classification the enrichment writes (here US for both).
            "AAPL": CompanyClassification(
                "technology", "consumer_electronics", "US"
            ),
            "MSFT": CompanyClassification(
                "technology", "software_infrastructure", "US"
            ),
        }
    )

    report = SyncUniverse(_FakeScreener(screen), repo, classifier).execute()

    assert classifier.calls == ["AAPL", "MSFT"]
    assert repo.classified == [
        ("AAPL", CompanyClassification("technology", "consumer_electronics", "US")),
        ("MSFT", CompanyClassification("technology", "software_infrastructure", "US")),
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


def test_sync_values_screened_stocks_with_the_card_pe():
    # Two priced names on top of a plausible screen: one with four cached quarters (a TTM),
    # one un-cached. The baseline names carry no price, so the valuation pass skips them.
    priced = (
        _stock("AAPL", market_cap=3e12, price=100.0),
        _stock("MSFT", market_cap=2e12, price=50.0),
    )
    screen = _a_screen(SyncUniverse.MIN_PLAUSIBLE_SCREEN) + priced
    repo = _FakeRepo()
    quarterly = _FakeQuarterlyRepo({"AAPL": 5.0})  # AAPL TTM 5 -> 100/5 = 20; MSFT un-cached

    report = SyncUniverse(_FakeScreener(screen), repo, _FakeClassifier(), quarterly).execute()

    # Only the priced names are written; the price-less baseline is skipped, not nulled.
    assert set(repo.pe_written) == {"AAPL", "MSFT"}
    assert repo.pe_written["AAPL"] == 20.0  # price / TTM EPS — the card's exact rule
    assert repo.pe_written["MSFT"] is None  # priced but no cached quarters -> no P/E
    assert report.valued == 1  # only AAPL got a non-null figure


def test_sync_nulls_the_pe_for_a_trailing_loss():
    screen = _a_screen(SyncUniverse.MIN_PLAUSIBLE_SCREEN) + (
        _stock("LOSS", market_cap=1e10, price=30.0),
    )
    repo = _FakeRepo()
    quarterly = _FakeQuarterlyRepo({"LOSS": -2.0})  # negative TTM -> a P/E is meaningless

    report = SyncUniverse(_FakeScreener(screen), repo, _FakeClassifier(), quarterly).execute()

    assert repo.pe_written["LOSS"] is None
    assert report.valued == 0


def test_sync_writes_no_pe_when_no_quarterly_cache_is_wired():
    # P/E is best-effort enrichment: with no quarterly repo the sync still screens/classifies
    # but the valuation pass is a no-op — set_pe_ratios is never called.
    screen = _a_screen(SyncUniverse.MIN_PLAUSIBLE_SCREEN) + (
        _stock("AAPL", market_cap=3e12, price=100.0),
    )
    repo = _FakeRepo()

    report = SyncUniverse(_FakeScreener(screen), repo, _FakeClassifier()).execute()

    assert repo.pe_written is None  # set_pe_ratios never called
    assert report.valued == 0
    assert report.skipped is False  # the screen itself still succeeded


def test_sync_materializes_the_fcf_yield_from_stored_fcf_per_share():
    # Two priced names on a plausible screen; the anchor already carries an fcf_per_share for
    # AAPL (the annual slice's write) but not MSFT. fcf_yield = fcf/share / price * 100.
    priced = (
        _stock("AAPL", market_cap=3e12, price=100.0),
        _stock("MSFT", market_cap=2e12, price=50.0),
    )
    screen = _a_screen(SyncUniverse.MIN_PLAUSIBLE_SCREEN) + priced
    repo = _FakeRepo(fcf_per_share={"AAPL": 4.0})  # MSFT has no stored fcf/share
    quarterly = _FakeQuarterlyRepo()

    SyncUniverse(_FakeScreener(screen), repo, _FakeClassifier(), quarterly).execute()

    # Only the priced names are valued; the price-less baseline is skipped.
    assert set(repo.fcf_yield_written) == {"AAPL", "MSFT"}
    assert repo.fcf_yield_written["AAPL"] == 4.0  # 4 / 100 * 100 — the card's fcf_yield rule
    assert repo.fcf_yield_written["MSFT"] is None  # no stored fcf/share -> null yield


def test_sync_materializes_a_negative_fcf_yield_for_a_cash_burner():
    # Unlike the P/E, the materialized FCF yield keeps its sign: a negative FCF/share is a
    # real "burning cash" reading, so the stock ranks below zero rather than dropping out.
    screen = _a_screen(SyncUniverse.MIN_PLAUSIBLE_SCREEN) + (
        _stock("BURN", market_cap=1e10, price=25.0),
    )
    repo = _FakeRepo(fcf_per_share={"BURN": -5.0})

    SyncUniverse(_FakeScreener(screen), repo, _FakeClassifier()).execute()

    assert repo.fcf_yield_written["BURN"] == -20.0  # -5 / 25 * 100, sign kept


def test_sync_materializes_fcf_yield_even_without_a_quarterly_cache():
    # The FCF yield needs only the anchor read (fcf_per_share), not the quarterly TTM, so it
    # materializes even when no quarterly cache is wired (which zeroes the P/E pass).
    screen = _a_screen(SyncUniverse.MIN_PLAUSIBLE_SCREEN) + (
        _stock("AAPL", market_cap=3e12, price=100.0),
    )
    repo = _FakeRepo(fcf_per_share={"AAPL": 4.0})

    SyncUniverse(_FakeScreener(screen), repo, _FakeClassifier()).execute()  # no quarterly

    assert repo.pe_written is None  # P/E pass is a no-op without the quarterly cache
    assert repo.fcf_yield_written["AAPL"] == 4.0  # but the FCF yield still materialized


def test_sync_materializes_ev_ebitda_from_stored_components():
    # Two priced names; the anchor carries EV components (ebitda/debt/cash) for AAPL (the
    # fundamentals slice's write) but not MSFT. EV = market_cap + debt - cash, over EBITDA:
    # AAPL = (3e12 + 2e11 - 1e11) / 5e11 = 3.1e12 / 5e11 = 6.2.
    priced = (
        _stock("AAPL", market_cap=3e12, price=100.0),
        _stock("MSFT", market_cap=2e12, price=50.0),
    )
    screen = _a_screen(SyncUniverse.MIN_PLAUSIBLE_SCREEN) + priced
    repo = _FakeRepo(ev_components={"AAPL": (5e11, 2e11, 1e11)})  # MSFT has none
    quarterly = _FakeQuarterlyRepo()

    SyncUniverse(_FakeScreener(screen), repo, _FakeClassifier(), quarterly).execute()

    assert set(repo.ev_ebitda_written) == {"AAPL", "MSFT"}
    assert repo.ev_ebitda_written["AAPL"] == 6.2  # (3e12 + 2e11 - 1e11) / 5e11
    assert repo.ev_ebitda_written["MSFT"] is None  # no stored EV components -> null


def test_sync_ev_ebitda_defaults_missing_debt_and_cash_to_zero():
    # A name whose only stored component is EBITDA (debt/cash null): EV is just its market cap,
    # so ev_ebitda = market_cap / ebitda = 1e12 / 5e11 = 2.0.
    screen = _a_screen(SyncUniverse.MIN_PLAUSIBLE_SCREEN) + (
        _stock("DEBTFREE", market_cap=1e12, price=100.0),
    )
    repo = _FakeRepo(ev_components={"DEBTFREE": (5e11, None, None)})

    SyncUniverse(_FakeScreener(screen), repo, _FakeClassifier()).execute()

    assert repo.ev_ebitda_written["DEBTFREE"] == 2.0


def test_sync_materializes_a_negative_ev_ebitda_for_a_net_cash_name():
    # Like the FCF yield, the materialized EV/EBITDA keeps its sign: a company worth less than
    # its net cash has a negative enterprise value, a real "valued below net cash" reading.
    # EV = 1e10 + 0 - 5e10 = -4e10, over 1e9 EBITDA -> -40.0.
    screen = _a_screen(SyncUniverse.MIN_PLAUSIBLE_SCREEN) + (
        _stock("CASHPILE", market_cap=1e10, price=25.0),
    )
    repo = _FakeRepo(ev_components={"CASHPILE": (1e9, 0.0, 5e10)})

    SyncUniverse(_FakeScreener(screen), repo, _FakeClassifier()).execute()

    assert repo.ev_ebitda_written["CASHPILE"] == -40.0  # sign kept


def test_sync_ev_ebitda_is_null_without_a_positive_ebitda():
    # EV/EBITDA off a non-positive EBITDA is meaningless (the same guard as the card) -> null,
    # even though the market cap and the other components are present.
    screen = _a_screen(SyncUniverse.MIN_PLAUSIBLE_SCREEN) + (
        _stock("LOSS", market_cap=1e10, price=25.0),
    )
    repo = _FakeRepo(ev_components={"LOSS": (0.0, 1e9, 0.0)})

    SyncUniverse(_FakeScreener(screen), repo, _FakeClassifier()).execute()

    assert repo.ev_ebitda_written["LOSS"] is None


# --- SearchStocks / ListClassifications (the read side) ------------------------------------

_RESULT = StockSearchResult(
    ticker="NVDA",
    name="Nvidia",
    sector="technology",
    industry="semiconductors",
    market_cap=3e12,
    pe_ratio=48.2,
    fcf_yield=1.9,
    ev_ebitda=42.5,
    revenue_growth_yoy=61.6,
    eps_growth_yoy=587.4,
    fcf_growth_yoy=60.8,
    forward_revenue_growth_yoy=52.1,
    forward_eps_growth_yoy=48.3,
    in_sp500=True,
    in_nasdaq100=True,
)


class _FakeSearchRepo(StockSearchRepository):
    def __init__(
        self,
        *,
        page=None,
        classifications=None,
        pe_ratios=(),
        industry=None,
        peers=(),
    ) -> None:
        self._page = page or StockSearchPage(results=(), total=0, limit=0, offset=0)
        self._classifications = classifications or Classifications((), ())
        self._pe_ratios = pe_ratios
        self._industry = industry
        self._peers = tuple(peers)
        self.criteria: StockSearchCriteria | None = None
        self.classifications_calls = 0
        self.industry_asked: str | None = None
        self.ticker_asked: str | None = None
        self.peers_industry_asked: str | None = None

    def search(self, criteria):
        self.criteria = criteria
        return self._page

    def classifications(self):
        self.classifications_calls += 1
        return self._classifications

    def pe_ratios_for_industry(self, industry):
        self.industry_asked = industry
        return tuple(self._pe_ratios)

    def industry_for_ticker(self, ticker):
        self.ticker_asked = ticker
        return self._industry

    def anchor_metrics_for_ticker(self, ticker):  # pragma: no cover - not the search path
        raise NotImplementedError

    def tier_for_ticker(self, ticker):  # pragma: no cover - the endpoint path is industry-wide
        raise NotImplementedError

    def industry_peers(self, industry):  # pragma: no cover - the endpoint path is industry-wide
        raise NotImplementedError

    def peers_for_industry(self, industry):
        self.peers_industry_asked = industry
        return self._peers


def test_search_normalizes_inputs_and_passes_clean_criteria():
    repo = _FakeSearchRepo()
    SearchStocks(repo).execute(
        query="  NvDa ",
        sectors=["Consumer Electronics"],
        industries=["  Semiconductors  "],
        in_sp500=True,
        in_nasdaq100=False,
        market_cap_tiers=[MarketCapTier.LARGE],
        sort=StockSort.REVENUE_GROWTH,
        direction=SortDirection.ASC,
        limit=10,
        offset=20,
    )
    c = repo.criteria
    # Trimmed but NOT lower-cased — the SQL match is case-insensitive, so the raw case is kept.
    assert c.query == "NvDa"
    assert c.sectors == ("consumer_electronics",)  # slugged to the stored convention
    assert c.industries == ("semiconductors",)  # slugged + trimmed
    assert (c.in_sp500, c.in_nasdaq100) == (True, False)
    assert c.market_cap_tiers == (MarketCapTier.LARGE,)  # enum passes straight through
    assert (c.sort, c.direction) == (StockSort.REVENUE_GROWTH, SortDirection.ASC)
    assert (c.limit, c.offset) == (10, 20)


def test_search_multi_select_slugs_dedupes_and_drops_blanks():
    repo = _FakeSearchRepo()
    SearchStocks(repo).execute(
        sectors=["Technology", "  technology  ", "", "Energy"],
        industries=["Semiconductors", "semiconductors"],
        market_cap_tiers=[MarketCapTier.LARGE, MarketCapTier.MID, MarketCapTier.LARGE],
    )
    c = repo.criteria
    # Each label slugged; blanks dropped; duplicates collapsed with first-seen order kept.
    assert c.sectors == ("technology", "energy")
    assert c.industries == ("semiconductors",)
    assert c.market_cap_tiers == (MarketCapTier.LARGE, MarketCapTier.MID)


def test_search_uppercases_dedupes_and_drops_blank_countries():
    repo = _FakeSearchRepo()
    SearchStocks(repo).execute(countries=["ca", " US ", "", "ca"])
    # ISO-2 codes upper-cased, blanks dropped, duplicates collapsed with first-seen order kept.
    assert repo.criteria.countries == ("CA", "US")


def test_search_defaults_countries_to_empty():
    repo = _FakeSearchRepo()
    SearchStocks(repo).execute()
    assert repo.criteria.countries == ()  # no country filter -> every market


def test_search_blank_text_and_filters_become_empty():
    repo = _FakeSearchRepo()
    SearchStocks(repo).execute(query="   ", sectors=["", "   "], industries=None)
    c = repo.criteria
    assert c.query is None
    # Multi-select filters normalize to an empty tuple ("don't filter on this axis").
    assert (c.sectors, c.industries, c.market_cap_tiers) == ((), (), ())
    # Index flags default to a tri-state "don't filter".
    assert (c.in_sp500, c.in_nasdaq100) == (None, None)


def test_search_defaults_to_no_sort_and_the_default_page():
    repo = _FakeSearchRepo()
    SearchStocks(repo).execute()
    c = repo.criteria
    # No sort by default — the repository orders an unsorted browse by ticker (A→Z). The
    # direction default (descending) rides along unused until a sort field is chosen.
    assert c.sort is None
    assert c.direction is SortDirection.DESC
    assert (c.limit, c.offset) == (SearchStocks.DEFAULT_LIMIT, 0)
    assert c.query is None
    assert c.market_cap_tiers == ()  # no tier filter unless asked


@pytest.mark.parametrize(
    "given, expected",
    [
        (0, 1),
        (-5, 1),
        (1, 1),
        (50, 50),
        (SearchStocks.MAX_LIMIT, SearchStocks.MAX_LIMIT),
        (SearchStocks.MAX_LIMIT + 1, SearchStocks.MAX_LIMIT),
        (10_000, SearchStocks.MAX_LIMIT),
    ],
)
def test_search_clamps_limit_into_range(given, expected):
    repo = _FakeSearchRepo()
    SearchStocks(repo).execute(limit=given)
    assert repo.criteria.limit == expected


def test_search_floors_a_negative_offset():
    repo = _FakeSearchRepo()
    SearchStocks(repo).execute(offset=-3)
    assert repo.criteria.offset == 0


def test_search_returns_the_repository_page_unchanged():
    page = StockSearchPage(results=(_RESULT,), total=1, limit=25, offset=0)
    repo = _FakeSearchRepo(page=page)
    assert SearchStocks(repo).execute(query="nv") is page


def test_list_classifications_passes_through():
    classifications = Classifications(
        ("energy", "technology"), ("oil_gas", "semiconductors")
    )
    repo = _FakeSearchRepo(classifications=classifications)

    result = ListClassifications(repo).execute()

    assert result is classifications
    assert repo.classifications_calls == 1


# --- GetIndustryValuation (the per-industry P/E benchmark) ---------------------------------


def test_industry_valuation_slugs_the_industry_and_summarizes_peers():
    repo = _FakeSearchRepo(pe_ratios=(10.0, 20.0, 30.0, 40.0, 50.0))
    result = GetIndustryValuation(repo).execute("  Semiconductors ")
    # The raw label is slugged + trimmed before the read, so a slug or a label both work.
    assert repo.industry_asked == "semiconductors"
    assert result.industry == "semiconductors"
    assert result.count == 5
    assert (result.median_pe, result.p25_pe, result.p75_pe) == (30.0, 20.0, 40.0)


def test_industry_valuation_interpolates_quartiles_on_an_even_sample():
    # Four peers: the median sits between the two middles, the quartiles interpolate.
    result = GetIndustryValuation(
        _FakeSearchRepo(pe_ratios=(10.0, 20.0, 30.0, 40.0))
    ).execute("x")
    assert result.median_pe == 25.0  # (20 + 30) / 2
    assert result.p25_pe == 17.5
    assert result.p75_pe == 32.5


def test_industry_valuation_single_peer_is_its_own_median():
    result = GetIndustryValuation(_FakeSearchRepo(pe_ratios=(18.0,))).execute("x")
    assert result.count == 1
    assert (result.median_pe, result.p25_pe, result.p75_pe) == (18.0, 18.0, 18.0)


def test_industry_valuation_empty_when_no_peers():
    # An unknown but well-formed industry has no peers — a valid benchmark, not an error.
    result = GetIndustryValuation(_FakeSearchRepo(pe_ratios=())).execute("nonesuch")
    assert result.count == 0
    assert (result.median_pe, result.p25_pe, result.p75_pe) == (None, None, None)


def test_industry_valuation_rejects_a_blank_industry():
    with pytest.raises(ValueError):
        GetIndustryValuation(_FakeSearchRepo()).execute("   ")


# --- GetPeerComparison (the side-by-side peer table) --------------------------------------


def _peer(ticker, tier, *, cap=1e12, pe=None):
    return PeerCompany(
        ticker=ticker,
        name=f"{ticker} Inc.",
        market_cap=cap,
        pe_ratio=pe,
        ev_ebitda=None,
        fcf_yield=None,
        net_margin=None,
        revenue_growth_yoy=None,
        tier=tier,
    )


def test_peer_comparison_resolves_the_industry_then_builds_the_cohort():
    # The use case reads the anchor's industry, then its industry peers, and hands them to
    # PeerComparison.build — which splits the anchor out and scopes the cohort to its tier.
    peers = (
        _peer("NVDA", MarketCapTier.MEGA, pe=50.0),  # the anchor, in its own industry
        *[_peer(f"M{i}", MarketCapTier.MEGA, pe=20.0 + i) for i in range(4)],
    )
    repo = _FakeSearchRepo(industry="semiconductors", peers=peers)

    result = GetPeerComparison(repo).execute("  nvda ")  # normalized to NVDA

    assert repo.ticker_asked == "NVDA"  # industry looked up by the normalized symbol
    assert repo.peers_industry_asked == "semiconductors"
    assert result.industry == "semiconductors"
    assert result.anchor is not None and result.anchor.ticker == "NVDA"
    assert {p.ticker for p in result.peers} == {"M0", "M1", "M2", "M3"}


def test_peer_comparison_is_empty_for_an_unclassified_stock():
    # No stored industry -> nothing to compare against -> an empty comparison (a 200), and the
    # peers read is never made.
    repo = _FakeSearchRepo(industry=None)

    result = GetPeerComparison(repo).execute("XYZ")

    assert result.industry is None
    assert result.anchor is None
    assert result.peers == ()
    assert repo.peers_industry_asked is None  # short-circuited before the peers read


def test_peer_comparison_rejects_a_blank_ticker():
    with pytest.raises(ValueError):
        GetPeerComparison(_FakeSearchRepo()).execute("   ")


# --- AiScreenStocks (the AI-driven screen) ------------------------------------------------


class _FakeTranslator(ScreenerQueryTranslator):
    def __init__(self, *, intent: ScreenIntent | None = None, boom: bool = False) -> None:
        self._intent = intent or ScreenIntent()
        self._boom = boom
        self.query: str | None = None
        self.sectors: tuple[str, ...] | None = None
        self.industries: tuple[str, ...] | None = None

    def translate(self, query, *, sectors, industries):
        self.query = query
        self.sectors = tuple(sectors)
        self.industries = tuple(industries)
        if self._boom:
            raise StockDataUnavailable(query, "model down")
        return self._intent


def _ai_use_case(translator, repo):
    return AiScreenStocks(translator, repo)


def test_ai_screen_returns_the_translated_intent():
    # "mega-cap technology stocks" -> sector + tier filters, biggest first. The use case
    # returns the intent as-is; running the search is the client's job.
    intent = ScreenIntent(
        sectors=("technology",),
        market_cap_tiers=(MarketCapTier.MEGA,),
        sort=StockSort.MARKET_CAP,
        direction=SortDirection.DESC,
    )
    result = _ai_use_case(_FakeTranslator(intent=intent), _FakeSearchRepo()).execute(
        query="mega cap tech stocks"
    )
    assert result is intent


def test_ai_screen_maps_a_growth_request():
    # "top S&P 500 names by revenue growth" -> membership + sort, descending.
    intent = ScreenIntent(
        in_sp500=True,
        sort=StockSort.REVENUE_GROWTH,
        direction=SortDirection.DESC,
    )
    result = _ai_use_case(_FakeTranslator(intent=intent), _FakeSearchRepo()).execute(
        query="top sp500 stocks with good revenue growth"
    )
    assert result.in_sp500 is True
    assert (result.sort, result.direction) == (
        StockSort.REVENUE_GROWTH,
        SortDirection.DESC,
    )


def test_ai_screen_passes_the_universe_vocabulary_to_the_translator():
    # The translator is handed the universe's current slugs as its allowed vocabulary.
    classifications = Classifications(
        ("energy", "technology"), ("oil_gas", "semiconductors")
    )
    repo = _FakeSearchRepo(classifications=classifications)
    translator = _FakeTranslator()
    _ai_use_case(translator, repo).execute(query="anything")
    assert translator.sectors == ("energy", "technology")
    assert translator.industries == ("oil_gas", "semiconductors")
    assert translator.query == "anything"  # trimmed request


def test_ai_screen_trims_the_request_and_rejects_a_blank_one():
    repo = _FakeSearchRepo()
    translator = _FakeTranslator()
    _ai_use_case(translator, repo).execute(query="  find me tech  ")
    assert translator.query == "find me tech"  # trimmed before translation
    with pytest.raises(ValueError):
        _ai_use_case(_FakeTranslator(), _FakeSearchRepo()).execute(query="   ")


def test_ai_screen_propagates_a_translation_failure():
    # A model/vendor failure isn't "no matches" — it propagates (a 502 at the edge).
    with pytest.raises(StockDataUnavailable):
        _ai_use_case(_FakeTranslator(boom=True), _FakeSearchRepo()).execute(query="tech")
