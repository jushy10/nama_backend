import httpx
import pytest

from app.stocks.adapters.sec_edgar_insider_transactions_adapter import (
    SecEdgarInsiderTransactionsProvider,
    _parse_form4,
)
from app.stocks.exceptions import StockDataUnavailable, StockNotFound


# ── a hand-built Form 4 ownership document (no namespace, like the real thing) ─────────────

_FORM4_XML = (
    '<?xml version="1.0"?>'
    "<ownershipDocument>"
    "<documentType>4</documentType>"
    "<periodOfReport>2026-06-15</periodOfReport>"
    "<issuer><issuerCik>0000320193</issuerCik><issuerName>Apple Inc.</issuerName>"
    "<issuerTradingSymbol>AAPL</issuerTradingSymbol></issuer>"
    "<reportingOwner>"
    "<reportingOwnerId><rptOwnerCik>0001</rptOwnerCik>"
    "<rptOwnerName>Cook Timothy</rptOwnerName></reportingOwnerId>"
    "<reportingOwnerRelationship><isDirector>0</isDirector><isOfficer>1</isOfficer>"
    "<officerTitle>Chief Executive Officer</officerTitle></reportingOwnerRelationship>"
    "</reportingOwner>"
    "<nonDerivativeTable>"
    # an open-market purchase (P) with a price
    "<nonDerivativeTransaction>"
    "<securityTitle><value>Common Stock</value></securityTitle>"
    "<transactionDate><value>2026-06-15</value></transactionDate>"
    "<transactionCoding><transactionFormType>4</transactionFormType>"
    "<transactionCode>P</transactionCode></transactionCoding>"
    "<transactionAmounts>"
    "<transactionShares><value>1000</value></transactionShares>"
    "<transactionPricePerShare><value>200.50</value></transactionPricePerShare>"
    "<transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode>"
    "</transactionAmounts>"
    "<postTransactionAmounts><sharesOwnedFollowingTransaction><value>5000</value>"
    "</sharesOwnedFollowingTransaction></postTransactionAmounts>"
    "</nonDerivativeTransaction>"
    # an open-market sale (S); share count carries a comma
    "<nonDerivativeTransaction>"
    "<securityTitle><value>Common Stock</value></securityTitle>"
    "<transactionDate><value>2026-06-15</value></transactionDate>"
    "<transactionCoding><transactionCode>S</transactionCode></transactionCoding>"
    "<transactionAmounts>"
    "<transactionShares><value>2,000</value></transactionShares>"
    "<transactionPricePerShare><value>201.00</value></transactionPricePerShare>"
    "<transactionAcquiredDisposedCode><value>D</value></transactionAcquiredDisposedCode>"
    "</transactionAmounts>"
    "</nonDerivativeTransaction>"
    # an option exercise (M) whose price is only a footnote reference -> price None
    "<nonDerivativeTransaction>"
    "<securityTitle><value>Common Stock</value></securityTitle>"
    "<transactionDate><value>2026-06-15</value></transactionDate>"
    "<transactionCoding><transactionCode>M</transactionCode></transactionCoding>"
    "<transactionAmounts>"
    "<transactionShares><value>300</value></transactionShares>"
    '<transactionPricePerShare><footnoteId id="F1"/></transactionPricePerShare>'
    "<transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode>"
    "</transactionAmounts>"
    "</nonDerivativeTransaction>"
    # a code-less line -> dropped (nothing to classify)
    "<nonDerivativeTransaction>"
    "<securityTitle><value>Common Stock</value></securityTitle>"
    "<transactionCoding></transactionCoding>"
    "<transactionAmounts><transactionShares><value>1</value></transactionShares>"
    "</transactionAmounts>"
    "</nonDerivativeTransaction>"
    "</nonDerivativeTable>"
    "</ownershipDocument>"
).encode()

_FORM4_XML_2 = (
    '<?xml version="1.0"?>'
    "<ownershipDocument>"
    "<documentType>4</documentType>"
    "<reportingOwner>"
    "<reportingOwnerId><rptOwnerName>Levinson Arthur</rptOwnerName></reportingOwnerId>"
    "<reportingOwnerRelationship><isDirector>1</isDirector><isOfficer>0</isOfficer>"
    "</reportingOwnerRelationship>"
    "</reportingOwner>"
    "<nonDerivativeTable>"
    "<nonDerivativeTransaction>"
    "<securityTitle><value>Common Stock</value></securityTitle>"
    "<transactionDate><value>2026-05-01</value></transactionDate>"
    "<transactionCoding><transactionCode>P</transactionCode></transactionCoding>"
    "<transactionAmounts>"
    "<transactionShares><value>500</value></transactionShares>"
    "<transactionPricePerShare><value>190.00</value></transactionPricePerShare>"
    "<transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode>"
    "</transactionAmounts>"
    "</nonDerivativeTransaction>"
    "</nonDerivativeTable>"
    "</ownershipDocument>"
).encode()


def test_parses_the_non_derivative_transactions():
    txns = _parse_form4(_FORM4_XML)
    # Three parsed (the code-less line is dropped).
    assert [t.transaction_code for t in txns] == ["P", "S", "M"]
    buy = txns[0]
    assert buy.insider_name == "Cook Timothy"
    assert buy.officer_title == "Chief Executive Officer"
    assert buy.is_officer and not buy.is_director
    assert buy.shares == 1000 and buy.price_per_share == 200.50
    assert buy.acquired_disposed == "A"
    assert buy.shares_owned_following == 5000


def test_strips_commas_in_share_counts():
    sale = _parse_form4(_FORM4_XML)[1]
    assert sale.shares == 2000  # "2,000" parsed


def test_footnote_only_price_is_none():
    exercise = _parse_form4(_FORM4_XML)[2]
    assert exercise.transaction_code == "M"
    assert exercise.price_per_share is None  # price was a footnote reference, no <value>
    assert exercise.shares == 300


def test_bad_xml_is_empty_not_a_crash():
    assert _parse_form4(b"<not-a-form4>") == []


def test_no_non_derivative_table_is_empty():
    xml = b"<ownershipDocument><documentType>4</documentType></ownershipDocument>"
    assert _parse_form4(xml) == []


# ── the SEC walk (fake httpx) ─────────────────────────────────────────────────────────────

_TICKERS = {"0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."}}
_SUBMISSIONS = {
    "filings": {
        "recent": {
            # newest first; an 8-K interleaved to prove non-Form-4s are skipped
            "form": ["4", "8-K", "4"],
            "accessionNumber": [
                "0001140361-26-025622",
                "0000320193-26-000010",
                "0001140361-26-020000",
            ],
            "primaryDocument": ["xslF345X06/form4.xml", "a8k.htm", "form4.xml"],
            "filingDate": ["2026-06-17", "2026-06-10", "2026-05-01"],
        }
    }
}
_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK0000320193.json"
_FILING1_URL = (
    "https://www.sec.gov/Archives/edgar/data/320193/000114036126025622/form4.xml"
)
_FILING2_URL = (
    "https://www.sec.gov/Archives/edgar/data/320193/000114036126020000/form4.xml"
)


class _Resp:
    def __init__(self, status_code=200, *, json_data=None, content=b""):
        self.status_code = status_code
        self._json = json_data
        self.content = content

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json


class FakeHttp:
    def __init__(self, responses, *, errors=()):
        self._responses = responses
        self._errors = set(errors)
        self.requests: list[str] = []

    def get(self, url):
        self.requests.append(url)
        if url in self._errors:
            raise httpx.ConnectError("boom")
        if url not in self._responses:
            return _Resp(404)
        return self._responses[url]


def _provider(http) -> SecEdgarInsiderTransactionsProvider:
    p = SecEdgarInsiderTransactionsProvider()
    p._http = http
    return p


def _happy_responses():
    return {
        _TICKERS_URL: _Resp(json_data=_TICKERS),
        _SUBMISSIONS_URL: _Resp(json_data=_SUBMISSIONS),
        _FILING1_URL: _Resp(content=_FORM4_XML),
        _FILING2_URL: _Resp(content=_FORM4_XML_2),
    }


def test_walks_ticker_to_cik_to_form4s_to_transactions():
    http = FakeHttp(_happy_responses())
    activity = _provider(http).get_insider_transactions("AAPL")
    assert not activity.is_empty
    # 3 from filing 1 + 1 from filing 2, in filing order.
    assert [t.transaction_code for t in activity.transactions] == ["P", "S", "M", "P"]
    # line_index resets per filing; accession disambiguates.
    filing2_txn = activity.transactions[-1]
    assert filing2_txn.accession_number == "0001140361-26-020000"
    assert filing2_txn.line_index == 0 and filing2_txn.insider_name == "Levinson Arthur"
    # The walk hit exactly the expected URLs (the 8-K is not fetched).
    assert http.requests == [
        _TICKERS_URL,
        _SUBMISSIONS_URL,
        _FILING1_URL,
        _FILING2_URL,
    ]


def test_summary_reflects_the_open_market_buys_and_sells():
    activity = _provider(FakeHttp(_happy_responses())).get_insider_transactions("AAPL")
    summary = activity.summary
    # Two open-market buys (1000*200.50 + 500*190.00) and one sale (2000*201.00).
    assert summary.open_market_buy_count == 2
    assert summary.open_market_sell_count == 1
    assert summary.open_market_buy_value == 1000 * 200.50 + 500 * 190.00
    assert summary.open_market_sell_value == 2000 * 201.00


def test_ticker_map_is_cached_across_calls():
    http = FakeHttp(_happy_responses())
    provider = _provider(http)
    provider.get_insider_transactions("AAPL")
    provider.get_insider_transactions("AAPL")
    assert http.requests.count(_TICKERS_URL) == 1  # fetched once, reused


def test_unmapped_ticker_is_stock_not_found():
    http = FakeHttp({_TICKERS_URL: _Resp(json_data=_TICKERS)})
    with pytest.raises(StockNotFound):
        _provider(http).get_insider_transactions("ZZZZ")


def test_submissions_transport_failure_is_stock_data_unavailable():
    http = FakeHttp(_happy_responses(), errors={_SUBMISSIONS_URL})
    with pytest.raises(StockDataUnavailable):
        _provider(http).get_insider_transactions("AAPL")


def test_an_unreadable_filing_is_skipped_best_effort():
    # Filing 2's XML is missing (404); filing 1 still parses — one bad filing doesn't sink it.
    responses = _happy_responses()
    del responses[_FILING2_URL]
    activity = _provider(FakeHttp(responses)).get_insider_transactions("AAPL")
    assert [t.transaction_code for t in activity.transactions] == ["P", "S", "M"]


def test_no_form4_filings_is_an_empty_activity():
    subs = {"filings": {"recent": {"form": ["10-Q"], "accessionNumber": ["x"], "primaryDocument": ["y.htm"], "filingDate": ["2026-01-01"]}}}
    http = FakeHttp(
        {_TICKERS_URL: _Resp(json_data=_TICKERS), _SUBMISSIONS_URL: _Resp(json_data=subs)}
    )
    activity = _provider(http).get_insider_transactions("AAPL")
    assert activity.is_empty  # covered filer, just no recent Form 4s


def test_a_present_null_submissions_body_degrades_to_empty_not_a_crash():
    # A malformed 200 body with an explicit-null `filings` must not raise an unmapped
    # AttributeError (which would 500) — it degrades to an empty activity.
    responses = {
        _TICKERS_URL: _Resp(json_data=_TICKERS),
        _SUBMISSIONS_URL: _Resp(json_data={"filings": None}),
    }
    assert _provider(FakeHttp(responses)).get_insider_transactions("AAPL").is_empty


def test_live_order_matches_db_serving_order():
    # The load-bearing invariant: the adapter's live sort and the DB repository's serving order
    # must agree on the SAME data, so a live-served and a cache-served response are identical.
    from datetime import datetime, timezone

    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from app.db import Base
    from app.stocks.insider_transactions.db_repository import (
        SqlInsiderTransactionsRepository,
    )

    live = _provider(FakeHttp(_happy_responses())).get_insider_transactions("AAPL")
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    now = datetime(2026, 7, 11, tzinfo=timezone.utc)
    with Session(engine) as db:
        SqlInsiderTransactionsRepository(db, now=lambda: now).upsert("AAPL", "Apple", live)
        stored = SqlInsiderTransactionsRepository(db).get("AAPL")

    live_keys = [(t.accession_number, t.line_index) for t in live.transactions]
    db_keys = [(t.accession_number, t.line_index) for t in stored.transactions]
    assert live_keys == db_keys  # identical ordering regardless of cache state
