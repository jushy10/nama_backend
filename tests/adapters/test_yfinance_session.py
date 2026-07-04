"""Tests for the shared yfinance retry/pacing helper.

Offline: ``reset_crumb`` is monkeypatched to a counter so nothing touches Yahoo's real
singleton, and the wrapped ``fn`` is a plain callable. Backoff/pacing default to 0 (env
unset), so these run instantly. Verifies the single crumb-refresh retry on both the
swallowed-empty path and the raised-401 path, and that unrelated errors aren't retried.
"""

import pytest

from app.stocks.adapters import yfinance_session


@pytest.fixture(autouse=True)
def _no_real_crumb_reset(monkeypatch):
    """Count crumb resets instead of poking yfinance's real global state."""
    resets = {"n": 0}

    def _fake_reset():
        resets["n"] += 1

    monkeypatch.setattr(yfinance_session, "reset_crumb", _fake_reset)
    return resets


def test_empty_result_triggers_one_crumb_refresh_and_retry(_no_real_crumb_reset):
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        # First call comes back empty (a swallowed crumb 401); the retry succeeds.
        return {} if calls["n"] == 1 else {"sector": "Technology"}

    result = yfinance_session.call(fn, is_empty=lambda data: not data)

    assert result == {"sector": "Technology"}
    assert calls["n"] == 2  # retried exactly once
    assert _no_real_crumb_reset["n"] == 1  # crumb dropped before the retry


def test_still_empty_after_retry_returns_empty(_no_real_crumb_reset):
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        return {}  # Yahoo genuinely has nothing — stays empty after the one retry

    result = yfinance_session.call(fn, is_empty=lambda data: not data)

    assert result == {}
    assert calls["n"] == 2  # one retry, then gives up rather than looping
    assert _no_real_crumb_reset["n"] == 1


def test_non_empty_result_is_not_retried(_no_real_crumb_reset):
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        return {"sector": "Technology"}

    result = yfinance_session.call(fn, is_empty=lambda data: not data)

    assert result == {"sector": "Technology"}
    assert calls["n"] == 1
    assert _no_real_crumb_reset["n"] == 0


def test_raised_401_is_refreshed_and_retried_then_propagates(_no_real_crumb_reset):
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        raise RuntimeError("HTTP Error 401: Invalid Crumb")

    with pytest.raises(RuntimeError):
        yfinance_session.call(fn)

    assert calls["n"] == 2  # tried, dropped the crumb, retried once, then propagated
    assert _no_real_crumb_reset["n"] == 1


def test_rate_limit_and_other_errors_are_not_retried(_no_real_crumb_reset):
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        raise RuntimeError("429 Too Many Requests")  # not a crumb 401

    with pytest.raises(RuntimeError):
        yfinance_session.call(fn)

    assert calls["n"] == 1  # a 429 is left alone — no fresh-crumb retry
    assert _no_real_crumb_reset["n"] == 0


def test_hard_feature_gate_is_not_treated_as_a_crumb_401(_no_real_crumb_reset):
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
    assert _no_real_crumb_reset["n"] == 0
