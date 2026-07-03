"""Interface Adapter: a stock's options chain from Yahoo Finance (via ``yfinance``).

``Ticker.options`` lists the symbol's expiration dates and ``Ticker.option_chain(date)``
returns that expiry's calls and puts as two DataFrames — strike, bid/ask, last price,
volume, open interest and implied volatility per contract, keyless. The adapter maps
those rows onto the ticker slice's ``OptionContract`` entities; every derived figure
(ATM IV, expected move, insurance cost, put/call ratio) is domain logic and lives on
the entity, not here.

This is the only module that knows Yahoo serves the chain; swap it and nothing else
changes. It is deliberately defensive — Yahoo is an unofficial, best-effort feed that
reshapes payloads without notice and rate-limits data-centre IPs — so any vendor
failure becomes ``StockDataUnavailable``, and a symbol without listed options yields
empty coverage rather than an error. The card treats the whole read as best-effort,
so a blocked call just leaves the block null.
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import yfinance as yf

from app.stocks.exceptions import StockDataUnavailable
from app.stocks.ticker.entities import OptionContract
from app.stocks.ticker.ports import OptionChainProvider


class YfinanceOptionChainProvider(OptionChainProvider):
    """Fetches a stock's option expirations and per-expiry chains from Yahoo (no API key)."""

    def __init__(self, *, ticker_factory=None) -> None:
        # Injectable so tests supply a fake Ticker (canned frames) instead of
        # reaching Yahoo; defaults to the real thing.
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

    def get_chain(self, symbol: str, expiration: date) -> tuple[OptionContract, ...]:
        try:
            chain = self._ticker_factory(symbol).option_chain(expiration.isoformat())
        except Exception as exc:  # noqa: BLE001 — vendor boundary: any failure → domain error
            raise StockDataUnavailable(
                symbol, f"yfinance option chain failed ({exc})"
            ) from exc
        calls = _parse_side(getattr(chain, "calls", None), expiration, is_call=True)
        puts = _parse_side(getattr(chain, "puts", None), expiration, is_call=False)
        return tuple(calls + puts)


def _parse_side(frame, expiration: date, *, is_call: bool) -> list[OptionContract]:
    """One side's DataFrame (calls or puts) → entities.

    Rows without a usable strike are dropped (there'd be nothing to anchor the
    contract on); every other field is optional and NaN-tolerant. An empty/missing
    frame yields an empty list, not an error. Keeps all pandas/NaN handling here."""
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
                is_call=is_call,
                bid=_float(_series_get(series, "bid")),
                ask=_float(_series_get(series, "ask")),
                last_price=_float(_series_get(series, "lastPrice")),
                volume=_int(_series_get(series, "volume")),
                open_interest=_int(_series_get(series, "openInterest")),
                implied_volatility=_float(_series_get(series, "impliedVolatility")),
            )
        )
    return contracts


def _series_get(series, key: str):
    """One labelled value from a row Series, or ``None`` (missing column)."""
    try:
        return series.get(key)
    except Exception:  # noqa: BLE001 — a frame quirk must not escape the adapter
        return None


def _float(value) -> float | None:
    """Coerce a price/ratio to float, treating missing/NaN/malformed as absent."""
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
    """Coerce a count to int, treating missing/NaN/malformed as absent (a thin
    contract's unreported volume is unknown, not zero)."""
    parsed = _float(value)
    return None if parsed is None else int(parsed)
