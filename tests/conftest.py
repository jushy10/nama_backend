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
