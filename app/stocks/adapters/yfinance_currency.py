from __future__ import annotations

import math
from dataclasses import dataclass

from app.stocks.adapters import yfinance_session

# Only *detect* the market-EPS currency when the two currencies are at least this far apart in
# log-space (~ln(1.35), i.e. >~35%). Below it — EUR, GBP, CHF, CAD against USD — the pair is
# too close to tell apart by magnitude *and* the residual error is immaterial, so the market
# surfaces are left on the trading-currency assumption. The *reliably* reporting-currency
# fields are still converted (a small, unambiguous adjustment).
_DETECT_MIN_LOG_FX = 0.30


@dataclass(frozen=True)
class CurrencyNormalizer:
    fx: float = 1.0
    market_fx: float = 1.0

    @property
    def is_identity(self) -> bool:
        return self.fx == 1.0 and self.market_fx == 1.0

    def to_trading(self, value: float | None) -> float | None:
        if value is None:
            return None
        return value * self.fx

    def market_to_trading(self, value: float | None) -> float | None:
        if value is None:
            return None
        return value * self.market_fx


def read_info(ticker) -> dict:
    try:
        info = yfinance_session.call(lambda: ticker.info)
    except Exception:  # noqa: BLE001 — info is optional enrichment
        return {}
    return info if isinstance(info, dict) else {}


def build(
    ticker_factory,
    info: dict | None,
    *,
    market_eps_sample: float | None = None,
    market_eps_reference: float | None = None,
) -> CurrencyNormalizer:
    if not isinstance(info, dict):
        return CurrencyNormalizer()
    reporting = info.get("financialCurrency")
    trading = info.get("currency")
    if not reporting or not trading or reporting == trading:
        return CurrencyNormalizer()
    fx = _fetch_fx_rate(ticker_factory, reporting, trading)
    if fx is None:
        return CurrencyNormalizer()
    market_fx = _resolve_market_fx(fx, market_eps_sample, market_eps_reference)
    return CurrencyNormalizer(fx=fx, market_fx=market_fx)


def _resolve_market_fx(
    fx: float, sample: float | None, reference: float | None
) -> float:
    if abs(math.log(fx)) < _DETECT_MIN_LOG_FX:
        return 1.0
    if not sample or not reference:
        return 1.0
    reference = abs(reference)
    distance_if_trading = abs(math.log(abs(sample) / reference))
    distance_if_reporting = abs(math.log(abs(sample) * fx / reference))
    return fx if distance_if_reporting < distance_if_trading else 1.0


def _fetch_fx_rate(ticker_factory, reporting: str, trading: str) -> float | None:
    pair = f"{reporting}{trading}=X"
    try:
        fx_ticker = ticker_factory(pair)
    except Exception:  # noqa: BLE001 — vendor boundary
        return None
    rate = _fast_info_last_price(fx_ticker)
    if rate is not None:
        return rate
    return _history_last_close(fx_ticker)


def _fast_info_last_price(fx_ticker) -> float | None:
    try:
        fast_info = yfinance_session.call(lambda: fx_ticker.fast_info)
    except Exception:  # noqa: BLE001 — vendor boundary
        return None
    return _positive_float(_lookup(fast_info, "last_price"))


def _history_last_close(fx_ticker) -> float | None:
    try:
        frame = yfinance_session.call(lambda: fx_ticker.history(period="5d"))
    except Exception:  # noqa: BLE001 — vendor boundary
        return None
    try:
        if frame is None or getattr(frame, "empty", True) or "Close" not in frame.columns:
            return None
        closes = frame["Close"].dropna()
        return _positive_float(closes.iloc[-1]) if len(closes) else None
    except Exception:  # noqa: BLE001 — never let a frame quirk escape the adapter
        return None


def _lookup(obj, key: str):
    if obj is None:
        return None
    try:
        if hasattr(obj, "get"):
            return obj.get(key)
        return obj[key]
    except Exception:  # noqa: BLE001
        return getattr(obj, key, None)


def _positive_float(value) -> float | None:
    try:
        rate = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(rate) or rate <= 0:
        return None
    return rate
