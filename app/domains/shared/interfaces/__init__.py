from app.domains.shared.interfaces.all_time_high_adapter import AllTimeHighAdapter
from app.domains.shared.interfaces.analyst_estimates_adapter import AnalystEstimatesAdapter
from app.domains.shared.interfaces.bulk_performance_adapter import BulkPerformanceAdapter
from app.domains.shared.interfaces.bulk_quote_adapter import BulkQuoteAdapter
from app.domains.shared.interfaces.generation_quota_adapter import (
    GenerationQuotaAdapter,
    consume_generation_quota,
)
from app.domains.shared.interfaces.stock_data_adapter import StockDataAdapter
from app.domains.shared.interfaces.stock_performance_adapter import StockPerformanceAdapter
from app.domains.shared.interfaces.stock_quote_adapter import StockQuoteAdapter

__all__ = ["AllTimeHighAdapter", "AnalystEstimatesAdapter", "BulkPerformanceAdapter", "BulkQuoteAdapter", "GenerationQuotaAdapter", "StockDataAdapter", "StockPerformanceAdapter", "StockQuoteAdapter", "consume_generation_quota"]
