"""A tiny, dependency-free progress channel for the out-of-band sync sweeps.

The ``/internal/**/sync`` cron sweeps walk hundreds to thousands of stocks sequentially and
run for minutes on a background thread. This is the seam that lets them report where they are
*as they go* without knowing who is listening: a sync use case takes an optional
``ProgressReporter`` and calls it once per stock with a ``SyncProgress`` tick; the caller (the
cron runner) decides what to do with it — today, log a heartbeat (see
``background_sync.logging_progress_reporter``); tomorrow, update an in-memory status object a
``GET .../sync/status`` endpoint can read, with no change to the sweeps.

It is pure application code (stdlib only), so it sits in the shared kernel beside
``exceptions.py`` and every sub-slice use case may depend on it — exactly as they already
depend on the shared exceptions — never on the framework or a vendor.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum


class SyncOutcome(str, Enum):
    """What happened to one stock in a sweep. Three states, so a reporter can tell a real
    failure (surface it) from a deliberate no-op (ignore it):

    - ``OK`` — the stock was refreshed / enriched / written this run.
    - ``FAILED`` — the source could not serve it (outage/block) or returned nothing usable;
      it is left as-is and retried next run. Counts toward the report's ``failed``.
    - ``SKIPPED`` — a deliberate no-op: nothing to write and nothing wrong (e.g. the source
      has no classification for the ticker yet). Counted in neither total.
    """

    OK = "ok"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass(frozen=True)
class SyncProgress:
    """One tick of a sweep: the stock just processed and where it sits in the run.

    ``done`` is 1-based and counts through ``total`` (the size of this run's work-list, known
    up front because the repositories hand back a materialized list). ``detail`` is an optional
    short reason attached to non-OK ticks (``"empty"``, ``"unavailable"``, ...), for the log.
    """

    done: int
    total: int
    symbol: str
    outcome: SyncOutcome
    detail: str | None = None


# A progress sink a sweep calls once per stock. Optional at every call site — a sweep handed
# no reporter simply runs silently, exactly as it did before this channel existed.
ProgressReporter = Callable[[SyncProgress], None]


def report_progress(
    reporter: ProgressReporter | None,
    done: int,
    total: int,
    symbol: str,
    outcome: SyncOutcome,
    detail: str | None = None,
) -> None:
    """Send one tick to ``reporter`` if there is one — the guarded call the sweep loops use, so
    a ``None`` reporter (and the cost of building the tick) stays out of the hot path."""
    if reporter is not None:
        reporter(SyncProgress(done, total, symbol, outcome, detail))
