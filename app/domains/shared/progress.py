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
