from datetime import date

import httpx
import pytest

from app.adapters.stock_watcher.congress_trades_adapter_impl import (
    _HOUSE_FEED,
    _SENATE_FEED,
    CongressTradesAdapterImpl,
    _normalize_ticker,
    _normalize_tx_type,
    _parse_date,
    _parse_feed,
)
from app.domains.shared.exceptions import StockDataUnavailable

# Real House-feed row shapes (from TattooedHead/house-stock-watcher-data).
_HOUSE_ROWS = [
    {
        "transaction_date": "06/12/2026",
        "disclosure_date": "07/08/2026",
        "ticker": "ABT",
        "asset_description": "Abbott Laboratories Common Stock",
        "asset_type": "Stock",
        "type": "Purchase",
        "amount": "$1,001 - $15,000",
        "representative": "Richard Dean Dr McCormick",
        "district": "GA06",
        "owner": "Self",
        "source_url": "https://disclosures-clerk.house.gov/x/1.pdf",
    },
    {
        "transaction_date": "06/09/2026",
        "disclosure_date": "07/08/2026",
        "ticker": "--",  # non-equity / placeholder -> dropped
        "type": "Sale",
        "amount": "$1,001 - $15,000",
        "representative": "Somebody",
        "owner": "Self",
    },
]

# Real Senate-feed row shapes (from timothycarambat/senate-stock-watcher-data): keyed on `senator`,
# no `disclosure_date`, sale split into "Sale (Full)" / "Sale (Partial)".
_SENATE_ROWS = [
    {
        "transaction_date": "11/16/2020",
        "owner": "Spouse",
        "ticker": "BA",
        "asset_description": "The Boeing Company",
        "asset_type": "Stock",
        "type": "Purchase",
        "amount": "$15,001 - $50,000",
        "senator": "Pat Roberts",
        "ptr_link": "https://efdsearch.senate.gov/x/1",
    },
    {
        "transaction_date": "11/10/2020",
        "owner": "Spouse",
        "ticker": "BYND",
        "type": "Sale (Full)",
        "amount": "$50,001 - $100,000",
        "senator": "Ron L Wyden",
        "ptr_link": "https://efdsearch.senate.gov/x/2",
    },
]


def test_parse_house_feed():
    trades = _parse_feed(_HOUSE_ROWS, _HOUSE_FEED)
    assert len(trades) == 1  # the "--" ticker row is dropped
    t = trades[0]
    assert t.member == "Richard Dean Dr McCormick"
    assert t.chamber == "House"
    assert t.ticker == "ABT"
    assert t.tx_type == "Purchase" and t.is_buy
    assert t.transaction_date == date(2026, 6, 12)
    assert t.disclosure_date == date(2026, 7, 8)
    assert t.owner == "Self"
    assert t.party is None
    assert t.source_url.endswith("/1.pdf")


def test_parse_senate_feed_uses_senator_key_and_folds_sale_variants():
    trades = _parse_feed(_SENATE_ROWS, _SENATE_FEED)
    assert [t.member for t in trades] == ["Pat Roberts", "Ron L Wyden"]
    assert all(t.chamber == "Senate" for t in trades)
    # Senate archive carries no disclosure date.
    assert all(t.disclosure_date is None for t in trades)
    # "Sale (Full)" folds to the normalized SALE.
    assert trades[1].tx_type == "Sale" and trades[1].is_sell


def test_parse_skips_rows_without_a_member():
    rows = [{"ticker": "ABT", "type": "Purchase", "amount": "$1", "owner": "Self"}]  # no representative
    assert _parse_feed(rows, _HOUSE_FEED) == []


def test_parse_skips_non_dict_rows():
    assert _parse_feed(["oops", None, 42], _HOUSE_FEED) == []


def test_normalize_ticker():
    assert _normalize_ticker("ABT") == "ABT"
    assert _normalize_ticker("brk.b") == "BRK-B"  # dotted class suffix folded + upper-cased
    assert _normalize_ticker("--") is None
    assert _normalize_ticker("") is None
    assert _normalize_ticker(None) is None
    assert _normalize_ticker("BTC-USD") is None  # crypto pair — suffix too long
    assert _normalize_ticker("TOOLONGX") is None


def test_normalize_tx_type():
    assert _normalize_tx_type("Purchase") == "Purchase"
    assert _normalize_tx_type("Sale (Partial)") == "Sale"
    assert _normalize_tx_type("Sale (Full)") == "Sale"
    assert _normalize_tx_type("Exchange") == "Exchange"
    assert _normalize_tx_type("Receive") == "Other"
    assert _normalize_tx_type(None) == "Other"


def test_parse_date_rejects_implausible_years():
    assert _parse_date("06/12/2026") == date(2026, 6, 12)
    assert _parse_date("2026-06-12") == date(2026, 6, 12)  # ISO fallback
    assert _parse_date("06/08/0009") is None  # garbled year
    assert _parse_date("--") is None
    assert _parse_date("") is None


# --- fetch_recent_trades with a fake _http -----------------------------------------------


class _FakeHttp:
    def __init__(self, responses):
        self._responses = responses  # url -> (status, payload) | Exception

    def get(self, url):
        result = self._responses[url]
        if isinstance(result, Exception):
            raise result
        status, payload = result
        return httpx.Response(status, json=payload)


def _provider_with(responses) -> CongressTradesAdapterImpl:
    provider = CongressTradesAdapterImpl()
    provider._http = _FakeHttp(responses)
    return provider


def test_fetch_merges_both_chambers_newest_first():
    provider = _provider_with(
        {
            _HOUSE_FEED.url: (200, _HOUSE_ROWS),
            _SENATE_FEED.url: (200, _SENATE_ROWS),
        }
    )
    trades = provider.fetch_recent_trades()
    chambers = {t.chamber for t in trades}
    assert chambers == {"House", "Senate"}
    # Newest activity first: the 2026 House disclosure precedes the 2020 Senate transactions.
    assert trades[0].chamber == "House" and trades[0].ticker == "ABT"


def test_fetch_is_best_effort_per_feed():
    # The Senate feed is down; the House feed still yields its trades (no raise).
    provider = _provider_with(
        {
            _HOUSE_FEED.url: (200, _HOUSE_ROWS),
            _SENATE_FEED.url: httpx.ConnectError("boom"),
        }
    )
    trades = provider.fetch_recent_trades()
    assert len(trades) == 1 and trades[0].chamber == "House"


def test_fetch_raises_only_when_every_feed_fails():
    provider = _provider_with(
        {
            _HOUSE_FEED.url: (503, None),
            _SENATE_FEED.url: httpx.ConnectError("boom"),
        }
    )
    with pytest.raises(StockDataUnavailable):
        provider.fetch_recent_trades()


def test_fetch_treats_non_200_as_a_feed_failure():
    provider = _provider_with(
        {
            _HOUSE_FEED.url: (200, _HOUSE_ROWS),
            _SENATE_FEED.url: (404, None),
        }
    )
    # House still comes through; the 404 Senate feed is skipped.
    trades = provider.fetch_recent_trades()
    assert {t.chamber for t in trades} == {"House"}
