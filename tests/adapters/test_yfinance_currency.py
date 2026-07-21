import pytest

from app.stocks.adapters import yfinance_currency
from app.stocks.adapters.yfinance_currency import CurrencyNormalizer


class _FxTicker:
    def __init__(self, rate=None):
        self.fast_info = {} if rate is None else {"last_price": rate}


def _factory(rate):
    fx_ticker = _FxTicker(rate)

    def factory(symbol):
        assert symbol.endswith("=X"), symbol
        return fx_ticker

    return factory


def _forbidden_factory(symbol):  # a factory that must never be called (domestic issuer)
    raise AssertionError(f"FX factory should not be called: {symbol}")


def _info(financial_currency, currency="USD"):
    return {"currency": currency, "financialCurrency": financial_currency}


# --- build(): when a normalizer is (or isn't) constructed --------------------------------


def test_build_is_identity_for_a_domestic_issuer():
    # currency == financialCurrency: no conversion, and no FX call is made at all.
    normalizer = yfinance_currency.build(_forbidden_factory, _info("USD"))
    assert normalizer.is_identity


def test_build_is_identity_when_currency_keys_are_missing():
    assert yfinance_currency.build(_forbidden_factory, {}).is_identity
    assert yfinance_currency.build(_forbidden_factory, {"currency": "USD"}).is_identity


def test_build_is_identity_when_info_is_not_a_dict():
    assert yfinance_currency.build(_forbidden_factory, None).is_identity


def test_build_is_identity_when_the_fx_rate_is_unavailable():
    # A foreign issuer, but the FX pair yields no rate: fall back to identity (never-worse),
    # not a wrong conversion.
    normalizer = yfinance_currency.build(_factory(None), _info("TWD"))
    assert normalizer.is_identity


def test_build_rejects_a_non_positive_fx_rate():
    assert yfinance_currency.build(_factory(0.0), _info("TWD")).is_identity


# --- build(): detecting the market-EPS currency ------------------------------------------


def test_build_detects_trading_currency_market_eps():
    # TSM-like: the 0y market estimate (USD) matches forwardEps (USD) → market surfaces are
    # already trading currency, so only the reliable fields convert (market_fx == 1).
    normalizer = yfinance_currency.build(
        _factory(0.03125), _info("TWD"), market_eps_sample=16.0, market_eps_reference=20.0
    )
    assert normalizer.fx == 0.03125
    assert normalizer.market_fx == 1.0


def test_build_detects_reporting_currency_market_eps():
    # BABA-like: the 0y market estimate (CNY) is ~1/fx of forwardEps (USD) → market surfaces
    # are in the reporting currency, so market_fx picks up the rate too.
    normalizer = yfinance_currency.build(
        _factory(0.15), _info("CNY"), market_eps_sample=45.0, market_eps_reference=9.0
    )
    assert normalizer.fx == 0.15
    assert normalizer.market_fx == 0.15


def test_build_leaves_market_eps_on_trading_currency_for_near_parity():
    # EUR/GBP: fx within the guard → the market currency isn't detected (assumed trading), so
    # market_fx stays 1 while the reliable fields still convert by the (near-1) rate.
    normalizer = yfinance_currency.build(
        _factory(1.1), _info("EUR"), market_eps_sample=45.0, market_eps_reference=9.0
    )
    assert normalizer.fx == 1.1
    assert normalizer.market_fx == 1.0


def test_build_leaves_market_eps_on_trading_currency_without_a_sample():
    # No 0y estimate or no forwardEps to detect against → assume trading currency (market_fx 1).
    normalizer = yfinance_currency.build(
        _factory(0.03125), _info("TWD"), market_eps_sample=None, market_eps_reference=20.0
    )
    assert normalizer.fx == 0.03125 and normalizer.market_fx == 1.0


def test_to_trading_converts_reliable_fields_and_passes_none_through():
    normalizer = CurrencyNormalizer(fx=0.03125, market_fx=1.0)
    assert normalizer.to_trading(320.0) == 10.0  # income-statement figure
    assert normalizer.to_trading(3.2e12) == 1.0e11
    assert normalizer.to_trading(None) is None


def test_market_to_trading_uses_the_market_rate():
    # A reporting-currency market issuer: market EPS converts by market_fx, not left alone.
    normalizer = CurrencyNormalizer(fx=0.15, market_fx=0.15)
    assert normalizer.market_to_trading(45.0) == pytest.approx(6.75)
    assert normalizer.market_to_trading(None) is None


def test_market_to_trading_is_a_no_op_when_market_is_trading_currency():
    # A trading-currency market issuer (TSM): fx set for the reliable fields, but market EPS
    # is left unchanged.
    normalizer = CurrencyNormalizer(fx=0.03125, market_fx=1.0)
    assert normalizer.market_to_trading(16.0) == 16.0
    assert normalizer.to_trading(320.0) == 10.0  # reliable field still converts


def test_identity_normalizer_is_a_no_op():
    normalizer = CurrencyNormalizer()
    assert normalizer.is_identity
    assert normalizer.to_trading(320.0) == 320.0
    assert normalizer.market_to_trading(45.0) == 45.0


# --- read_info(): best-effort ------------------------------------------------------------


def test_read_info_returns_the_dict():
    class _T:
        info = {"currency": "USD"}

    assert yfinance_currency.read_info(_T()) == {"currency": "USD"}


def test_read_info_degrades_to_empty_on_failure():
    class _T:
        @property
        def info(self):
            raise RuntimeError("info blocked")

    assert yfinance_currency.read_info(_T()) == {}


def test_read_info_degrades_to_empty_when_not_a_dict():
    class _T:
        info = None

    assert yfinance_currency.read_info(_T()) == {}
