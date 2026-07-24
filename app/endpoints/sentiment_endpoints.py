from fastapi import APIRouter, Depends, Response

from app.domains.macro.sentiment import wiring
from app.domains.macro.sentiment.api_schemas import MarketSentimentResponse
from app.domains.macro.sentiment.use_cases import GetMarketSentiment

router = APIRouter(tags=["market"])


def get_get_market_sentiment() -> GetMarketSentiment:
    # Depends shim over the slice's wiring — exists for the dependency_overrides
    # test seam, nothing more (both sources are keyless, so no 503 gate).
    return wiring.build_get_market_sentiment()


@router.get("/market/sentiment", response_model=MarketSentimentResponse)
def get_market_sentiment_endpoint(
    response: Response,
    use_case: GetMarketSentiment = Depends(get_get_market_sentiment),
) -> MarketSentimentResponse:
    # Each leg is best-effort inside the use case; only both legs failing raises
    # StockDataUnavailable, translated to 502 by the central handlers in
    # endpoints/error_handlers.py.
    sentiment = use_case.run()
    # Backs a homepage widget hit by every visitor; both inputs move slowly (the
    # VIX is an end-of-day close), so cache generously — a burst of viewers
    # collapses onto one upstream fetch rather than hammering FRED/CNN.
    response.headers["Cache-Control"] = "public, max-age=900"
    return MarketSentimentResponse.from_sentiment(sentiment)
