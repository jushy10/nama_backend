"""Interface Adapter: analyst upgrade/downgrade events from Yahoo Finance (via ``yfinance``).

``Ticker.upgrades_downgrades`` returns the sell-side's individual rating actions as a
date-indexed frame (index ``GradeDate``; columns ``Firm``, ``ToGrade``, ``FromGrade``,
``Action``, ``priceTargetAction``, ``currentPriceTarget``, ``priorPriceTarget``) — the
discrete events that, aggregated by month, become the recommendation *trend*. Keyless, like
the rest of the Yahoo slice.

Yahoo serves the *full* multi-year log (hundreds of rows for a widely-covered name), so the
adapter keeps only the ``_MAX_CHANGES`` most recent — enough for the "recent analyst activity"
read the feature is for, and a bound on what the sync then accumulates. Each kept row becomes a
``RatingChange`` (firm + date is its identity; grades and targets are optional). Rows with no
firm are dropped (nothing to key on), as is a duplicate ``(firm, date)``.

This is the only module (besides the recommendations adapter) that knows Yahoo serves this;
swap it and nothing else changes. Deliberately defensive — Yahoo is an unofficial, best-effort
feed that reshapes payloads without notice and rate-limits data-centre IPs — so any vendor
failure becomes ``StockDataUnavailable`` and a symbol Yahoo doesn't cover yields an empty run
rather than an error. The fetch is routed through ``yfinance_session`` so a transient crumb
401 — which yfinance swallows into an empty frame — is retried once with a fresh crumb.
"""

from __future__ import annotations

from datetime import date, datetime

import pandas as pd
import yfinance as yf

from app.stocks.adapters import yfinance_session
from app.stocks.exceptions import StockDataUnavailable
from app.stocks.recommendations.entities import AnalystRatingChanges, RatingChange
from app.stocks.recommendations.ports import RatingChangeProvider


class YfinanceRatingChangeProvider(RatingChangeProvider):
    """Fetches a stock's individual rating actions from Yahoo (no API key)."""

    # Cap on stored history per stock: Yahoo serves the full multi-year log, but the feature
    # only needs the recent window, and this bounds the table (~this many rows/stock max).
    _MAX_CHANGES = 50

    def __init__(self, *, ticker_factory=None) -> None:
        # Injectable so tests supply a fake Ticker instead of reaching Yahoo.
        self._ticker_factory = ticker_factory or yf.Ticker

    def get_rating_changes(self, symbol: str) -> AnalystRatingChanges:
        try:
            # An empty frame is how yfinance surfaces a swallowed crumb 401, so retry once
            # with a fresh crumb; genuine no-coverage just comes back empty after that.
            frame = yfinance_session.call(
                lambda: self._ticker_factory(symbol).upgrades_downgrades,
                is_empty=yfinance_session.frame_is_empty,
            )
        except Exception as exc:  # noqa: BLE001 — vendor boundary: any failure → domain error
            raise StockDataUnavailable(
                symbol, f"yfinance upgrades/downgrades failed ({exc})"
            ) from exc
        changes = _parse_changes(frame)[: self._MAX_CHANGES]
        return AnalystRatingChanges(symbol=symbol, changes=tuple(changes))


def _parse_changes(frame) -> list[RatingChange]:
    """The upgrades/downgrades frame → entities, newest action first.

    Rows with no firm or no parseable date are dropped (there'd be no identity to key on),
    as is a duplicate ``(firm, date)``. An empty/missing frame — how Yahoo presents an
    uncovered symbol — yields an empty list, not an error. Keeps all pandas/NaN handling here."""
    if frame is None or getattr(frame, "empty", True):
        return []
    try:
        pairs = list(frame.iterrows())
    except Exception:  # noqa: BLE001 — never let a frame quirk escape the adapter
        return []
    seen: set[tuple[str, date]] = set()
    changes: list[RatingChange] = []
    for index, series in pairs:
        firm = _text(_series_get(series, "Firm"))
        if not firm:
            continue
        # Current yfinance carries the date in the index; some payloads add a GradeDate column.
        published_at = _to_date(index) or _to_date(_series_get(series, "GradeDate"))
        if published_at is None:
            continue
        key = (firm, published_at)
        if key in seen:
            continue
        seen.add(key)
        changes.append(
            RatingChange(
                firm=firm,
                published_at=published_at,
                action=_text(_series_get(series, "Action")),
                from_grade=_text(_series_get(series, "FromGrade")),
                to_grade=_text(_series_get(series, "ToGrade")),
                target_current=_target(_series_get(series, "currentPriceTarget")),
                target_prior=_target(_series_get(series, "priorPriceTarget")),
            )
        )
    # Newest first, whatever order Yahoo sent (so the [:MAX] cap keeps the most recent).
    changes.sort(key=lambda change: change.published_at, reverse=True)
    return changes


def _series_get(series, key: str):
    """One labelled value from a row Series, or ``None`` (missing column)."""
    try:
        return series.get(key)
    except Exception:  # noqa: BLE001 — a frame quirk must not escape the adapter
        return None


def _to_date(value) -> date | None:
    """A pandas Timestamp / datetime / date (index or column) → a plain date, or ``None``."""
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        converted = pd.to_datetime(value)
    except (TypeError, ValueError):
        return None
    if converted is None or pd.isna(converted):
        return None
    return converted.date()


def _text(value) -> str | None:
    """A trimmed non-empty string, or ``None`` (missing / NaN / blank — Yahoo uses ``""``
    for a missing grade on an initiation)."""
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    text = str(value).strip()
    return text or None


def _target(value) -> float | None:
    """Coerce a per-firm price target to a positive float, or ``None`` for missing/NaN/non-positive
    (Yahoo writes ``0.0`` for "no target set")."""
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None
