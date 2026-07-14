"""HTTP API for the combined market-sentiment read.

``GET /market/sentiment`` — the home page's at-a-glance "market mood": the VIX
(from FRED) and the CNN Fear & Greed score (from CNN), in one payload. Controller
+ presenter + wiring, the composition-root way, sitting in ``app/stocks/endpoints/``
beside the other market reads. Both sources are keyless and live-per-request, so
there's no table, no cron, and — like the ``/sectors`` and yield-curve reads — the
wiring factories are local, keyless, and un-gated.

Each leg is best-effort: the use case drops a failed source to ``null`` and only
raises when *both* are unavailable, so a CNN block can't take the VIX down with
it. The response is cached generously (15 min) — this backs a widget every home
visitor hits, and both inputs move slowly (the VIX is an end-of-day close), so a
burst of viewers collapses onto one upstream fetch.
"""

from functools import lru_cache

from fastapi import APIRouter, Depends, HTTPException, Response

from app.stocks.adapters.cnn_fear_greed_adapter import CnnFearGreedProvider
from app.stocks.adapters.fred_vix_adapter import FredVixProvider
from app.stocks.exceptions import StockDataUnavailable, StockNotFound
from app.stocks.sentiment.entities import (
    FearGreedSnapshot,
    MarketSentiment,
    VixSnapshot,
)
from app.stocks.sentiment.ports import FearGreedProvider, VixProvider
from app.stocks.sentiment.schemas import (
    FearGreedResponse,
    MarketSentimentResponse,
    VixResponse,
)
from app.stocks.sentiment.use_cases import GetMarketSentiment

router = APIRouter(tags=["market"])


@lru_cache(maxsize=1)
def get_vix_provider() -> VixProvider:
    # Keyless (FRED), so no 503 gate — unlike the Alpaca price feed.
    return FredVixProvider()


@lru_cache(maxsize=1)
def get_fear_greed_provider() -> FearGreedProvider:
    # Keyless (CNN), so no 503 gate.
    return CnnFearGreedProvider()


def get_market_sentiment(
    vix_provider: VixProvider = Depends(get_vix_provider),
    fear_greed_provider: FearGreedProvider = Depends(get_fear_greed_provider),
) -> GetMarketSentiment:
    return GetMarketSentiment(vix_provider, fear_greed_provider)


def _present_vix(vix: VixSnapshot) -> VixResponse:
    return VixResponse(
        as_of=vix.as_of,
        value=vix.value,
        previous_close=vix.previous_close,
        change=vix.change,
        change_percent=vix.change_percent,
        regime=vix.regime,
    )


def _present_fear_greed(fear_greed: FearGreedSnapshot) -> FearGreedResponse:
    return FearGreedResponse(
        score=fear_greed.score,
        as_of=fear_greed.as_of,
        rating=fear_greed.rating,
        band=fear_greed.band.value,
        label=fear_greed.label,
        previous_close=fear_greed.previous_close,
        previous_1_week=fear_greed.previous_1_week,
        previous_1_month=fear_greed.previous_1_month,
        previous_1_year=fear_greed.previous_1_year,
    )


def _present_sentiment(sentiment: MarketSentiment) -> MarketSentimentResponse:
    """Presenter: the combined entity -> HTTP response DTO (either leg may be null)."""
    return MarketSentimentResponse(
        vix=_present_vix(sentiment.vix) if sentiment.vix is not None else None,
        fear_greed=(
            _present_fear_greed(sentiment.fear_greed)
            if sentiment.fear_greed is not None
            else None
        ),
    )


@router.get("/market/sentiment", response_model=MarketSentimentResponse)
def get_market_sentiment_endpoint(
    response: Response,
    use_case: GetMarketSentiment = Depends(get_market_sentiment),
) -> MarketSentimentResponse:
    try:
        sentiment = use_case.execute()
    except StockNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except StockDataUnavailable as exc:
        raise HTTPException(502, str(exc)) from exc
    # Backs a homepage widget hit by every visitor; both inputs move slowly (the
    # VIX is an end-of-day close), so cache generously — a burst of viewers
    # collapses onto one upstream fetch rather than hammering FRED/CNN.
    response.headers["Cache-Control"] = "public, max-age=900"
    return _present_sentiment(sentiment)
