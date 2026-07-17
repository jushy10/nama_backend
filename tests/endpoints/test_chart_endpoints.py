"""Tests for the chart read endpoints (candles / ema / support-levels / trend /
indicators).

Offline: a fake CandleProvider is injected through dependency_overrides on the
market-routing ``get_price_provider`` factory, so these exercise the *real* use cases and the pure
indicator math end-to-end — only the vendor fetch is faked. They cover the new
unified ``/indicators`` endpoint (spec parsing, the JSON shape, overlay flags,
window trimming, error mapping) and smoke-test that the four pre-existing endpoints
still serve after the shared window/error-translation refactor.
"""

from datetime import datetime, timedelta, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.stocks.endpoints import chart_endpoints as endpoints
from app.stocks.charts.ports import CandleProvider
from app.stocks.entities import Candle, CandleSeries, Timeframe
from app.stocks.exceptions import StockDataUnavailable, StockNotFound
from app.stocks.wiring import get_price_provider


class _FakeCandleProvider(CandleProvider):
    """Returns a canned candle series (ignoring the window) or raises."""

    def __init__(self, *, series: CandleSeries | None = None, error=None) -> None:
        self._series = series
        self._error = error
        self.calls: list[tuple] = []

    def get_candles(self, symbol, timeframe, *, start, end) -> CandleSeries:
        self.calls.append((symbol, timeframe, start, end))
        if self._error is not None:
            raise self._error
        return self._series


def _rising_series(count: int = 60) -> CandleSeries:
    """A daily series of steadily rising bars — enough history to compute every
    indicator (the deepest default lookback is MACD's 26 + 9)."""
    base = datetime(2026, 6, 1, tzinfo=timezone.utc)
    candles = tuple(
        Candle(
            timestamp=base + timedelta(days=i),
            open=float(100 + i),
            high=float(100 + i) + 1.0,
            low=float(100 + i) - 1.0,
            close=float(100 + i),
            volume=1000 + i,
        )
        for i in range(count)
    )
    return CandleSeries(symbol="AAPL", timeframe=Timeframe.DAY_1, candles=candles)


def _client(fake: _FakeCandleProvider) -> TestClient:
    app = FastAPI()
    app.include_router(endpoints.router)
    # The chart endpoints ride the market-routing price provider; override it with the fake
    # CandleProvider (the router slot accepts any CandleProvider), so these stay offline.
    app.dependency_overrides[get_price_provider] = lambda: fake
    return TestClient(app)


# --------------------------- /indicators: shape ---------------------------


def test_indicators_returns_requested_set_in_order():
    fake = _FakeCandleProvider(series=_rising_series())
    resp = _client(fake).get("/stocks/ticker/AAPL/indicators?indicator=rsi,macd&range=MAX")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["symbol"] == "AAPL"
    assert body["count"] == 2
    assert [ind["name"] for ind in body["indicators"]] == ["rsi", "macd"]

    rsi = body["indicators"][0]
    assert rsi["label"] == "RSI (14)"
    assert rsi["overlay"] is False
    assert [line["key"] for line in rsi["lines"]] == ["rsi"]
    point = rsi["lines"][0]["points"][-1]
    assert {"time", "timestamp", "value"} <= point.keys()
    assert rsi["lines"][0]["latest"] == 100.0  # strictly rising -> RSI pinned at 100

    macd = body["indicators"][1]
    assert [line["key"] for line in macd["lines"]] == ["macd", "signal", "histogram"]


def test_indicators_marks_overlays():
    fake = _FakeCandleProvider(series=_rising_series())
    resp = _client(fake).get(
        "/stocks/ticker/AAPL/indicators?indicator=sma:20,vwap,rsi&range=MAX"
    )
    assert resp.status_code == 200, resp.text
    overlay = {ind["name"]: ind["overlay"] for ind in resp.json()["indicators"]}
    assert overlay == {"sma": True, "vwap": True, "rsi": False}


def test_indicators_period_override_and_dedup():
    fake = _FakeCandleProvider(series=_rising_series())
    # Two SMA periods requested; an exact duplicate rsi is collapsed.
    resp = _client(fake).get(
        "/stocks/ticker/AAPL/indicators?indicator=sma:50,sma:200,rsi,rsi&range=MAX"
    )
    assert resp.status_code == 200, resp.text
    labels = [ind["label"] for ind in resp.json()["indicators"]]
    assert labels == ["SMA (50)", "SMA (200)", "RSI (14)"]


def test_indicators_single_fetch_for_the_whole_set():
    fake = _FakeCandleProvider(series=_rising_series())
    _client(fake).get(
        "/stocks/ticker/AAPL/indicators?indicator=rsi,macd,bbands,atr,adx&range=MAX"
    )
    # One provider round-trip covers every requested indicator.
    assert len(fake.calls) == 1


# --------------------------- /indicators: window trimming ---------------------------


def test_indicators_trims_points_to_the_visible_window():
    fake = _FakeCandleProvider(series=_rising_series())
    # Explicit window starting mid-series (the fake spans 2026-06-01 .. ~2026-07-30).
    start = "2026-07-01T00:00:00Z"
    resp = _client(fake).get(
        f"/stocks/ticker/AAPL/indicators?indicator=rsi&start={start}&end=2026-12-31T00:00:00Z"
    )
    assert resp.status_code == 200, resp.text
    points = resp.json()["indicators"][0]["lines"][0]["points"]
    assert points  # some survive the trim
    cutoff = datetime(2026, 7, 1, tzinfo=timezone.utc)
    assert all(datetime.fromisoformat(p["timestamp"]) >= cutoff for p in points)


# --------------------------- /indicators: validation / errors ---------------------------


def test_indicators_unknown_name_is_a_400():
    fake = _FakeCandleProvider(series=_rising_series())
    resp = _client(fake).get("/stocks/ticker/AAPL/indicators?indicator=rsi,bogus")
    assert resp.status_code == 400
    assert "bogus" in resp.json()["detail"]


def test_indicators_period_on_no_period_indicator_is_a_400():
    fake = _FakeCandleProvider(series=_rising_series())
    resp = _client(fake).get("/stocks/ticker/AAPL/indicators?indicator=macd:5")
    assert resp.status_code == 400


def test_indicators_out_of_range_period_is_a_400():
    fake = _FakeCandleProvider(series=_rising_series())
    resp = _client(fake).get("/stocks/ticker/AAPL/indicators?indicator=rsi:1")
    assert resp.status_code == 400


def test_indicators_empty_request_is_a_400():
    fake = _FakeCandleProvider(series=_rising_series())
    assert _client(fake).get("/stocks/ticker/AAPL/indicators?indicator=").status_code == 400


def test_indicators_too_many_is_a_400():
    fake = _FakeCandleProvider(series=_rising_series())
    thirteen = "rsi,macd,bbands,atr,stoch,adx,obv,vwap,willr,cci,roc,mfi,sma"
    resp = _client(fake).get(f"/stocks/ticker/AAPL/indicators?indicator={thirteen}")
    assert resp.status_code == 400


def test_indicators_bad_symbol_is_a_400():
    fake = _FakeCandleProvider(series=_rising_series())
    assert _client(fake).get("/stocks/ticker/123/indicators?indicator=rsi").status_code == 400


def test_indicators_unknown_symbol_is_a_404():
    fake = _FakeCandleProvider(error=StockNotFound("ZZZZ"))
    resp = _client(fake).get("/stocks/ticker/ZZZZ/indicators?indicator=rsi&range=MAX")
    assert resp.status_code == 404


def test_indicators_upstream_failure_is_a_502():
    fake = _FakeCandleProvider(error=StockDataUnavailable("AAPL", "boom"))
    resp = _client(fake).get("/stocks/ticker/AAPL/indicators?indicator=rsi&range=MAX")
    assert resp.status_code == 502


# --------------------------- refactor smoke: the pre-existing endpoints ---------------------------


def test_candles_endpoint_still_serves():
    fake = _FakeCandleProvider(series=_rising_series())
    resp = _client(fake).get("/stocks/ticker/AAPL/candles?range=MAX")
    assert resp.status_code == 200, resp.text
    assert resp.json()["count"] == 60


def test_ema_endpoint_still_serves():
    fake = _FakeCandleProvider(series=_rising_series())
    resp = _client(fake).get("/stocks/ticker/AAPL/ema?range=MAX&period=9&period=21")
    assert resp.status_code == 200, resp.text
    assert [line["period"] for line in resp.json()["lines"]] == [9, 21]


def test_support_levels_endpoint_still_serves():
    fake = _FakeCandleProvider(series=_rising_series())
    resp = _client(fake).get("/stocks/ticker/AAPL/support-levels?range=MAX")
    assert resp.status_code == 200, resp.text


def test_trend_endpoint_still_serves():
    # Enough history to warm the 200-bar long horizon (the new default trio is
    # 20/50/200); a steadily rising series reads as all three horizons aligned.
    fake = _FakeCandleProvider(series=_rising_series(250))
    resp = _client(fake).get("/stocks/ticker/AAPL/trend?range=MAX")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["reading"] == "strong_uptrend"
    assert body["short_term"] is not None
    assert body["medium_term"] is not None
    assert body["long_term"] is not None


def test_trend_endpoint_unknown_when_long_horizon_lacks_history():
    # 60 bars can't warm the 200-bar long horizon -> reading falls back to unknown.
    fake = _FakeCandleProvider(series=_rising_series(60))
    resp = _client(fake).get("/stocks/ticker/AAPL/trend?range=MAX")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["reading"] == "unknown"
    assert body["long_term"] is None
    assert body["short_term"] is not None


def test_trend_endpoint_rejects_non_increasing_periods():
    fake = _FakeCandleProvider(series=_rising_series(250))
    resp = _client(fake).get(
        "/stocks/ticker/AAPL/trend?range=MAX"
        "&short_period=50&medium_period=50&long_period=200"
    )
    assert resp.status_code == 400, resp.text


def test_bad_symbol_maps_to_400_on_a_pre_existing_endpoint():
    fake = _FakeCandleProvider(series=_rising_series())
    assert _client(fake).get("/stocks/ticker/123/candles?range=MAX").status_code == 400
