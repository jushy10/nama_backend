"""Shared machinery for the fire-and-forget cron sync endpoints.

Each ``/internal/**/sync`` endpoint kicks off a minutes-long sweep that can't run
synchronously behind API Gateway's hard 30s integration timeout, so it schedules the sweep
on a background thread and returns ``202`` at once; the sweep then runs to completion inside
the always-on container. This module owns the bit that's easy to get wrong — the
single-flight guard, the exception-safe thread spawn, and never letting an exception die
silently inside the thread — so the three cron endpoints share one tested implementation
instead of three copies.

Each endpoint keeps its **own** lock and its **own** runner:

- The lock is per-slice on purpose. The annual / quarterly / recommendations sweeps are
  independent and may run at the same time; a lock only stops a sweep overlapping *itself*
  (an overlapping cron + manual trigger both hitting Yahoo, which blocks data-centre IPs
  under load).
- The runner is the slice's unit of work: it opens a **fresh** DB session (the request-
  scoped ``get_db`` one is closed by the time the thread runs), builds the slice's
  provider/repo/use case, and executes the sweep. It's a DI seam so tests substitute a fake
  and drive the endpoint offline.

The guard is per-process, which is correct for the single always-on task today. If the
service ever scales past one task, swap the in-process lock for a Postgres advisory lock
taken inside the runner so the single-flight guarantee holds across containers.
"""

import logging
import threading
from collections.abc import Callable

from fastapi import Response, status
from pydantic import BaseModel

logger = logging.getLogger(__name__)

# A sync runner performs one full sweep of up to ``limit`` stocks most in need of a refresh
# (``None`` = every stock). Its return value is ignored (the refreshed/failed counts go to the
# logs, not the HTTP response) — it exists only for the runner's own logging and for tests to
# assert against.
SyncRunner = Callable[[int | None], object]


class SyncTriggerResponse(BaseModel):
    """The trigger's outcome: whether a sweep was started or one was already running, and
    the per-run cap the background sweep applies (``null`` when the run is uncapped)."""

    status: str  # "accepted" | "already_running"
    limit: int | None


def _run_guarded(
    lock: threading.Lock, run: SyncRunner, limit: int | None, label: str
) -> None:
    """Background body: run the sweep, always release the guard, and never let an exception
    escape the thread (an unhandled one would otherwise die silently and, worse, strand the
    guard)."""
    try:
        run(limit)
    except Exception:
        logger.exception("%s failed", label)
    finally:
        lock.release()


def trigger_sync(
    lock: threading.Lock,
    run: SyncRunner,
    limit: int | None,
    response: Response,
    *,
    label: str,
) -> SyncTriggerResponse:
    """Start ``run(limit)`` on a daemon thread unless a sweep is already in flight.

    Single-flight: if ``lock`` is already held, nothing starts and the response is a ``200``
    ``already_running``. Otherwise the sweep starts and the response is ``202`` ``accepted``
    (the ``202`` is the route's default status; only the no-op path overrides it to ``200``).
    The lock is held from here until the thread finishes releasing it in ``_run_guarded``.
    """
    if not lock.acquire(blocking=False):
        response.status_code = status.HTTP_200_OK
        return SyncTriggerResponse(status="already_running", limit=limit)
    try:
        # daemon=True so a container shutdown doesn't block on an in-flight sweep; the
        # stalest-first, commit-per-stock sweeps are safe to interrupt and resume.
        threading.Thread(
            target=_run_guarded, args=(lock, run, limit, label), daemon=True
        ).start()
    except BaseException:
        lock.release()  # thread never started — don't strand the guard
        raise
    return SyncTriggerResponse(status="accepted", limit=limit)
