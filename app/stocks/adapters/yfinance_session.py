"""Shared yfinance resilience: pace calls, and retry once past a transient crumb 401.

Every yfinance JSON call (``.info``, ``income_stmt``, ``recommendations``, option chains,
``yf.screen``) hits ``query1/2.finance.yahoo.com``, which gates each request behind a cached
**cookie + crumb** pair. From a data-centre IP that handshake intermittently comes back
**HTTP 401 "Invalid Crumb"**, and two things make it sticky:

- yfinance caches the crumb on a **process-global singleton** (``YfData``) and, on a 401,
  does *not* invalidate it — with the default ``hide_exceptions`` it merely logs the 401 and
  hands back empty data. So every later call reuses the same poisoned crumb and keeps
  failing, and the empty result is indistinguishable from "Yahoo genuinely has no data".

This helper closes that gap. It runs a yfinance access and, on a failure that looks like a
crumb rejection — a **raised** 401, or (when ``is_empty`` is supplied) an **empty result**
that signals a *swallowed* 401 — it drops the singleton's cached cookie/crumb so the next
attempt re-establishes them, waits an optional backoff, and retries once.

It deliberately does **not** paper over Yahoo's harder gate ("User is unable to access this
feature"), an IP-reputation block a fresh crumb can't clear: that isn't classified as a
crumb 401, so it exhausts the (single) retry and surfaces to the caller unchanged.

Adapter-layer infrastructure — importing yfinance here is fine (only adapters know the
vendor). The retry backoff and the inter-call pacing are env-tunable and default to *off*,
so local runs and the offline tests add no latency; the deployed app can dial them in.
"""

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
    """Block until at least ``_MIN_INTERVAL_SECONDS`` has elapsed since the previous call, so
    concurrent sync loops don't burst Yahoo from one IP. A no-op when pacing is off."""
    if _MIN_INTERVAL_SECONDS <= 0:
        return
    global _last_call_at
    with _pace_lock:
        wait = _MIN_INTERVAL_SECONDS - (time.monotonic() - _last_call_at)
        if wait > 0:
            time.sleep(wait)
        _last_call_at = time.monotonic()


def reset_crumb() -> None:
    """Drop yfinance's process-global cached cookie + crumb so the next call re-acquires them.

    yfinance reuses a cached crumb and never invalidates it on a 401, so without this a
    poisoned crumb sticks for the whole process. Best-effort and version-guarded: reaching
    into the ``YfData`` singleton's private state is the only way to force a refresh, and an
    upstream layout change must degrade to a no-op, never a crash.
    """
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


def _is_crumb_401(exc: Exception) -> bool:
    """Whether ``exc`` looks like a recoverable Yahoo crumb/401 (worth a fresh-crumb retry).

    Matches a 401 status or the crumb/unauthorized wording. Deliberately excludes a 429
    rate-limit (retrying immediately would only add load) and Yahoo's hard "unable to access
    this feature" IP gate (a fresh crumb can't clear it), so neither is retried.
    """
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
    """Run a yfinance access ``fn`` with pacing and a single crumb-refresh retry.

    ``fn`` is the raw access, e.g. ``lambda: yf.Ticker(sym).info``. On a crumb 401 — either
    raised, or (with ``is_empty`` supplied) surfaced as an empty result, the swallowed case —
    the cached crumb is dropped and ``fn`` is retried once. Any non-crumb exception propagates
    at once; a genuinely non-empty result (or an empty one with no ``is_empty`` predicate) is
    returned as-is.
    """
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
