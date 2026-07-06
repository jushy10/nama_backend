"""Progress logging for the long-running cron sweeps.

The ``/internal/**/sync`` sweeps (and their ``python -m app.sync`` twins) walk hundreds to
thousands of stocks sequentially — minutes of work between the endpoint's "started" and the
runner's "… sync done" line, during which a CloudWatch tail shows nothing. Wrapping a sweep's
loop in :func:`iter_with_progress` fills that gap: it logs an INFO line as the sweep crosses
each ~``step_percent`` mark, so an operator watching the log group sees the run advancing and
can tell a slow run from a wedged one.

This is pure stdlib ``logging`` — a cross-cutting concern a use case may lean on directly (the
same way ``indicators`` is a pure helper the use cases import), with no framework, vendor, or
adapter in sight, so the sweeps stay unit-testable offline. The caller passes its own module
logger, so each line is tagged with the slice it came from (e.g.
``app.stocks.earnings.quarterly.use_cases``) and shares a ``label`` prefix with that slice's
final "… sync done" line, so one CloudWatch filter catches a run's progress and its summary.

The work-list is sized up front from ``len(items)`` — every sweep hands in a materialized
list/tuple (its ``refresh_targets`` / ``tickers_missing_*`` result), never a lazy generator, so
the percentage denominator is known before the first item.
"""

from __future__ import annotations

import logging
from collections.abc import Collection, Iterator
from typing import TypeVar

T = TypeVar("T")


def iter_with_progress(
    items: Collection[T],
    *,
    logger: logging.Logger,
    label: str,
    step_percent: int = 10,
) -> Iterator[T]:
    """Yield each of ``items`` unchanged, logging progress at ~``step_percent`` intervals.

    Emits at INFO:

    - one ``"<label>: starting (N to process)"`` line before the first item, so the total
      (the percentage's denominator) is on the record up front;
    - a ``"<label>: P% (i/N)"`` line the first time the completed count crosses each
      ``step_percent`` boundary, and always one at 100% for the final item;
    - a single ``"<label>: nothing to process"`` line, and no iterations, when ``items`` is
      empty.

    The percentage reflects items **completed** — the line for item ``i`` is logged after the
    consumer finishes processing it (control returns to this generator only when the ``for``
    body asks for the next item), so "100%" means the sweep is done, not merely started on its
    last item. ``step_percent`` is floored at 1. This adds nothing to the items and raises
    nothing of its own, so it drops around any ``for`` loop without touching the loop body.
    """
    step = max(1, step_percent)
    total = len(items)
    if total == 0:
        logger.info("%s: nothing to process", label)
        return
    logger.info("%s: starting (%d to process)", label, total)
    next_mark = step
    for index, item in enumerate(items, start=1):
        yield item
        percent = index * 100 // total
        if percent >= next_mark or index == total:
            logger.info("%s: %d%% (%d/%d)", label, percent, index, total)
            # Jump the next threshold just past the percentage we logged, so a big list still
            # logs ~once per step and a tiny list (each item a >step jump) logs each item once.
            next_mark = (percent // step + 1) * step
