import logging

import pytest

from app.stocks.progress import iter_with_progress


@pytest.fixture
def progress_log():
    logger = logging.getLogger("tests.progress")
    logger.disabled = False
    logger.propagate = False
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    messages: list[str] = []
    handler = logging.Handler()
    handler.emit = lambda record: messages.append(record.getMessage())
    logger.addHandler(handler)
    yield logger, messages
    logger.handlers.clear()


def test_yields_every_item_unchanged_and_in_order(progress_log):
    logger, _ = progress_log
    items = ["AAPL", "MSFT", "NVDA"]
    # Transparent passthrough — the wrapped loop body sees exactly the same items, in order.
    assert list(iter_with_progress(items, logger=logger, label="sync")) == items


def test_empty_logs_nothing_to_process_and_yields_nothing(progress_log):
    logger, messages = progress_log
    assert list(iter_with_progress([], logger=logger, label="sync")) == []
    assert messages == ["sync: nothing to process"]


def test_logs_start_with_total_then_one_line_per_step_and_a_final_100(progress_log):
    logger, messages = progress_log
    # 10 items at 50% steps → a starting line, then 50% and 100% (each step once, 100% always).
    list(iter_with_progress(range(10), logger=logger, label="sync", step_percent=50))
    assert messages == [
        "sync: starting (10 to process)",
        "sync: 50% (5/10)",
        "sync: 100% (10/10)",
    ]


def test_percent_counts_completed_items_not_started_ones(progress_log):
    logger, messages = progress_log
    # The line for item i is logged only after the consumer takes the *next* item, so the
    # percentage is "done", not "started": pulling 5 of 10 has not yet logged 50%.
    gen = iter_with_progress(range(10), logger=logger, label="sync", step_percent=50)
    for _ in range(5):
        next(gen)
    # Only the starting line so far — the 50% line fires when the 6th item is requested.
    assert messages == ["sync: starting (10 to process)"]


def test_tiny_list_logs_each_item_once_and_ends_at_100(progress_log):
    logger, messages = progress_log
    list(iter_with_progress(["a", "b", "c"], logger=logger, label="sync"))
    assert messages == [
        "sync: starting (3 to process)",
        "sync: 33% (1/3)",
        "sync: 66% (2/3)",
        "sync: 100% (3/3)",
    ]


def test_step_percent_floored_at_one_so_zero_does_not_divide_by_zero(progress_log):
    logger, messages = progress_log
    list(iter_with_progress(["a", "b"], logger=logger, label="sync", step_percent=0))
    # No crash, and the final 100% line is still emitted.
    assert messages[-1] == "sync: 100% (2/2)"
