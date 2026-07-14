"""Tests for the Congressional-trades use cases.

Offline: hand-written fakes for the source and repository ports drive the three use cases without
touching HTTP or the database. Covers the DB-only per-ticker read (miss -> empty, symbol
normalization, DB error -> empty), the market-wide windowed read, and the bulk sync (fetch once,
group by ticker, distribute anchor stalest-first, seeding + skipping empties, and a total-source
outage propagating).
"""

from datetime import date

import pytest

from app.stocks.congress.entities import (
    CongressActivity,
    CongressTrade,
)
from app.stocks.congress.repository import RefreshTarget
from app.stocks.congress.use_cases import (
    GetCongressActivity,
    GetCongressTrades,
    SyncCongressTrades,
    parse_window,
)
from app.stocks.exceptions import StockDataUnavailable


def _trade(ticker="NVDA", member="Pelosi", chamber="House", tx_type="Purchase", disc=date(2026, 7, 1)):
    return CongressTrade(
        member=member,
        chamber=chamber,
        party=None,
        ticker=ticker,
        company_name=f"{ticker} Inc.",
        tx_type=tx_type,
        amount_range="$1,001 - $15,000",
        transaction_date=date(2026, 6, 20),
        disclosure_date=disc,
        owner="Self",
        source_url=None,
    )


class _FakeRepo:
    def __init__(self, *, stored=None, market=None, targets=(), get_raises=False, market_raises=False):
        self._stored = stored or {}
        self._market = market or ([], 0)
        self._targets = list(targets)
        self._get_raises = get_raises
        self._market_raises = market_raises
        self.upserts: list[tuple[str, str | None, CongressActivity]] = []

    def get(self, symbol):
        if self._get_raises:
            raise RuntimeError("db down")
        return self._stored.get(symbol)

    def recent_market_activity(self, *, since, limit, offset):
        if self._market_raises:
            raise RuntimeError("db down")
        self.last_since = since
        trades, total = self._market
        return list(trades)[offset : offset + limit], total

    def upsert(self, symbol, name, activity):
        self.upserts.append((symbol, name, activity))

    def refresh_targets(self, limit):
        return self._targets if limit is None else self._targets[:limit]


# --- GetCongressTrades (per-ticker, DB-only) ---------------------------------------------


def test_get_returns_stored_activity():
    activity = CongressActivity("NVDA", (_trade(), _trade(member="Tuberville", tx_type="Sale")))
    out = GetCongressTrades(_FakeRepo(stored={"NVDA": activity})).execute("nvda")
    assert out.symbol == "NVDA" and len(out.trades) == 2


def test_get_miss_is_empty_not_error():
    out = GetCongressTrades(_FakeRepo()).execute("ZZZZ")
    assert out.is_empty and out.symbol == "ZZZZ"


def test_get_normalizes_symbol():
    activity = CongressActivity("BRK-B", (_trade(ticker="BRK-B"),))
    out = GetCongressTrades(_FakeRepo(stored={"BRK-B": activity})).execute(" brk.b ")
    assert out.symbol == "BRK-B"


def test_get_bad_symbol_raises_valueerror():
    with pytest.raises(ValueError):
        GetCongressTrades(_FakeRepo()).execute("123")


def test_get_swallows_db_error_as_empty():
    out = GetCongressTrades(_FakeRepo(get_raises=True)).execute("NVDA")
    assert out.is_empty


# --- GetCongressActivity (market-wide) ---------------------------------------------------


def test_market_activity_returns_page_and_total():
    trades = (_trade(), _trade(ticker="AAPL", member="Khanna"))
    repo = _FakeRepo(market=(trades, 5))
    out = GetCongressActivity(repo, today=lambda: date(2026, 7, 14)).execute(
        window_days=30, limit=10, offset=0
    )
    assert out.total == 5 and len(out.trades) == 2
    # A 30-day window from 2026-07-14 cuts off at 2026-06-14.
    assert repo.last_since == date(2026, 6, 14)


def test_market_activity_all_window_has_no_cutoff():
    repo = _FakeRepo(market=((_trade(),), 1))
    GetCongressActivity(repo, today=lambda: date(2026, 7, 14)).execute(
        window_days=None, limit=10, offset=0
    )
    assert repo.last_since is None


def test_market_activity_swallows_db_error_as_empty():
    out = GetCongressActivity(_FakeRepo(market_raises=True)).execute(
        window_days=30, limit=10, offset=0
    )
    assert out.is_empty and out.total == 0


def test_parse_window():
    assert parse_window("30d") == 30
    assert parse_window("1y") == 365
    assert parse_window("all") is None
    assert parse_window(None) == 30  # default
    with pytest.raises(ValueError):
        parse_window("bogus")


# --- SyncCongressTrades (bulk) -----------------------------------------------------------


class _FakeSource:
    def __init__(self, trades=(), error=None):
        self._trades = tuple(trades)
        self._error = error
        self.calls = 0

    def fetch_recent_trades(self):
        self.calls += 1
        if self._error is not None:
            raise self._error
        return self._trades


def test_sync_fetches_once_and_distributes_by_ticker():
    source = _FakeSource(
        [
            _trade(ticker="NVDA", member="Pelosi"),
            _trade(ticker="NVDA", member="Tuberville", chamber="Senate", tx_type="Sale"),
            _trade(ticker="AAPL", member="Greene"),
            _trade(ticker="TSLA", member="Nobody"),  # not an anchor target -> never stored
        ]
    )
    repo = _FakeRepo(targets=[RefreshTarget("NVDA", "NVIDIA"), RefreshTarget("AAPL", "Apple")])
    report = SyncCongressTrades(source, repo).execute()

    assert source.calls == 1  # one bulk fetch, not per-ticker
    assert report.fetched == 4 and report.stored == 2 and report.failed == 0
    stored = {sym: act for sym, _, act in repo.upserts}
    assert set(stored) == {"NVDA", "AAPL"}
    assert len(stored["NVDA"].trades) == 2  # both NVDA trades grouped onto it
    assert len(stored["AAPL"].trades) == 1


def test_sync_skips_anchor_stocks_with_no_trades():
    source = _FakeSource([_trade(ticker="NVDA")])
    repo = _FakeRepo(
        targets=[RefreshTarget("NVDA", "NVIDIA"), RefreshTarget("ZZZZ", "Zilch")]
    )
    report = SyncCongressTrades(source, repo).execute()
    assert report.stored == 1  # ZZZZ had no trades in the feed -> skipped, not failed
    assert report.failed == 0
    assert [sym for sym, _, _ in repo.upserts] == ["NVDA"]


def test_sync_respects_the_limit():
    source = _FakeSource([_trade(ticker="NVDA"), _trade(ticker="AAPL")])
    repo = _FakeRepo(
        targets=[RefreshTarget("NVDA", "NVIDIA"), RefreshTarget("AAPL", "Apple")]
    )
    report = SyncCongressTrades(source, repo).execute(limit=1)
    assert report.limit == 1
    assert [sym for sym, _, _ in repo.upserts] == ["NVDA"]  # only the first target visited


def test_sync_propagates_a_total_source_outage():
    source = _FakeSource(error=StockDataUnavailable("congress", "all feeds down"))
    repo = _FakeRepo(targets=[RefreshTarget("NVDA", "NVIDIA")])
    with pytest.raises(StockDataUnavailable):
        SyncCongressTrades(source, repo).execute()
    assert repo.upserts == []  # nothing stored on a total outage
