"""Tests for the brief use cases — GenerateDailyBrief and GetDailyBrief.

Offline: the gather legs are duck-typed fakes with ``.execute()`` (standing in for the two
Alpaca boards + the heat map), and the model port + store are hand-written fakes. So the
orchestration — best-effort gathering, mover/breadth derivation, the complete-only upsert, and
the read path — runs with no vendor, model, or DB.
"""

from datetime import date

import pytest

from app.stocks.brief.entities import (
    BriefTone,
    MarketBrief,
    MarketBriefContext,
    MarketBriefSection,
)
from app.stocks.brief.ports import MarketBriefProvider
from app.stocks.brief.repository import MarketBriefRepository
from app.stocks.brief.use_cases import GenerateDailyBrief, GetDailyBrief
from app.stocks.entities import StockPerformance
from app.stocks.exceptions import StockDataUnavailable
from app.stocks.heatmap.entities import HeatMap, HeatMapRow, HeatMapScope
from app.stocks.market.entities import MarketIndexPerformance, SectorPerformance

_TODAY = date(2026, 7, 14)


# --- Fakes -------------------------------------------------------------------------------------


class _FakeExec:
    """A stand-in for a read use case: returns a canned result or raises. ``execute`` accepts
    an optional arg so it stands in for both the no-arg boards and the heat map (scope)."""

    def __init__(self, result=None, error=None):
        self._result = result
        self._error = error

    def execute(self, *args):
        if self._error is not None:
            raise self._error
        return self._result


class _FakeProvider(MarketBriefProvider):
    def __init__(self, brief=None, error=None):
        self._brief = brief
        self._error = error
        self.context = None
        self.brief_date = None
        self.calls = 0

    def generate(self, context, brief_date):
        self.calls += 1
        self.context = context
        self.brief_date = brief_date
        if self._error is not None:
            raise self._error
        return self._brief


class _FakeRepository(MarketBriefRepository):
    def __init__(self):
        self.store: dict[date, MarketBrief] = {}
        self.upserts = 0

    def get(self, brief_date):
        return self.store.get(brief_date)

    def latest(self):
        if not self.store:
            return None
        return self.store[max(self.store)]

    def upsert(self, brief):
        self.upserts += 1
        self.store[brief.brief_date] = brief


def _index(name, symbol, price, prev, perf=None) -> MarketIndexPerformance:
    return MarketIndexPerformance(
        name=name, symbol=symbol, price=price, previous_close=prev, as_of=None,
        performance=perf,
    )


def _sector(name, symbol, price, prev) -> SectorPerformance:
    return SectorPerformance(
        sector=name, symbol=symbol, price=price, previous_close=prev, as_of=None
    )


def _heatmap() -> HeatMap:
    rows = (
        HeatMapRow("NVDA", "NVIDIA", "technology", "semis", 3e12),
        HeatMapRow("JPM", "JPMorgan", "financials", "banks", 6e11),
        HeatMapRow("XOM", "Exxon", "energy", "oil", 4e11),
    )
    return HeatMap.build(
        HeatMapScope.SP500, rows, {"NVDA": 5.0, "JPM": -2.0, "XOM": -3.0}
    )


def _complete_brief() -> MarketBrief:
    return MarketBrief(
        brief_date=_TODAY,
        generated_at=None,
        tone=BriefTone.RISK_ON,
        summary="A broad rally.",
        sections=(MarketBriefSection("Overview", "Up across the board."),),
        model="test-model",
    )


def _generator(overview, sectors, heatmap, provider, repository, movers=5):
    return GenerateDailyBrief(
        overview, sectors, heatmap, provider, repository,
        movers=movers, today=lambda: _TODAY,
    )


# --- GenerateDailyBrief ------------------------------------------------------------------------


def test_generates_and_stores_a_complete_brief():
    provider = _FakeProvider(brief=_complete_brief())
    repo = _FakeRepository()
    gen = _generator(
        _FakeExec(result=[_index("S&P 500", "SPY", 100, 99)]),
        _FakeExec(result=[_sector("Technology", "XLK", 50, 49)]),
        _FakeExec(result=_heatmap()),
        provider,
        repo,
    )

    brief = gen.execute()

    assert brief is not None
    assert provider.brief_date == _TODAY
    assert repo.upserts == 1
    assert repo.get(_TODAY) is brief


def test_derives_movers_and_breadth_from_the_heatmap():
    provider = _FakeProvider(brief=_complete_brief())
    gen = _generator(
        _FakeExec(result=[_index("S&P 500", "SPY", 100, 99)]),
        _FakeExec(result=[_sector("Technology", "XLK", 50, 49)]),
        _FakeExec(result=_heatmap()),
        provider,
        _FakeRepository(),
    )

    gen.execute()

    ctx = provider.context
    assert isinstance(ctx, MarketBriefContext)
    # Breadth: NVDA up, JPM + XOM down, three quoted.
    assert (ctx.advancers, ctx.decliners, ctx.quoted) == (1, 2, 3)
    # Top gainer is NVDA; losers are most-negative first (XOM -3 before JPM -2).
    assert [m.ticker for m in ctx.gainers] == ["NVDA"]
    assert [m.ticker for m in ctx.losers] == ["XOM", "JPM"]
    # The index move is a true quote joined from the board, not authored by the model.
    assert ctx.indexes[0].change_percent == pytest.approx(1.01, abs=0.01)


def test_skips_when_no_market_data_was_gathered():
    provider = _FakeProvider(brief=_complete_brief())
    repo = _FakeRepository()
    gen = _generator(
        _FakeExec(error=StockDataUnavailable("market", "down")),
        _FakeExec(error=StockDataUnavailable("sectors", "down")),
        _FakeExec(error=StockDataUnavailable("quotes", "down")),
        provider,
        repo,
    )

    assert gen.execute() is None
    assert provider.calls == 0  # never bothered the model
    assert repo.upserts == 0


def test_one_board_down_still_generates_from_the_other():
    provider = _FakeProvider(brief=_complete_brief())
    repo = _FakeRepository()
    gen = _generator(
        _FakeExec(error=StockDataUnavailable("market", "down")),  # index board down
        _FakeExec(result=[_sector("Technology", "XLK", 50, 49)]),  # sectors OK
        _FakeExec(error=StockDataUnavailable("quotes", "down")),  # heatmap down
        provider,
        repo,
    )

    brief = gen.execute()
    assert brief is not None
    assert provider.context.indexes == ()  # the down board degraded to empty
    assert len(provider.context.sectors) == 1
    assert repo.upserts == 1


def test_does_not_store_an_incomplete_brief():
    hollow = MarketBrief(
        brief_date=_TODAY, generated_at=None, tone=BriefTone.MIXED,
        summary="", sections=(), model="m",  # not is_complete
    )
    repo = _FakeRepository()
    gen = _generator(
        _FakeExec(result=[_index("S&P 500", "SPY", 100, 99)]),
        _FakeExec(result=[]),
        _FakeExec(result=None),
        _FakeProvider(brief=hollow),
        repo,
    )

    assert gen.execute() is None
    assert repo.upserts == 0


def test_a_model_failure_is_swallowed_to_no_brief():
    repo = _FakeRepository()
    gen = _generator(
        _FakeExec(result=[_index("S&P 500", "SPY", 100, 99)]),
        _FakeExec(result=[]),
        _FakeExec(result=None),
        _FakeProvider(error=StockDataUnavailable("market-brief", "model down")),
        repo,
    )

    assert gen.execute() is None
    assert repo.upserts == 0


def test_generates_for_an_explicit_date():
    provider = _FakeProvider(brief=_complete_brief())
    gen = _generator(
        _FakeExec(result=[_index("S&P 500", "SPY", 100, 99)]),
        _FakeExec(result=[]),
        _FakeExec(result=None),
        provider,
        _FakeRepository(),
    )

    gen.execute(date(2026, 1, 2))
    assert provider.brief_date == date(2026, 1, 2)


# --- GetDailyBrief -----------------------------------------------------------------------------


def test_read_returns_the_latest_when_no_date():
    repo = _FakeRepository()
    repo.store[date(2026, 7, 13)] = _complete_brief()
    newest = _complete_brief()
    repo.store[date(2026, 7, 14)] = newest

    assert GetDailyBrief(repo).execute() is newest


def test_read_returns_a_specific_date():
    repo = _FakeRepository()
    target = _complete_brief()
    repo.store[date(2026, 7, 14)] = target

    assert GetDailyBrief(repo).execute(date(2026, 7, 14)) is target
    assert GetDailyBrief(repo).execute(date(2020, 1, 1)) is None
