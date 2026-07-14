"""Interface Adapter: the CNN Fear & Greed Index from CNN's dataviz endpoint.

CNN publishes its Fear & Greed Index — a 0–100 composite market-sentiment score —
through an undocumented but widely-used dataviz JSON endpoint. We read the current
``fear_and_greed`` block (score, CNN's rating, timestamp, and the trailing
close/1-week/1-month/1-year comparisons) into a ``FearGreedSnapshot``. It's the
only module that knows CNN backs this read; swap it for another
``FearGreedProvider`` and only this file changes.

**Keyless**, but with two contract notes:

- The host is ``production.dataviz.cnn.io`` (the ``.com`` host is dead), and CNN
  gates the endpoint on the ``User-Agent``: a plain descriptive agent is answered
  with HTTP 418, so we send a ``Mozilla/5.0 (compatible; …)`` agent that still
  identifies us as nama-backend rather than spoofing a specific browser.
- It's an *unofficial* endpoint, so this source is treated **best-effort** by the
  use case — any failure here (transport, non-200, missing block) raises
  ``StockDataUnavailable`` and the combined read simply drops the Fear & Greed leg
  rather than failing. There is no official free API for this index; CNN is the
  canonical source of the number users recognize.

``_http`` is the fake seam the offline tests swap; ``_parse_fear_greed`` is a pure
function the tests drive on a canned payload.
"""

from __future__ import annotations

import datetime
import logging
from typing import Any

import httpx

from app.stocks.exceptions import StockDataUnavailable
from app.stocks.sentiment.entities import FearGreedSnapshot
from app.stocks.sentiment.ports import FearGreedProvider

logger = logging.getLogger(__name__)

_URL = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"

# CNN answers a plain descriptive agent with HTTP 418; a Mozilla-prefixed agent
# clears the gate while still honestly identifying our client.
_USER_AGENT = "Mozilla/5.0 (compatible; nama-backend/1.0; +https://namainsights.com)"

# The index has no per-stock symbol; sentinel for a source-wide failure message.
_FEAR_GREED = "*"


class CnnFearGreedProvider(FearGreedProvider):
    """Reads the current CNN Fear & Greed score from CNN's dataviz feed (keyless)."""

    def __init__(self) -> None:
        self._http = httpx.Client(
            timeout=15.0,
            follow_redirects=True,
            headers={"User-Agent": _USER_AGENT, "Accept": "application/json"},
        )

    def get_fear_greed(self) -> FearGreedSnapshot:
        try:
            resp = self._http.get(_URL)
        except httpx.HTTPError as exc:
            raise StockDataUnavailable(
                _FEAR_GREED, f"CNN Fear & Greed request failed: {exc}"
            ) from exc
        if resp.status_code != 200:
            raise StockDataUnavailable(
                _FEAR_GREED, f"CNN Fear & Greed returned HTTP {resp.status_code}"
            )
        try:
            payload = resp.json()
        except ValueError as exc:
            raise StockDataUnavailable(
                _FEAR_GREED, f"CNN Fear & Greed returned non-JSON: {exc}"
            ) from exc
        snapshot = _parse_fear_greed(payload)
        if snapshot is None:
            raise StockDataUnavailable(
                _FEAR_GREED, "CNN Fear & Greed payload had no usable score"
            )
        return snapshot


def _parse_fear_greed(payload: Any) -> FearGreedSnapshot | None:
    """Parse CNN's graphdata payload into a ``FearGreedSnapshot``.

    Pure function (the tested seam). Returns ``None`` when the ``fear_and_greed``
    block is missing or lacks a usable score/timestamp — the two fields the read
    can't do without. Everything past those (rating + the trailing comparisons)
    is best-effort and left absent when CNN omits it.
    """
    if not isinstance(payload, dict):
        return None
    block = payload.get("fear_and_greed")
    if not isinstance(block, dict):
        return None
    score = _as_float(block.get("score"))
    as_of = _parse_timestamp(block.get("timestamp"))
    if score is None or as_of is None:
        return None
    return FearGreedSnapshot(
        score=round(score, 2),
        as_of=as_of,
        rating=str(block.get("rating") or "").strip(),
        previous_close=_round2(block.get("previous_close")),
        previous_1_week=_round2(block.get("previous_1_week")),
        previous_1_month=_round2(block.get("previous_1_month")),
        previous_1_year=_round2(block.get("previous_1_year")),
    )


def _parse_timestamp(value: Any) -> datetime.datetime | None:
    """Parse CNN's ISO timestamp (``2026-07-14T22:24:38+00:00``), or ``None``."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")  # tolerate a Z suffix
    try:
        return datetime.datetime.fromisoformat(text)
    except ValueError:
        return None


def _as_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _round2(value: Any) -> float | None:
    parsed = _as_float(value)
    return None if parsed is None else round(parsed, 2)
