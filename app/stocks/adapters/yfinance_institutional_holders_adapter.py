"""Interface Adapter: a stock's institutional ownership from Yahoo Finance (via ``yfinance``).

The live source for the institutional-ownership slice — the top 13F holders and the ownership
breakdown that back the "big money buys and sells" card. It's the only module that knows
Yahoo/``yfinance`` backs this; swap it for another ``InstitutionalOwnershipProvider`` (e.g. a future
SEC 13F reverse-index adapter) and only this file changes. Sibling of the news/earnings yfinance
adapters, and it reuses their crumb-401 retry seam (``yfinance_session``).

Three Yahoo surfaces are read per stock, all keyless:

- ``Ticker.institutional_holders`` — the **primary** feed: the top institutions holding the stock as
  of the latest reported 13F quarter (``Date Reported``, ``Holder``, ``Shares``, ``Value``,
  ``pctHeld``, ``pctChange``). Routed through ``yfinance_session.call`` with a frame-empty predicate
  so a swallowed crumb 401 (an empty frame) is retried once with a fresh crumb. A hard failure
  raises ``StockDataUnavailable``; an empty frame after the retry is genuine no-coverage (an empty
  feed, not an error).
- ``Ticker.mutualfund_holders`` — the same shape for registered funds. **Best-effort enrichment**:
  its failure or absence just omits the fund rows, never sinks the feed.
- ``Ticker.major_holders`` — the **breakdown** summary (``institutionsPercentHeld`` /
  ``insidersPercentHeld`` / ``institutionsFloatPercentHeld`` / ``institutionsCount``). Best-effort:
  a failure or an unexpected shape yields a ``None`` breakdown.

Yahoo reports ``pctHeld`` / ``pctChange`` / the breakdown percents as **fractions** (``0.089`` =
8.9%), so they're multiplied to human percent — the basis the rest of the app stores percents on. A
holder row with no name or no reported date is dropped (nothing to key on); every numeric field is
best-effort and left ``None`` when absent/non-finite.
"""

from __future__ import annotations

import math
from datetime import date, datetime

import yfinance as yf

from app.stocks.adapters import yfinance_session
from app.stocks.exceptions import StockDataUnavailable
from app.stocks.institutional_ownership.entities import (
    HOLDER_TYPE_INSTITUTION,
    HOLDER_TYPE_MUTUAL_FUND,
    InstitutionalHolder,
    InstitutionalOwnership,
    OwnershipBreakdown,
)
from app.stocks.institutional_ownership.ports import InstitutionalOwnershipProvider


class YfinanceInstitutionalHoldersProvider(InstitutionalOwnershipProvider):
    """Fetches a stock's institutional ownership from Yahoo (no API key). Raises
    ``StockDataUnavailable`` on a hard/blocked primary read; best-effort on the fund feed and the
    breakdown past that."""

    def __init__(self, *, ticker_factory=None) -> None:
        # Injectable so tests supply a fake Ticker instead of reaching Yahoo; defaults to the real
        # yfinance client in production.
        self._ticker_factory = ticker_factory or yf.Ticker

    def get_institutional_ownership(self, symbol: str) -> InstitutionalOwnership:
        # Primary feed: a hard failure (including a Ticker-construction failure) raises; an
        # empty-after-retry frame is genuine no-coverage.
        ticker, institutions = self._read_primary(symbol)
        holders = _parse_holders(institutions, HOLDER_TYPE_INSTITUTION)

        # Best-effort fund feed — its failure/absence must never sink the primary feed.
        holders += _parse_holders(
            self._read_best_effort(lambda: ticker.mutualfund_holders),
            HOLDER_TYPE_MUTUAL_FUND,
        )

        breakdown = _parse_breakdown(
            self._read_best_effort(lambda: ticker.major_holders)
        )

        holders.sort(key=_holder_sort_key, reverse=True)  # newest quarter, largest position first
        return InstitutionalOwnership(
            symbol=symbol, breakdown=breakdown, holders=tuple(holders)
        )

    def _read_primary(self, symbol: str):
        """Build the Ticker and read ``institutional_holders`` with the crumb-401 retry, returning
        the ``(ticker, frame)`` pair. The Ticker is built inside the try so a construction failure
        is wrapped too. An empty frame is treated as a (likely swallowed) 401 and retried once; any
        raised error becomes ``StockDataUnavailable``."""
        try:
            ticker = self._ticker_factory(symbol)
            frame = yfinance_session.call(
                lambda: ticker.institutional_holders,
                is_empty=yfinance_session.frame_is_empty,
            )
            return ticker, frame
        except Exception as exc:  # noqa: BLE001 — vendor boundary: any failure → domain error
            raise StockDataUnavailable(
                symbol, f"yfinance institutional holders failed ({exc})"
            ) from exc

    @staticmethod
    def _read_best_effort(fn):
        """Read a best-effort Yahoo surface; any failure degrades to ``None`` (an empty result),
        so it can never sink the primary feed."""
        try:
            return fn()
        except Exception:  # noqa: BLE001 — best-effort enrichment: absence is fine
            return None


def _parse_holders(frame, holder_type: str) -> list[InstitutionalHolder]:
    """A Yahoo holders frame → entities. Rows without a holder name or a parseable reported date are
    dropped (nothing to key on). Handles a missing/empty frame (no coverage) by returning ``[]``."""
    if yfinance_session.frame_is_empty(frame):
        return []
    try:
        records = frame.to_dict("records")
    except Exception:  # noqa: BLE001 — an unexpected frame shape yields no rows, not a crash
        return []
    holders: list[InstitutionalHolder] = []
    for rec in records:
        holder = _clean(rec.get("Holder"))
        reported = _date(rec.get("Date Reported"))
        if holder is None or reported is None:
            continue
        holders.append(
            InstitutionalHolder(
                holder=holder,
                holder_type=holder_type,
                date_reported=reported,
                shares=_num(rec.get("Shares")),
                value=_num(rec.get("Value")),
                pct_held=_pct(_first(rec, "pctHeld", "% Out")),
                pct_change=_pct(_first(rec, "pctChange", "% Change")),
            )
        )
    return holders


def _parse_breakdown(frame) -> OwnershipBreakdown | None:
    """Yahoo's ``major_holders`` frame → the breakdown summary, or ``None``.

    Recent yfinance returns a frame indexed by ``insidersPercentHeld`` / ``institutionsPercentHeld``
    / ``institutionsFloatPercentHeld`` / ``institutionsCount`` with a single ``Value`` column, the
    percents as fractions. Defensive: an unexpected shape yields ``None`` rather than an error."""
    if yfinance_session.frame_is_empty(frame):
        return None
    try:
        data = frame.to_dict()
    except Exception:  # noqa: BLE001 — an unexpected shape is a None breakdown, not a crash
        return None
    value_map = data.get("Value") if isinstance(data, dict) else None
    if not isinstance(value_map, dict):
        return None
    breakdown = OwnershipBreakdown(
        institutions_pct_held=_pct(value_map.get("institutionsPercentHeld")),
        insiders_pct_held=_pct(value_map.get("insidersPercentHeld")),
        institutions_float_pct_held=_pct(value_map.get("institutionsFloatPercentHeld")),
        institutions_count=_int(value_map.get("institutionsCount")),
    )
    return None if breakdown.is_empty else breakdown


def _holder_sort_key(holder: InstitutionalHolder) -> tuple:
    """Newest reported quarter first, largest position (by value) first within a quarter — the
    canonical order the DB repository's serving order mirrors."""
    return (holder.date_reported, holder.value if holder.value is not None else -1.0)


def _first(rec: dict, *keys: str):
    """The first present, non-null value among ``keys`` (Yahoo has renamed these columns across
    versions), or ``None``."""
    for key in keys:
        if key in rec and rec[key] is not None:
            return rec[key]
    return None


def _date(value) -> date | None:
    """A Yahoo reported date (a pandas ``Timestamp`` / ``datetime`` / ISO string) → a ``date``, or
    ``None`` when missing/unparseable. ``pandas.Timestamp`` subclasses ``datetime``, so the datetime
    branch covers it."""
    if value is None:
        return None
    # A pandas NaT (an empty date cell) is *not equal to itself*, like NaN — catch it before the
    # datetime branch, since NaT is a datetime subclass whose ``.date()`` would return NaT again and
    # poison the ordering.
    try:
        if value != value:
            return None
    except Exception:  # noqa: BLE001 — an un-comparable value just isn't a NaT
        pass
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    to_pydatetime = getattr(value, "to_pydatetime", None)
    if callable(to_pydatetime):
        try:
            return to_pydatetime().date()
        except Exception:  # noqa: BLE001 — an odd timestamp is unparseable, not fatal
            return None
    if isinstance(value, str) and value.strip():
        try:
            return datetime.fromisoformat(value.strip()[:10]).date()
        except ValueError:
            return None
    return None


def _num(value) -> float | None:
    """A finite numeric field → ``float``, or ``None`` when absent/non-numeric/non-finite.

    Coerces via ``float()`` rather than an ``isinstance`` gate so a **numpy** scalar — what
    ``DataFrame.to_dict`` yields for numeric columns (``np.int64`` is *not* a Python ``int``) —
    still maps. A pandas ``NaN`` coerces to ``float('nan')`` and is rejected by the finiteness
    check; ``bool`` is rejected up front (never a real figure); a non-numeric string/None raises and
    degrades to ``None``."""
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _pct(value) -> float | None:
    """A Yahoo fraction (``0.089``) → a human percent (``8.9``), or ``None`` when
    absent/non-numeric/non-finite."""
    number = _num(value)
    return None if number is None else number * 100.0


def _int(value) -> int | None:
    """A finite integer count → ``int``, or ``None``. Accepts a float count (Yahoo sometimes types
    ``institutionsCount`` as a float) and truncates it."""
    number = _num(value)
    return None if number is None else int(number)


def _clean(value) -> str | None:
    """A trimmed non-empty string, or ``None`` for a missing/blank/non-string value."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None
