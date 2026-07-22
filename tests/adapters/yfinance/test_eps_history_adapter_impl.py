from datetime import date

import pandas as pd
import pytest

from app.adapters.yfinance.eps_history_adapter_impl import (
    EpsHistoryAdapterImpl,
)
from app.domains.shared.exceptions import StockDataUnavailable

_NAN = float("nan")


def _earnings_dates(rows: list[tuple[str, float]]) -> pd.DataFrame:
    index = pd.DatetimeIndex([pd.Timestamp(d) for d, _ in rows])
    return pd.DataFrame({"Reported EPS": [eps for _, eps in rows]}, index=index)


def _estimate_frame(avgs: dict) -> pd.DataFrame:
    return pd.DataFrame.from_dict(
        {label: {"avg": value} for label, value in avgs.items()}, orient="index"
    )


class FakeTicker:
    def __init__(
        self, *, earnings_dates=None, eps_estimate=None, info=None, error=None
    ) -> None:
        self._earnings_dates = earnings_dates
        self._eps_estimate = eps_estimate
        self._info = info if info is not None else {}
        self._error = error
        self.requested_limit: int | None = None

    def get_earnings_dates(self, limit: int = 12):
        self.requested_limit = limit
        if self._error is not None:
            raise self._error
        return self._earnings_dates

    @property
    def info(self):
        return self._info

    @property
    def earnings_estimate(self):
        return self._eps_estimate


def _provider(fake: FakeTicker, **kwargs) -> EpsHistoryAdapterImpl:
    return EpsHistoryAdapterImpl(ticker_factory=lambda _symbol: fake, **kwargs)


def test_parses_reported_quarters_oldest_first():
    # Rows out of order, with two future (NaN) quarters that must drop out.
    fake = FakeTicker(
        earnings_dates=_earnings_dates(
            [
                ("2025-10-30", _NAN),  # future — no reported EPS yet
                ("2024-11-01", 1.29),
                ("2025-02-01", 2.40),
                ("2024-08-01", 1.40),
                ("2025-08-01", _NAN),  # future
                ("2025-05-01", 1.65),
            ]
        )
    )
    history = _provider(fake).get_eps_history("AAPL")

    assert [(str(p.report_date), p.eps) for p in history] == [
        ("2024-08-01", 1.40),
        ("2024-11-01", 1.29),
        ("2025-02-01", 2.40),
        ("2025-05-01", 1.65),
    ]


def test_dedupes_by_date_keeping_a_reported_value():
    # Yahoo can list a boundary quarter twice; a single reported figure survives per date.
    fake = FakeTicker(
        earnings_dates=_earnings_dates([("2025-02-01", 2.40), ("2025-02-01", 2.41)])
    )
    history = _provider(fake).get_eps_history("AAPL")
    assert len(history) == 1
    assert history[0].report_date == date(2025, 2, 1)


def test_empty_frame_is_no_coverage_not_an_error():
    assert _provider(FakeTicker(earnings_dates=pd.DataFrame())).get_eps_history("X") == ()


def test_none_frame_is_no_coverage():
    assert _provider(FakeTicker(earnings_dates=None)).get_eps_history("X") == ()


def test_all_future_rows_yield_empty():
    fake = FakeTicker(
        earnings_dates=_earnings_dates([("2025-10-30", _NAN), ("2026-02-01", _NAN)])
    )
    assert _provider(fake).get_eps_history("X") == ()


def test_vendor_failure_becomes_domain_error():
    fake = FakeTicker(error=RuntimeError("yahoo blocked the data-centre IP"))
    with pytest.raises(StockDataUnavailable):
        _provider(fake).get_eps_history("AAPL")


def test_requests_the_configured_depth():
    fake = FakeTicker(earnings_dates=_earnings_dates([("2025-02-01", 2.40)]))
    _provider(fake, limit=40).get_eps_history("AAPL")
    assert fake.requested_limit == 40


# --- foreign ADRs: reporting→trading currency normalization -------------------------------
#
# "Reported EPS" is a *market* EPS surface, quoted per-ADR in a currency that varies by issuer
# (USD for TSM, the reporting currency for BABA). The adapter detects that currency once — the
# earnings_estimate 0y forward annual estimate against info['forwardEps'] — and runs each
# reported EPS through the shared normalizer, so it divides cleanly into the USD P/E-history
# prices. (The detection logic itself is exhaustively covered in test_yfinance_currency.py;
# these check the adapter wires it in and applies it to every reported EPS.)


class _FxTicker:
    def __init__(self, rate):
        self.fast_info = {} if rate is None else {"last_price": rate}


def _provider_with_currency(fake: FakeTicker, *, fx_rate) -> EpsHistoryAdapterImpl:
    fx_ticker = _FxTicker(fx_rate)

    def factory(symbol):
        return fx_ticker if symbol.endswith("=X") else fake

    return EpsHistoryAdapterImpl(ticker_factory=factory)


def _adr_info(*, financial_currency, forward_eps, currency="USD"):
    return {
        "currency": currency,
        "financialCurrency": financial_currency,
        "forwardEps": forward_eps,
    }


def test_foreign_adr_leaves_usd_market_eps_unconverted():
    # TSM-like: the market EPS is already USD (the 0y estimate ~ forwardEps), so the reported
    # EPS is left alone even though the issuer reports in TWD. fx = 1/32 = 0.03125.
    fake = FakeTicker(
        earnings_dates=_earnings_dates(
            [("2024-11-01", 1.29), ("2025-02-01", 2.40), ("2025-05-01", 1.65)]
        ),
        eps_estimate=_estimate_frame({"0y": 12.0}),  # USD, ~ forwardEps
        info=_adr_info(financial_currency="TWD", forward_eps=13.0),
    )
    history = _provider_with_currency(fake, fx_rate=0.03125).get_eps_history("TSM")
    assert [(str(p.report_date), p.eps) for p in history] == [
        ("2024-11-01", 1.29),
        ("2025-02-01", 2.40),
        ("2025-05-01", 1.65),
    ]


def test_foreign_adr_converts_reporting_currency_market_eps():
    # BABA-like: the reported EPS is in the reporting currency (CNY), detected from the 0y
    # estimate (CNY) against forwardEps (USD) and converted onto USD. fx = 0.15.
    fake = FakeTicker(
        earnings_dates=_earnings_dates(
            [("2025-02-01", 14.0), ("2025-05-01", 13.0), ("2025-08-01", 15.0)]
        ),
        eps_estimate=_estimate_frame({"0y": 80.0}),  # CNY (≈ forwardEps / fx)
        info=_adr_info(financial_currency="CNY", forward_eps=12.0),
    )
    history = _provider_with_currency(fake, fx_rate=0.15).get_eps_history("BABA")
    assert [str(p.report_date) for p in history] == [
        "2025-02-01",
        "2025-05-01",
        "2025-08-01",
    ]
    assert [p.eps for p in history] == pytest.approx([14.0 * 0.15, 13.0 * 0.15, 15.0 * 0.15])


def test_foreign_adr_without_an_fx_rate_leaves_eps_unconverted():
    # The FX pair yields no rate → identity normalizer (never-worse), so the CNY EPS is served
    # unconverted rather than wrongly scaled.
    fake = FakeTicker(
        earnings_dates=_earnings_dates([("2025-05-01", 13.0)]),
        eps_estimate=_estimate_frame({"0y": 80.0}),
        info=_adr_info(financial_currency="CNY", forward_eps=12.0),
    )
    history = _provider_with_currency(fake, fx_rate=None).get_eps_history("BABA")
    assert [p.eps for p in history] == [13.0]  # unconverted


def test_domestic_issuer_makes_no_fx_call():
    # financialCurrency == currency (USD): no conversion, and the FX pair must never be fetched.
    fake = FakeTicker(
        earnings_dates=_earnings_dates([("2025-05-01", 1.65)]),
        eps_estimate=_estimate_frame({"0y": 6.5}),
        info=_adr_info(financial_currency="USD", forward_eps=6.5),  # == currency
    )

    def factory(symbol):
        if symbol.endswith("=X"):
            raise AssertionError("a domestic issuer must not fetch an FX rate")
        return fake

    history = EpsHistoryAdapterImpl(ticker_factory=factory).get_eps_history("AAPL")
    assert [p.eps for p in history] == [1.65]
