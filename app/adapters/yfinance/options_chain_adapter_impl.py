from __future__ import annotations

from datetime import date

import pandas as pd
import yfinance as yf

from app.domains.shared.exceptions import StockDataUnavailable
from app.domains.pricing.options.entities import ExpiryChain, OptionContract, OptionType
from app.domains.pricing.options.interfaces import OptionsChainAdapter


class OptionsChainAdapterImpl(OptionsChainAdapter):
    def __init__(self, *, ticker_factory=None) -> None:
        # Injectable so tests supply a fake Ticker (canned frames) instead of reaching
        # Yahoo; defaults to the real thing.
        self._ticker_factory = ticker_factory or yf.Ticker

    def get_expirations(self, symbol: str) -> tuple[date, ...]:
        try:
            labels = self._ticker_factory(symbol).options or ()
        except Exception as exc:  # noqa: BLE001 — vendor boundary: any failure → domain error
            raise StockDataUnavailable(
                symbol, f"yfinance option expirations failed ({exc})"
            ) from exc
        expirations = []
        for label in labels:
            try:
                expirations.append(date.fromisoformat(str(label)))
            except ValueError:
                continue  # an unparseable label is a row we can't key on — drop it
        return tuple(sorted(expirations))

    def get_chain(self, symbol: str, expiration: date) -> ExpiryChain:
        try:
            chain = self._ticker_factory(symbol).option_chain(expiration.isoformat())
        except Exception as exc:  # noqa: BLE001 — vendor boundary: any failure → domain error
            raise StockDataUnavailable(
                symbol, f"yfinance option chain failed ({exc})"
            ) from exc
        calls = _parse_side(getattr(chain, "calls", None), expiration, OptionType.CALL)
        puts = _parse_side(getattr(chain, "puts", None), expiration, OptionType.PUT)
        return ExpiryChain(
            expiration=expiration,
            spot=_underlying_spot(getattr(chain, "underlying", None)),
            contracts=tuple(calls + puts),
        )


def _underlying_spot(underlying) -> float | None:
    if not isinstance(underlying, dict):
        return None
    for key in ("regularMarketPrice", "postMarketPrice", "regularMarketPreviousClose"):
        spot = _float(underlying.get(key))
        if spot is not None and spot > 0:
            return spot
    return None


def _parse_side(frame, expiration: date, option_type: OptionType) -> list[OptionContract]:
    if frame is None or getattr(frame, "empty", True):
        return []
    try:
        rows = list(frame.iterrows())
    except Exception:  # noqa: BLE001 — never let a frame quirk escape the adapter
        return []
    contracts: list[OptionContract] = []
    for _, series in rows:
        strike = _float(_series_get(series, "strike"))
        if strike is None or strike <= 0:
            continue
        contracts.append(
            OptionContract(
                expiration=expiration,
                strike=strike,
                option_type=option_type,
                bid=_float(_series_get(series, "bid")),
                ask=_float(_series_get(series, "ask")),
                last_price=_float(_series_get(series, "lastPrice")),
                volume=_int(_series_get(series, "volume")),
                open_interest=_int(_series_get(series, "openInterest")),
                implied_volatility=_float(_series_get(series, "impliedVolatility")),
                in_the_money=_bool(_series_get(series, "inTheMoney")),
            )
        )
    return contracts


def _series_get(series, key: str):
    try:
        return series.get(key)
    except Exception:  # noqa: BLE001 — a frame quirk must not escape the adapter
        return None


def _float(value) -> float | None:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int(value) -> int | None:
    parsed = _float(value)
    return None if parsed is None else int(parsed)


def _bool(value) -> bool | None:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    return bool(value)
