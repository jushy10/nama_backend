"""Batch CLI for the stock-data sync sweeps — ``python -m app.sync <slice> [limit]``.

Runs one sync sweep to completion in the current process and exits (``0`` = success, non-zero
= failure), so a sweep can be launched as a one-off ECS task instead of behind the HTTP API.
It's the same work the ``/internal/*/sync`` cron endpoints trigger — but run directly, with no
API Gateway 30s clock, so none of the endpoints' background-thread / single-flight machinery
is needed: a one-off task is a single sweep by construction, and its exit code is the success
signal.

Like ``app.main`` (the web entrypoint) this is a composition-root/edge: it wires nothing new,
it just dispatches to the per-slice ``run_*_sync`` runners the cron endpoints already expose
(the universe one added for parity), so both entrypoints share one tested implementation.

    python -m app.sync universe
    python -m app.sync quarterly-earnings 500
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Callable, Sequence

from app.stocks.endpoints.cron_annual_earnings_endpoints import run_annual_earnings_sync
from app.stocks.endpoints.cron_quarterly_earnings_endpoints import (
    run_quarterly_earnings_sync,
)
from app.stocks.endpoints.cron_recommendations_endpoints import run_recommendations_sync
from app.stocks.endpoints.cron_universe_endpoints import run_universe_sync

logger = logging.getLogger("app.sync")

# slice name -> the sweep's unit of work, taking the per-run cap (stalest-first). Universe
# takes no cap — it screens the whole ≥$1B set at once — so it accepts and ignores the arg.
RUNNERS: dict[str, Callable[[int], object]] = {
    "quarterly-earnings": run_quarterly_earnings_sync,
    "annual-earnings": run_annual_earnings_sync,
    "recommendations": run_recommendations_sync,
    "universe": run_universe_sync,
}

# Matches the cron endpoints' Query default; a universe run ignores it.
DEFAULT_LIMIT = 1000


def main(argv: Sequence[str] | None = None) -> int:
    """Parse ``<slice> [limit]``, run that sweep, and return a process exit code."""
    args = list(sys.argv[1:] if argv is None else argv)

    if not args or args[0] not in RUNNERS:
        sys.stderr.write(f"usage: python -m app.sync <{'|'.join(RUNNERS)}> [limit]\n")
        return 2

    slice_name = args[0]
    try:
        limit = int(args[1]) if len(args) > 1 else DEFAULT_LIMIT
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

    logger.info("starting %s sync (limit=%d)", slice_name, limit)
    RUNNERS[slice_name](limit)  # a failure raises -> traceback + non-zero exit
    logger.info("%s sync finished", slice_name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
