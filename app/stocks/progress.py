"""A tiny progress-reporting seam for the long-running sync sweeps.

The sync use cases walk hundreds-to-thousands of stocks; a caller (the batch CLI running as a
one-off ECS task) wants to watch how far along a sweep is in CloudWatch without the use case
knowing anything about logging or threads. So the use case reports progress through this
abstraction — ``start`` once the work size is known, then ``advance`` per item — and the
composition root injects a concrete reporter (a heartbeat logger in production, the no-op
default in tests). Same inversion as the ports: the inner layer depends on the interface, the
outer layer implements it.

Framework-free on purpose (stdlib typing only), so a use case can import it like an entity or a
port — no framework, no vendor, no threads reach the application core.
"""

from __future__ import annotations

from typing import Protocol


class ProgressReporter(Protocol):
    """The sink a sweep pushes its progress into; the implementation decides how to surface it.

    A sweep calls ``start(total)`` once, as soon as it knows how many items it will process, then
    ``advance(ok=...)`` exactly once per item. That's enough for a reporter to render "N/total
    (P%)" with a success/failure split — see ``app.stocks.endpoints.sync_progress`` for the
    heartbeat-logging implementation.
    """

    def start(self, total: int) -> None:
        """Announce the total number of items about to be processed (the denominator)."""

    def advance(self, *, ok: bool = True) -> None:
        """Mark one item processed; ``ok=False`` records it as a counted failure rather than a
        success (a vendor miss, an empty result), so the reporter can show both."""


class NullProgress:
    """The default reporter: does nothing.

    Keeps the use cases' signatures clean and the offline tests silent — a sweep with no injected
    reporter simply reports into the void, so nothing about progress logging leaks into the tests
    or the read paths that reuse the same use cases.
    """

    def start(self, total: int) -> None:
        pass

    def advance(self, *, ok: bool = True) -> None:
        pass
