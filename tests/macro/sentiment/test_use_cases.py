from datetime import date, datetime, timezone

import pytest

from app.domains.shared.exceptions import StockDataUnavailable, StockNotFound
from app.domains.macro.sentiment.entities import FearGreedSnapshot, VixSnapshot
from app.domains.macro.sentiment.use_cases import GetMarketSentiment


class FakeVixProvider:
    def __init__(self, *, result=None, error=None):
        self._result = result
        self._error = error

    def get_vix(self) -> VixSnapshot:
        if self._error is not None:
            raise self._error
        return self._result


class FakeFearGreedProvider:
    def __init__(self, *, result=None, error=None):
        self._result = result
        self._error = error

    def get_fear_greed(self) -> FearGreedSnapshot:
        if self._error is not None:
            raise self._error
        return self._result


def _vix() -> VixSnapshot:
    return VixSnapshot(as_of=date(2026, 7, 13), value=17.16, previous_close=15.03)


def _fear_greed() -> FearGreedSnapshot:
    return FearGreedSnapshot(
        score=43.14,
        as_of=datetime(2026, 7, 14, 22, 24, 38, tzinfo=timezone.utc),
        rating="fear",
        previous_close=43.71,
    )


def test_gathers_both_legs():
    use_case = GetMarketSentiment(
        FakeVixProvider(result=_vix()),
        FakeFearGreedProvider(result=_fear_greed()),
    )
    sentiment = use_case.execute()
    assert sentiment.vix.value == 17.16
    assert sentiment.fear_greed.score == 43.14


def test_fear_greed_failure_drops_only_that_leg():
    use_case = GetMarketSentiment(
        FakeVixProvider(result=_vix()),
        FakeFearGreedProvider(error=StockDataUnavailable("*", "CNN blocked")),
    )
    sentiment = use_case.execute()
    assert sentiment.vix.value == 17.16
    assert sentiment.fear_greed is None


def test_vix_failure_drops_only_that_leg():
    use_case = GetMarketSentiment(
        FakeVixProvider(error=StockNotFound("*")),
        FakeFearGreedProvider(result=_fear_greed()),
    )
    sentiment = use_case.execute()
    assert sentiment.vix is None
    assert sentiment.fear_greed.score == 43.14


def test_both_failing_raises_unavailable():
    use_case = GetMarketSentiment(
        FakeVixProvider(error=StockDataUnavailable("*", "FRED down")),
        FakeFearGreedProvider(error=StockDataUnavailable("*", "CNN down")),
    )
    with pytest.raises(StockDataUnavailable):
        use_case.execute()
