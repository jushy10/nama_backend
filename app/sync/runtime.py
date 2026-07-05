"""Wall-clock backstop for the batch sync CLI.

A sync sweep is a synchronous run of hundreds-to-thousands of sequential Yahoo calls. Those
calls carry no hard per-request timeout (yfinance's transport), and the one-off ECS task that
runs the sweep has no task-level timeout — so a single hung socket would keep the sweep, and the
task's per-second billing, alive forever. ``run_with_timeout`` is the backstop: it runs the
sweep on a worker thread and, if it hasn't finished within the wall-clock budget, forces the
process to exit non-zero. A stuck native call can't be cancelled cooperatively, so a hard
``os._exit`` is the only reliable stop; the heartbeat progress logging is what makes the stall
visible in CloudWatch before this reaps it.

Only the CLI (one-off ECS task) needs this — the HTTP cron path runs inside the always-on API
container, where there's no task to strand. So it lives here in the batch edge, not in the
shared endpoint machinery.
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Callable

logger = logging.getLogger("app.sync")

# Default wall-clock budget for one sweep (seconds). Deliberately generous — it's a hang
# backstop, not a tight SLA: a legitimate full sweep of the whole anchor (thousands of stocks,
# each several sequential Yahoo calls) can run long, and killing a healthy run is worse than a
# late one. Tune with SYNC_MAX_RUNTIME_S.
_DEFAULT_MAX_RUNTIME_S = 7200  # 2 hours


def max_runtime_seconds() -> int:
    """The per-sweep wall-clock budget, from ``SYNC_MAX_RUNTIME_S`` (default 7200s / 2h).

    A non-numeric or non-positive value falls back to the default — there's no "disable"; the
    whole point is that a wedged task can't outlive the budget. Raise the env var if a legitimate
    full sweep needs longer.
    """
    raw = os.getenv("SYNC_MAX_RUNTIME_S")
    if raw is None:
        return _DEFAULT_MAX_RUNTIME_S
    try:
        value = int(raw)
    except ValueError:
        return _DEFAULT_MAX_RUNTIME_S
    return value if value > 0 else _DEFAULT_MAX_RUNTIME_S


def run_with_timeout(
    work: Callable[[], object],
    timeout_s: int,
    *,
    on_timeout: Callable[[int], None] | None = None,
) -> None:
    """Run ``work()`` on a worker thread, waiting at most ``timeout_s`` for it to finish.

    Returns normally when ``work`` completes, and re-raises whatever it raised (so a failed sweep
    still surfaces its traceback and a non-zero exit, exactly as before). If it doesn't finish in
    time, ``on_timeout`` is called — by default a hard ``os._exit(124)`` after logging, since a
    hung native call can't be interrupted. ``on_timeout`` is injectable so tests can assert the
    timeout path without killing the test runner.
    """
    outcome: dict[str, object] = {}

    def target() -> None:
        try:
            work()
            outcome["ok"] = True
        except BaseException as exc:  # noqa: BLE001 — carry any error back to the main thread
            outcome["exc"] = exc

    worker = threading.Thread(target=target, name="sync-worker", daemon=True)
    worker.start()
    worker.join(timeout_s)

    if worker.is_alive():
        (on_timeout or _force_exit)(timeout_s)
        return  # _force_exit never returns; an injected on_timeout may, so stop here

    exc = outcome.get("exc")
    if exc is not None:
        raise exc


def _force_exit(timeout_s: int) -> None:
    """Log and hard-exit the process — the default timeout action. ``os._exit`` (not
    ``sys.exit``) because a daemon worker blocked on a hung socket won't unwind a normal exit."""
    logger.error(
        "sync exceeded its %ds wall-clock limit — forcing exit (a Yahoo call likely hung; "
        "raise SYNC_MAX_RUNTIME_S if a full sweep legitimately needs longer)",
        timeout_s,
    )
    os._exit(124)
