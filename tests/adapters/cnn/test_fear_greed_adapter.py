from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import pytest

from app.stocks.adapters.cnn.fear_greed_adapter import (
    CnnFearGreedProvider,
    _parse_fear_greed,
)
from app.stocks.exceptions import StockDataUnavailable

_PAYLOAD = {
    "fear_and_greed": {
        "score": 43.1428571428572,
        "rating": "fear",
        "timestamp": "2026-07-14T22:24:38+00:00",
        "previous_close": 43.7142857142857,
        "previous_1_week": 40.0,
        "previous_1_month": 35.5142857142857,
        "previous_1_year": 76.1142857142857,
    },
    "market_volatility_vix": {"score": 50, "rating": "neutral"},
}


class FakeHttpClient:
    def __init__(self, *, payload=None, status=200, error=None, text=None):
        self._payload = payload
        self._status = status
        self._error = error
        self._text = text
        self.requests: list[str] = []

    def get(self, url):
        self.requests.append(url)
        if self._error is not None:
            raise self._error

        def _json():
            if self._text is not None:
                raise ValueError("not json")
            return self._payload

        return SimpleNamespace(status_code=self._status, json=_json, text=self._text or "")


def _provider(http) -> CnnFearGreedProvider:
    p = CnnFearGreedProvider()
    p._http = http
    return p


def test_parses_block_with_derived_band_and_comparisons():
    snap = _provider(FakeHttpClient(payload=_PAYLOAD)).get_fear_greed()
    assert snap.score == 43.14  # rounded to 2dp
    assert snap.rating == "fear"  # CNN's raw label carried verbatim
    assert snap.band.value == "fear"  # derived from the score (25–44)
    assert snap.label == "Fear"
    assert snap.as_of == datetime(2026, 7, 14, 22, 24, 38, tzinfo=timezone.utc)
    assert snap.previous_close == 43.71
    assert snap.previous_1_week == 40.0
    assert snap.previous_1_year == 76.11


def test_score_drives_band_thresholds():
    # canonical CNN bands: 0–24 EF, 25–44 F, 45–55 N, 56–75 G, 76–100 EG
    def band(score: float) -> str:
        payload = {"fear_and_greed": {"score": score, "timestamp": "2026-07-14T00:00:00+00:00"}}
        return _parse_fear_greed(payload).band.value

    assert band(10) == "extreme_fear"
    assert band(50) == "neutral"
    assert band(80) == "extreme_greed"


def test_tolerates_z_suffix_timestamp():
    payload = {"fear_and_greed": {"score": 50, "timestamp": "2026-07-14T22:24:38Z"}}
    snap = _parse_fear_greed(payload)
    assert snap.as_of == datetime(2026, 7, 14, 22, 24, 38, tzinfo=timezone.utc)


def test_missing_block_raises_unavailable():
    with pytest.raises(StockDataUnavailable):
        _provider(FakeHttpClient(payload={"market_volatility_vix": {}})).get_fear_greed()


def test_missing_score_returns_none_from_parser():
    assert _parse_fear_greed({"fear_and_greed": {"timestamp": "2026-07-14T00:00:00+00:00"}}) is None


def test_unparseable_timestamp_returns_none_from_parser():
    assert _parse_fear_greed({"fear_and_greed": {"score": 50, "timestamp": "not-a-date"}}) is None


def test_non_200_raises_unavailable():
    # CNN answers a blocked agent with 418; any non-200 is an outage.
    with pytest.raises(StockDataUnavailable):
        _provider(FakeHttpClient(status=418)).get_fear_greed()


def test_non_json_raises_unavailable():
    with pytest.raises(StockDataUnavailable):
        _provider(FakeHttpClient(text="<html>blocked</html>")).get_fear_greed()


def test_transport_error_raises_unavailable():
    with pytest.raises(StockDataUnavailable):
        _provider(FakeHttpClient(error=httpx.ConnectError("boom"))).get_fear_greed()
