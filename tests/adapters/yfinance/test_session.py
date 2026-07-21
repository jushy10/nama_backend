import pytest

from app.stocks.adapters.yfinance import session as yfinance_session


def _count_resets(monkeypatch) -> list:
    resets: list = []
    monkeypatch.setattr(yfinance_session, "reset_crumb", lambda: resets.append(1))
    return resets


def test_empty_result_triggers_one_crumb_refresh_and_retry(monkeypatch):
    resets = _count_resets(monkeypatch)
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        # First call comes back empty (a swallowed crumb 401); the retry succeeds.
        return {} if calls["n"] == 1 else {"sector": "Technology"}

    result = yfinance_session.call(fn, is_empty=lambda data: not data)

    assert result == {"sector": "Technology"}
    assert calls["n"] == 2  # retried exactly once
    assert len(resets) == 1  # crumb dropped before the retry


def test_still_empty_after_retry_returns_empty(monkeypatch):
    resets = _count_resets(monkeypatch)
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        return {}  # Yahoo genuinely has nothing — stays empty after the one retry

    result = yfinance_session.call(fn, is_empty=lambda data: not data)

    assert result == {}
    assert calls["n"] == 2  # one retry, then gives up rather than looping
    assert len(resets) == 1


def test_non_empty_result_is_not_retried(monkeypatch):
    resets = _count_resets(monkeypatch)
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        return {"sector": "Technology"}

    result = yfinance_session.call(fn, is_empty=lambda data: not data)

    assert result == {"sector": "Technology"}
    assert calls["n"] == 1
    assert len(resets) == 0


def test_raised_401_is_refreshed_and_retried_then_propagates(monkeypatch):
    resets = _count_resets(monkeypatch)
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        raise RuntimeError("HTTP Error 401: Invalid Crumb")

    with pytest.raises(RuntimeError):
        yfinance_session.call(fn)

    assert calls["n"] == 2  # tried, dropped the crumb, retried once, then propagated
    assert len(resets) == 1


def test_rate_limit_and_other_errors_are_not_retried(monkeypatch):
    resets = _count_resets(monkeypatch)
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        raise RuntimeError("429 Too Many Requests")  # not a crumb 401

    with pytest.raises(RuntimeError):
        yfinance_session.call(fn)

    assert calls["n"] == 1  # a 429 is left alone — no fresh-crumb retry
    assert len(resets) == 0


def test_hard_feature_gate_is_not_treated_as_a_crumb_401(monkeypatch):
    resets = _count_resets(monkeypatch)
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        raise RuntimeError(
            "HTTP Error 401: User is unable to access this feature - "
            "https://bit.ly/yahoo-finance-api-feedback"
        )

    with pytest.raises(RuntimeError):
        yfinance_session.call(fn)

    # The IP-reputation gate isn't crumb-recoverable, so it isn't retried even though it's a 401.
    assert calls["n"] == 1
    assert len(resets) == 0


def test_frame_is_empty_predicate():
    class _Frame:
        def __init__(self, empty):
            self.empty = empty

    assert yfinance_session.frame_is_empty(None) is True
    assert yfinance_session.frame_is_empty(_Frame(True)) is True
    assert yfinance_session.frame_is_empty(_Frame(False)) is False
