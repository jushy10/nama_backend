"""Shared pytest fixtures for the offline suite.

The yfinance retry helper's ``reset_crumb`` reaches into yfinance's process-global ``YfData``
singleton to drop the cached cookie/crumb. That's harmless (no network) but it's real vendor
global state, so stub it to a no-op everywhere to keep the suite hermetic. The retry helper's
own tests (``tests/adapters/test_yfinance_session.py``) re-patch it in-body to count resets.
"""

import pytest

from app.stocks.adapters import yfinance_session


@pytest.fixture(autouse=True)
def _stub_yfinance_crumb_reset(monkeypatch):
    monkeypatch.setattr(yfinance_session, "reset_crumb", lambda: None)


@pytest.fixture(autouse=True)
def _disable_rate_limiter(monkeypatch):
    """Turn the per-IP limiter off for the suite.

    Every ``TestClient`` request carries no ``X-Client-IP`` and shares one
    client host (``testclient``), so the live limiter would pool the whole suite
    into a single bucket and start returning 429s once the cumulative count
    crossed a window — flaky, and unrelated to what most tests assert. The
    limiter's own behaviour is covered by ``tests/test_rate_limiting.py``, which
    re-enables it in-body. ``monkeypatch`` restores the production default (on)
    after each test.
    """
    from app.main import limiter

    monkeypatch.setattr(limiter, "enabled", False)
