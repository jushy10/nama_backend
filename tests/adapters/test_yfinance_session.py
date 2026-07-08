"""Tests for the shared yfinance retry/pacing helper.

Offline: ``reset_crumb`` is re-patched in each test to a counter (overriding the conftest
no-op stub) so nothing touches Yahoo's real singleton, and the wrapped ``fn`` is a plain
callable. Backoff/pacing default to 0 (env unset), so these run instantly. Verifies the
single crumb-refresh retry on both the swallowed-empty path and the raised-401 path, and
that unrelated errors aren't retried.
"""

import pytest

from app.stocks.adapters import yfinance_session


def _count_resets(monkeypatch) -> list:
    """Patch ``reset_crumb`` to append to (and return) a list, so a test can assert how many
    fresh-crumb refreshes happened."""
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


# --- The optional egress proxy (YF_PROXY_URL) ----------------------------------------------------


def test_proxy_url_is_applied_to_yfinance_when_configured(monkeypatch):
    # With YF_PROXY_URL set, the first call routes yfinance's HTTP through it (set on yfinance's
    # own config, which it re-reads per request). yf.config.network.proxy is restored afterward so
    # the rest of the suite runs direct.
    import yfinance as yf

    monkeypatch.setattr(yfinance_session, "_PROXY_URL", "http://user:pass@proxy.test:8080")
    monkeypatch.setattr(yfinance_session, "_proxy_configured", False)
    original = yf.config.network.proxy
    try:
        yfinance_session.call(lambda: {"ok": True})
        assert yf.config.network.proxy == "http://user:pass@proxy.test:8080"
    finally:
        yf.config.network.proxy = original


def test_no_proxy_url_leaves_yfinance_direct(monkeypatch):
    # The default (unset) never touches yfinance's proxy config — local/tests egress directly.
    import yfinance as yf

    monkeypatch.setattr(yfinance_session, "_PROXY_URL", "")
    monkeypatch.setattr(yfinance_session, "_proxy_configured", False)
    original = yf.config.network.proxy
    try:
        yfinance_session.call(lambda: {"ok": True})
        assert yf.config.network.proxy == original  # untouched
    finally:
        yf.config.network.proxy = original


def test_placeholder_proxy_url_is_ignored(monkeypatch):
    # The SSM secret ships a "REPLACE_ME_VIA_PUT_PARAMETER" placeholder until the real value is set
    # out of band. Without a proxy scheme it must NOT be applied (that would break every Yahoo
    # call) — it's ignored and yfinance stays direct.
    import yfinance as yf

    monkeypatch.setattr(yfinance_session, "_PROXY_URL", "REPLACE_ME_VIA_PUT_PARAMETER")
    monkeypatch.setattr(yfinance_session, "_proxy_configured", False)
    original = yf.config.network.proxy
    try:
        yfinance_session.call(lambda: {"ok": True})
        assert yf.config.network.proxy == original  # placeholder ignored, still direct
    finally:
        yf.config.network.proxy = original


def test_proxy_is_configured_only_once(monkeypatch):
    # The one-time guard: a proxy-config failure (or success) sets the flag so it isn't re-applied
    # on every call. Here a broken yfinance import path would warn once; we assert the guard flips.
    monkeypatch.setattr(yfinance_session, "_PROXY_URL", "http://user:pass@proxy.test:8080")
    monkeypatch.setattr(yfinance_session, "_proxy_configured", False)
    import yfinance as yf

    original = yf.config.network.proxy
    try:
        yfinance_session._ensure_proxy_configured()
        assert yfinance_session._proxy_configured is True
    finally:
        yf.config.network.proxy = original
