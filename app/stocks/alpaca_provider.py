"""Interface Adapter: the Alpaca-backed StockDataProvider.

This is the only module that knows Alpaca exists. It translates Alpaca's
SDK models into our Stock entity and Alpaca's failures into domain errors.
Swap data vendors and only this file changes.

SDK: https://alpaca.markets/sdks/python/
"""

from datetime import datetime

from alpaca.common.enums import Sort
from alpaca.common.exceptions import APIError
from alpaca.data.enums import Adjustment, DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, StockSnapshotRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.trading.client import TradingClient

from app.stocks.entities import Candle, CandleSeries, Stock, Timeframe
from app.stocks.exceptions import StockDataUnavailable, StockNotFound
from app.stocks.ports import CandleProvider, StockDataProvider

# Our vendor-agnostic Timeframe -> Alpaca's (amount, unit).
_TIMEFRAME_MAP: dict[Timeframe, tuple[int, TimeFrameUnit]] = {
    Timeframe.MIN_1: (1, TimeFrameUnit.Minute),
    Timeframe.MIN_5: (5, TimeFrameUnit.Minute),
    Timeframe.MIN_15: (15, TimeFrameUnit.Minute),
    Timeframe.MIN_30: (30, TimeFrameUnit.Minute),
    Timeframe.HOUR_1: (1, TimeFrameUnit.Hour),
    Timeframe.HOUR_4: (4, TimeFrameUnit.Hour),
    Timeframe.DAY_1: (1, TimeFrameUnit.Day),
    Timeframe.WEEK_1: (1, TimeFrameUnit.Week),
    Timeframe.MONTH_1: (1, TimeFrameUnit.Month),
}

# Hard cap on candles per response. Also Alpaca's max page size. We ask Alpaca
# to sort newest-first and cap at this many, so when a window holds more bars
# than the cap it's the *most recent* ones that survive (a chart wants recent
# data); we then reverse back to chronological order before returning.
_MAX_CANDLES = 10_000


class AlpacaStockDataProvider(StockDataProvider, CandleProvider):
    """Fetches stock data from Alpaca and maps it onto the Stock entity."""

    def __init__(
        self,
        api_key: str,
        secret_key: str,
        *,
        feed: DataFeed = DataFeed.IEX,  # free plan -> IEX
        paper: bool = True,
    ) -> None:
        self._data = StockHistoricalDataClient(api_key, secret_key)
        self._trading = TradingClient(api_key, secret_key, paper=paper)
        self._feed = feed

    def get_stock(self, symbol: str) -> Stock:
        snapshot = self._fetch_snapshot(symbol)
        name, exchange = self._fetch_asset_metadata(symbol)
        return self._to_entity(symbol, snapshot, name, exchange)

    def get_candles(
        self,
        symbol: str,
        timeframe: Timeframe,
        *,
        start: datetime | None,
        end: datetime | None,
    ) -> CandleSeries:
        amount, unit = _TIMEFRAME_MAP[timeframe]
        try:
            request = StockBarsRequest(
                symbol_or_symbols=symbol,
                timeframe=TimeFrame(amount, unit),
                start=start,
                end=end,
                limit=_MAX_CANDLES,
                adjustment=Adjustment.SPLIT,  # keep the line continuous over splits
                sort=Sort.DESC,  # newest-first so the cap keeps recent bars
                feed=self._feed,
            )
            barset = self._data.get_stock_bars(request)
        except APIError as exc:
            raise StockDataUnavailable(symbol, str(exc)) from exc

        bars = barset.data.get(symbol, [])
        if not bars:
            raise StockNotFound(symbol)
        # Reverse the newest-first response into chronological (oldest-first)
        # order — the order a chart draws candles left to right.
        candles = tuple(self._to_candle(bar) for bar in reversed(bars))
        return CandleSeries(symbol=symbol, timeframe=timeframe, candles=candles)

    # --- Alpaca calls (thin and isolated) ---

    def _fetch_snapshot(self, symbol: str):
        try:
            request = StockSnapshotRequest(symbol_or_symbols=symbol, feed=self._feed)
            snapshots = self._data.get_stock_snapshot(request)
        except APIError as exc:
            raise StockDataUnavailable(symbol, str(exc)) from exc

        snapshot = snapshots.get(symbol)
        if snapshot is None or snapshot.latest_trade is None:
            raise StockNotFound(symbol)
        return snapshot

    def _fetch_asset_metadata(self, symbol: str) -> tuple[str | None, str | None]:
        """Company name + listing exchange. Best-effort; never fatal."""
        try:
            asset = self._trading.get_asset(symbol)
        except APIError:
            return None, None
        exchange = asset.exchange.value if asset.exchange else None
        return asset.name, exchange

    # --- Mapping: Alpaca SDK models -> domain entity ---

    @staticmethod
    def _to_candle(bar) -> Candle:
        return Candle(
            timestamp=bar.timestamp,
            open=bar.open,
            high=bar.high,
            low=bar.low,
            close=bar.close,
            volume=int(bar.volume) if bar.volume is not None else None,
        )

    @staticmethod
    def _to_entity(symbol, snapshot, name, exchange) -> Stock:
        trade = snapshot.latest_trade
        quote = snapshot.latest_quote
        daily = snapshot.daily_bar
        prev = snapshot.previous_daily_bar
        return Stock(
            symbol=symbol,
            name=name,
            exchange=exchange,
            price=trade.price,
            open=daily.open if daily else None,
            high=daily.high if daily else None,
            low=daily.low if daily else None,
            previous_close=prev.close if prev else None,
            volume=int(daily.volume) if daily and daily.volume is not None else None,
            bid=quote.bid_price if quote else None,
            ask=quote.ask_price if quote else None,
            as_of=trade.timestamp,
        )
