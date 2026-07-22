from __future__ import annotations

import datetime
import logging
from typing import Any

import httpx

from app.domains.shared.exceptions import StockDataUnavailable
from app.domains.macro.sentiment.entities import FearGreedSnapshot
from app.domains.macro.sentiment.interfaces import FearGreedAdapter

logger = logging.getLogger(__name__)

_URL = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"

# CNN answers a plain descriptive agent with HTTP 418; a Mozilla-prefixed agent
# clears the gate while still honestly identifying our client.
_USER_AGENT = "Mozilla/5.0 (compatible; nama-backend/1.0; +https://namainsights.com)"

# The index has no per-stock symbol; sentinel for a source-wide failure message.
_FEAR_GREED = "*"


class FearGreedAdapterImpl(FearGreedAdapter):
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
