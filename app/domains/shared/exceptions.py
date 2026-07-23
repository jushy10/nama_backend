class StockNotFound(Exception):
    def __init__(self, symbol: str) -> None:
        self.symbol = symbol
        super().__init__(f"No stock data found for symbol '{symbol}'.")


class StockDataUnavailable(Exception):
    def __init__(self, symbol: str, reason: str) -> None:
        self.symbol = symbol
        self.reason = reason
        super().__init__(f"Stock data for '{symbol}' is unavailable: {reason}")


class QuotaExceeded(Exception):
    def __init__(self) -> None:
        super().__init__(
            "Daily AI generation limit reached for this client. Try again tomorrow."
        )
