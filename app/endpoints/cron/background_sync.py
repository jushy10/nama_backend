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
    status: str  # "accepted" | "already_running"
    limit: int | None


def _run_guarded(
    lock: threading.Lock, run: SyncRunner, limit: int | None, label: str
) -> None:
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
