from __future__ import annotations

import logging
import sys
from collections.abc import Callable, Sequence

from app.endpoints.cron.annual_earnings_endpoints import run_annual_earnings_sync
from app.endpoints.cron.etf_endpoints import run_etf_sync
from app.endpoints.cron.fundamentals_endpoints import run_fundamentals_sync
from app.endpoints.cron.index_membership_endpoints import (
    run_index_membership_sync,
)
from app.endpoints.cron.congress_endpoints import run_congress_sync
from app.endpoints.cron.market_brief_endpoints import run_market_brief_sync
from app.endpoints.cron.insider_transactions_endpoints import (
    run_insider_transactions_sync,
)
from app.endpoints.cron.institutional_ownership_endpoints import (
    run_institutional_ownership_sync,
)
from app.endpoints.cron.news_endpoints import run_news_sync
from app.endpoints.cron.performance_endpoints import (
    run_stock_performance_sync,
)
from app.endpoints.cron.quarterly_earnings_endpoints import (
    run_quarterly_earnings_sync,
)
from app.endpoints.cron.recommendations_endpoints import run_recommendations_sync
from app.endpoints.cron.revenue_segments_endpoints import (
    run_revenue_segments_sync,
)
from app.endpoints.cron.universe_endpoints import run_universe_sync

logger = logging.getLogger("app.sync")

# slice name -> the sweep's unit of work. Each takes an optional cap: None means "process every
# stock" for the earnings/recs sweeps, "enrich the slice's own default cap" for universe, and
# "categorise every still-uncategorised fund" for etfs (both universe and etfs screen in full
# regardless; the cap bounds only the per-ticker sector/category enrichment). index-membership
# ignores the cap entirely — it's a full mark/clear reconcile against both index lists, not a
# stalest-N sweep.
RUNNERS: dict[str, Callable[[int | None], object]] = {
    "quarterly-earnings": run_quarterly_earnings_sync,
    "annual-earnings": run_annual_earnings_sync,
    "recommendations": run_recommendations_sync,
    "news": run_news_sync,
    "institutional-ownership": run_institutional_ownership_sync,
    "fundamentals": run_fundamentals_sync,
    "revenue-segments": run_revenue_segments_sync,
    "insider-transactions": run_insider_transactions_sync,
    "congress": run_congress_sync,
    "performance": run_stock_performance_sync,
    "market-brief": run_market_brief_sync,
    "universe": run_universe_sync,
    "index-membership": run_index_membership_sync,
    "etfs": run_etf_sync,
}


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)

    if not args or args[0] not in RUNNERS:
        sys.stderr.write(f"usage: python -m app.sync <{'|'.join(RUNNERS)}> [limit]\n")
        return 2

    slice_name = args[0]
    try:
        limit: int | None = int(args[1]) if len(args) > 1 else None
    except ValueError:
        sys.stderr.write(f"limit must be an integer, got {args[1]!r}\n")
        return 2

    # Configure logging so the one-off task's output reaches CloudWatch: a bare `python -m`
    # process has no handlers (uvicorn installs them for the web app), so without this the
    # runners' "… sync done: refreshed=… failed=…" INFO lines would be swallowed.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    logger.info("starting %s sync (limit=%s)", slice_name, limit)
    RUNNERS[slice_name](limit)  # a failure raises -> traceback + non-zero exit
    logger.info("%s sync finished", slice_name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
