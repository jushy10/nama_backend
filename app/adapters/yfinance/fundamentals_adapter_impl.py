from __future__ import annotations

import yfinance as yf

from app.adapters.yfinance import currency, session
from app.domains.shared.exceptions import StockDataUnavailable
from app.domains.listings.fundamentals.entities import Fundamentals
from app.domains.listings.fundamentals.interfaces import FundamentalsAdapter


class FundamentalsAdapterImpl(FundamentalsAdapter):
    def __init__(self, *, ticker_factory=None) -> None:
        # Injectable so tests supply a fake Ticker instead of reaching Yahoo; defaults to the
        # real yfinance client in production. The same factory builds the FX-pair ticker the
        # currency normalizer needs, so a foreign ADR's rate is faked the same way.
        self._ticker_factory = ticker_factory or yf.Ticker

    def get_fundamentals(self, symbol: str) -> Fundamentals:
        ticker = self._ticker_factory(symbol)
        info = self._read_info(symbol, ticker)  # raises on a hard/blocked read
        # Identity for a US issuer (financialCurrency == currency, no FX call); otherwise the
        # reporting→trading spot conversion for the per-share statement figures.
        normalizer = currency.build(self._ticker_factory, info)
        return Fundamentals(
            gross_margin=_percent_from_fraction(info.get("grossMargins")),
            operating_margin=_percent_from_fraction(info.get("operatingMargins")),
            net_margin=_percent_from_fraction(info.get("profitMargins")),
            return_on_equity=_percent_from_fraction(info.get("returnOnEquity")),
            current_ratio=_number(info.get("currentRatio")),
            debt_to_equity=_ratio_from_percent(info.get("debtToEquity")),
            beta=_number(info.get("beta")),
            book_value_per_share=normalizer.to_trading(_number(info.get("bookValue"))),
            sales_per_share=normalizer.to_trading(_sales_per_share(info)),
            dividend_per_share=_dividend_per_share(info),
            # Enterprise-value inputs. EBITDA / total debt / cash are absolute statement
            # figures in the reporting currency, so they ride the same reporting→trading
            # conversion as the per-share inputs (identity for a US issuer). Shares outstanding
            # is a count — currency-agnostic, so it's left raw.
            ebitda=normalizer.to_trading(_number(info.get("ebitda"))),
            total_debt=normalizer.to_trading(_number(info.get("totalDebt"))),
            cash_and_equivalents=normalizer.to_trading(_number(info.get("totalCash"))),
            shares_outstanding=_positive(_number(info.get("sharesOutstanding"))),
            name=_clean(info.get("longName")) or _clean(info.get("shortName")),
        )

    def _read_info(self, symbol: str, ticker) -> dict:
        try:
            info = session.call(
                lambda: ticker.info,
                is_empty=lambda data: not data,
            )
        except Exception as exc:  # noqa: BLE001 — vendor boundary: any failure → domain error
            raise StockDataUnavailable(
                symbol, f"yfinance fundamentals failed ({exc})"
            ) from exc
        if not info:
            raise StockDataUnavailable(
                symbol,
                "yfinance fundamentals returned an empty .info (crumb 401 / IP block?)",
            )
        return info


def _sales_per_share(info: dict) -> float | None:
    revenue = _number(info.get("totalRevenue"))
    shares = _number(info.get("sharesOutstanding"))
    if revenue is None or shares is None or shares <= 0:
        return None
    return revenue / shares


def _dividend_per_share(info: dict) -> float | None:
    rate = _number(info.get("dividendRate"))
    if rate is None:
        rate = _number(info.get("trailingAnnualDividendRate"))
    return rate if rate else None


def _percent_from_fraction(value: object) -> float | None:
    number = _number(value)
    return None if number is None else number * 100


def _ratio_from_percent(value: object) -> float | None:
    number = _number(value)
    return None if number is None else number / 100


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _positive(value: float | None) -> float | None:
    return value if value is not None and value > 0 else None


def _clean(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None
