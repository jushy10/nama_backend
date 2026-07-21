import pytest

from app.stocks.adapters import yfinance_session


@pytest.fixture(autouse=True)
def _stub_yfinance_crumb_reset(monkeypatch):
    monkeypatch.setattr(yfinance_session, "reset_crumb", lambda: None)


@pytest.fixture(autouse=True)
def _disable_rate_limiter(monkeypatch):
    from app.main import limiter

    monkeypatch.setattr(limiter, "enabled", False)
