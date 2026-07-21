import pytest

from app.stocks.adapters.yfinance.fundamentals_adapter_impl import (
    FundamentalsAdapterImpl,
)
from app.stocks.exceptions import StockDataUnavailable

# An AAPL-shaped ``.info`` at the raw units Yahoo returns (see the adapter's docstring):
# margins/ROE are fractions, debtToEquity is a percent, currentRatio/beta are plain figures.
_AAPL_INFO = {
    "currency": "USD",
    "financialCurrency": "USD",
    "grossMargins": 0.44,  # fraction -> 44.0%
    "operatingMargins": 0.30,  # -> 30.0%
    "profitMargins": 0.25,  # -> 25.0% (net margin)
    "returnOnEquity": 1.474,  # -> 147.4%
    "currentRatio": 0.87,  # as-is
    "debtToEquity": 154.0,  # percent -> ratio 1.54
    "beta": 1.24,  # as-is
    "bookValue": 4.2,  # per share (USD) -> P/B input
    "totalRevenue": 400_000_000_000,  # -> sales_per_share = revenue / shares
    "sharesOutstanding": 16_000_000_000,  # -> 400e9 / 16e9 = 25.0
    "dividendRate": 1.0,  # annual dividend per share
    "ebitda": 130_000_000_000,  # absolute (USD) -> EV input
    "totalDebt": 100_000_000_000,  # absolute (USD) -> EV input
    "totalCash": 60_000_000_000,  # absolute (USD) -> EV input
}


class _FakeTicker:
    def __init__(self, info, *, info_error=None, fast_info=None):
        self._info = info
        self._info_error = info_error
        self.fast_info = fast_info  # only the FX-pair ticker needs this

    @property
    def info(self):
        if self._info_error is not None:
            raise self._info_error
        return self._info


def _provider(info=None, *, info_error=None) -> FundamentalsAdapterImpl:
    ticker = _FakeTicker(info if info is not None else dict(_AAPL_INFO), info_error=info_error)
    return FundamentalsAdapterImpl(ticker_factory=lambda symbol: ticker)


def test_maps_and_normalizes_every_field():
    f = _provider().get_fundamentals("AAPL")

    assert f.gross_margin == pytest.approx(44.0)
    assert f.operating_margin == pytest.approx(30.0)
    assert f.net_margin == pytest.approx(25.0)
    assert f.return_on_equity == pytest.approx(147.4)
    assert f.current_ratio == pytest.approx(0.87)  # plain figure, unscaled
    assert f.debt_to_equity == pytest.approx(1.54)  # percent -> ratio
    assert f.beta == pytest.approx(1.24)  # plain figure
    assert f.book_value_per_share == pytest.approx(4.2)
    assert f.sales_per_share == pytest.approx(25.0)  # 400e9 / 16e9
    assert f.dividend_per_share == pytest.approx(1.0)
    # EV inputs: absolute dollar figures (USD issuer -> no conversion), shares a raw count.
    assert f.ebitda == pytest.approx(130_000_000_000)
    assert f.total_debt == pytest.approx(100_000_000_000)
    assert f.cash_and_equivalents == pytest.approx(60_000_000_000)
    assert f.shares_outstanding == pytest.approx(16_000_000_000)


def test_missing_fields_degrade_to_none():
    # A reachable-but-sparse .info: only a couple of fields present.
    f = _provider({"currency": "USD", "grossMargins": 0.5}).get_fundamentals("X")
    assert f.gross_margin == pytest.approx(50.0)
    assert f.net_margin is None
    assert f.current_ratio is None
    assert f.book_value_per_share is None
    assert f.sales_per_share is None  # needs both revenue and shares
    assert f.dividend_per_share is None
    assert f.ebitda is None
    assert f.total_debt is None
    assert f.cash_and_equivalents is None
    assert f.shares_outstanding is None


def test_dividend_falls_back_to_trailing_and_a_non_payer_is_none():
    paid = _provider(
        {"currency": "USD", "trailingAnnualDividendRate": 0.92}
    ).get_fundamentals("X")
    assert paid.dividend_per_share == pytest.approx(0.92)  # fallback when dividendRate absent

    non_payer = _provider({"currency": "USD", "dividendRate": 0.0}).get_fundamentals("X")
    assert non_payer.dividend_per_share is None  # a 0 non-payer reads as absent, not zero


def test_sales_per_share_none_without_positive_shares():
    f = _provider(
        {"currency": "USD", "totalRevenue": 1_000, "sharesOutstanding": 0}
    ).get_fundamentals("X")
    assert f.sales_per_share is None


def test_empty_info_raises_the_block_signal():
    # An empty .info (after the crumb retry) is Yahoo's swallowed-401 / IP-block signal -> the
    # sweep must skip the stock and leave its stored figures intact.
    with pytest.raises(StockDataUnavailable):
        _provider({}).get_fundamentals("AAPL")


def test_a_raised_info_read_becomes_domain_unavailable():
    with pytest.raises(StockDataUnavailable):
        _provider(info_error=RuntimeError("boom")).get_fundamentals("AAPL")


def test_foreign_adr_reporting_figures_are_converted_to_trading_currency():
    # A TWD-reporting ADR trading in USD: bookValue + totalRevenue are in TWD (the filing
    # currency) and must be converted onto the USD trading currency, while the margins (ratios)
    # and the dividend (already trading currency) are left untouched.
    info = {
        "currency": "USD",
        "financialCurrency": "TWD",
        "grossMargins": 0.5,  # ratio -> unchanged 50.0%
        "bookValue": 320.0,  # TWD/share -> x (1/32) -> 10.0 USD
        "totalRevenue": 3_200_000_000_000,  # TWD -> /1e9 shares -> 3200 TWD/sh -> x(1/32) -> 100
        "sharesOutstanding": 1_000_000_000,
        "dividendRate": 2.0,  # trading currency (USD) -> unchanged
        "ebitda": 320_000_000_000,  # TWD absolute -> x(1/32) -> 10e9 USD
        "totalDebt": 32_000_000_000,  # TWD absolute -> x(1/32) -> 1e9 USD
        "totalCash": 64_000_000_000,  # TWD absolute -> x(1/32) -> 2e9 USD
    }
    stock = _FakeTicker(info)
    fx = _FakeTicker({}, fast_info={"last_price": 1 / 32})  # TWDUSD=X: USD per TWD

    def factory(symbol):
        return fx if symbol == "TWDUSD=X" else stock

    f = FundamentalsAdapterImpl(ticker_factory=factory).get_fundamentals("TSM")

    assert f.gross_margin == pytest.approx(50.0)  # ratio: currency-agnostic
    assert f.book_value_per_share == pytest.approx(10.0)  # 320 / 32
    assert f.sales_per_share == pytest.approx(100.0)  # 3200 TWD/sh / 32
    assert f.dividend_per_share == pytest.approx(2.0)  # already trading currency
    # The EV absolute figures ride the same reporting->trading conversion; shares (a count) don't.
    assert f.ebitda == pytest.approx(10_000_000_000)  # 320e9 TWD / 32
    assert f.total_debt == pytest.approx(1_000_000_000)  # 32e9 TWD / 32
    assert f.cash_and_equivalents == pytest.approx(2_000_000_000)  # 64e9 TWD / 32
    assert f.shares_outstanding == pytest.approx(1_000_000_000)  # count: currency-agnostic


def test_shares_outstanding_is_positive_only():
    # A zero/negative share count is junk (it would divide an enterprise value), so it's nulled.
    f = _provider({"currency": "USD", "sharesOutstanding": 0}).get_fundamentals("X")
    assert f.shares_outstanding is None
