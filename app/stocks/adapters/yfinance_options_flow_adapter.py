"""Interface Adapter: a stock's options chain from Yahoo Finance (via ``yfinance``), for
the options-flow slice.

``Ticker.options`` lists the symbol's expiration dates and ``Ticker.option_chain(date)``
returns that expiry's calls and puts as two DataFrames — strike, bid/ask, last price,
volume, open interest, implied volatility and an in-the-money flag per contract — plus an
``underlying`` dict carrying the spot quote, all keyless. The adapter maps those onto the
slice's ``OptionContract`` / ``ExpiryChain`` entities; every *derived* figure (premium,
unusual-activity, the aggregates) is domain logic on the entities, not here.

This is the only module that knows Yahoo serves the chain; swap it — for a paid
time-and-sales feed, say — and only this file changes. It is deliberately defensive:
Yahoo is an unofficial, best-effort feed that reshapes payloads without notice and
rate-limits data-centre IPs, so any vendor failure becomes ``StockDataUnavailable`` and a
symbol without listed options yields empty coverage rather than an error.

It is a sibling of ``yfinance_options_adapter.py`` (the ticker card's four summary
metrics) rather than a reuse of it: that adapter returns the *ticker* slice's leaner
contract, this one the *flow* slice's richer ``ExpiryChain`` (with the underlying spot).
The two slices stay independent — the small amount of shared pandas plumbing is the
per-slice-adapter tradeoff the codebase already makes everywhere.
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import yfinance as yf

from app.stocks.exceptions import StockDataUnavailable
from app.stocks.options.entities import ExpiryChain, OptionContract, OptionType
from app.stocks.options.ports import OptionsChainProvider


class YfinanceOptionsChainProvider(OptionsChainProvider):
    """Fetches a stock's option expirations and per-expiry chains from Yahoo (no API key)."""

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
    """The underlying's price from the chain's ``underlying`` dict — best-effort context
    for the at-the-money row. Yahoo carries the live price under a couple of keys
    depending on session; take the first present, falling back to the prior close.
    ``None`` when the dict is missing or carries none of them."""
    if not isinstance(underlying, dict):
        return None
    for key in ("regularMarketPrice", "postMarketPrice", "regularMarketPreviousClose"):
        spot = _float(underlying.get(key))
        if spot is not None and spot > 0:
            return spot
    return None


def _parse_side(frame, expiration: date, option_type: OptionType) -> list[OptionContract]:
    """One side's DataFrame (calls or puts) → entities.

    Rows without a usable strike are dropped (there'd be nothing to anchor the contract
    on); every other field is optional and NaN-tolerant. An empty/missing frame yields an
    empty list, not an error. Keeps all pandas/NaN handling here."""
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
    """Coerce a count to int, treating missing/NaN/malformed as absent (a thin contract's
    unreported volume is unknown, not zero)."""
    parsed = _float(value)
    return None if parsed is None else int(parsed)


def _bool(value) -> bool | None:
    """Coerce the in-the-money flag to bool, treating missing/NaN as absent."""
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    return bool(value)
