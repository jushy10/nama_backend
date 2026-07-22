from __future__ import annotations

import os
import threading
import time
from typing import Callable, Optional, TypeVar

T = TypeVar("T")

# Backoff between a failed call and its retry (ms). The retry re-fetches a fresh crumb (a
# network round-trip) regardless, so 0 is not a busy-loop; a small value is gentler still.
_BACKOFF_SECONDS = float(os.getenv("YF_RETRY_BACKOFF_MS", "0")) / 1000.0

# Minimum spacing between *any* two yfinance calls in this process (ms) — the pacing knob.
# Default 0 (off) so local/tests add no latency; set it on the deployed app (e.g. 250) to
# space out the sync loops' sequential per-ticker calls and stay gentler on Yahoo's limits.
_MIN_INTERVAL_SECONDS = float(os.getenv("YF_MIN_REQUEST_INTERVAL_MS", "0")) / 1000.0

_pace_lock = threading.Lock()
_last_call_at = 0.0


def _pace() -> None:
    if _MIN_INTERVAL_SECONDS <= 0:
        return
    global _last_call_at
    with _pace_lock:
        wait = _MIN_INTERVAL_SECONDS - (time.monotonic() - _last_call_at)
        if wait > 0:
            time.sleep(wait)
        _last_call_at = time.monotonic()


def reset_crumb() -> None:
    try:
        from yfinance import data as yf_data

        singleton = yf_data.YfData()  # SingletonMeta → the shared instance every Ticker uses
        lock = getattr(singleton, "_cookie_lock", None)
        if lock is not None:
            with lock:
                singleton._crumb = None
                singleton._cookie = None
        else:
            singleton._crumb = None
            singleton._cookie = None
    except Exception:  # noqa: BLE001 — a reset failure must never mask the real error
        pass


def frame_is_empty(frame) -> bool:
    return frame is None or bool(getattr(frame, "empty", True))


def _is_crumb_401(exc: Exception) -> bool:
    response = getattr(exc, "response", None)
    if getattr(response, "status_code", None) == 401:
        return True
    message = str(exc).lower()
    if "unable to access this feature" in message:
        return False
    return "invalid crumb" in message or "unauthorized" in message or " 401" in message


def call(
    fn: Callable[[], T],
    *,
    is_empty: Optional[Callable[[T], bool]] = None,
    retries: int = 1,
) -> T:
    attempt = 0
    while True:
        _pace()
        try:
            result = fn()
        except Exception as exc:  # noqa: BLE001 — vendor boundary: classify, then retry or re-raise
            if attempt >= retries or not _is_crumb_401(exc):
                raise
            attempt += 1
            reset_crumb()
            if _BACKOFF_SECONDS > 0:
                time.sleep(_BACKOFF_SECONDS)
            continue
        if is_empty is not None and attempt < retries and is_empty(result):
            attempt += 1
            reset_crumb()
            if _BACKOFF_SECONDS > 0:
                time.sleep(_BACKOFF_SECONDS)
            continue
        return result
