"""Tests for the batch sync CLI (``python -m app.sync <slice> [limit]``).

Offline: the per-slice runners in ``RUNNERS`` are replaced with recorders, so this exercises
only the CLI's dispatch — argument parsing, the slice -> runner mapping, the default/explicit
limit, and the exit codes — without opening a DB session or touching Yahoo.
"""

import pytest

from app.sync import __main__ as cli


class _Recorder:
    """Stands in for a ``run_*_sync`` runner; records the limit it was called with."""

    def __init__(self) -> None:
        self.calls: list[int] = []

    def __call__(self, limit) -> None:
        self.calls.append(limit)


def _patch_runners(monkeypatch) -> dict[str, _Recorder]:
    recorders = {name: _Recorder() for name in cli.RUNNERS}
    monkeypatch.setattr(cli, "RUNNERS", recorders)
    return recorders


def test_dispatches_to_the_named_slice_with_the_default_limit(monkeypatch):
    recorders = _patch_runners(monkeypatch)
    assert cli.main(["quarterly-earnings"]) == 0
    assert recorders["quarterly-earnings"].calls == [cli.DEFAULT_LIMIT]
    # Only the named slice ran.
    assert all(r.calls == [] for n, r in recorders.items() if n != "quarterly-earnings")


def test_passes_an_explicit_limit(monkeypatch):
    recorders = _patch_runners(monkeypatch)
    assert cli.main(["annual-earnings", "250"]) == 0
    assert recorders["annual-earnings"].calls == [250]


def test_universe_dispatches_too(monkeypatch):
    recorders = _patch_runners(monkeypatch)
    assert cli.main(["universe"]) == 0
    assert recorders["universe"].calls == [cli.DEFAULT_LIMIT]


def test_unknown_slice_is_a_usage_error_and_runs_nothing(monkeypatch):
    recorders = _patch_runners(monkeypatch)
    assert cli.main(["does-not-exist"]) == 2
    assert all(r.calls == [] for r in recorders.values())


def test_no_slice_is_a_usage_error(monkeypatch):
    _patch_runners(monkeypatch)
    assert cli.main([]) == 2


def test_non_integer_limit_is_a_usage_error_and_runs_nothing(monkeypatch):
    recorders = _patch_runners(monkeypatch)
    assert cli.main(["recommendations", "lots"]) == 2
    assert recorders["recommendations"].calls == []


def test_a_runner_failure_propagates(monkeypatch):
    # A sweep that raises must not be swallowed — the process exits non-zero (the CLI lets the
    # exception surface so the traceback lands in the task's CloudWatch logs).
    def boom(_limit):
        raise RuntimeError("yahoo blocked")

    monkeypatch.setattr(cli, "RUNNERS", {"universe": boom})
    with pytest.raises(RuntimeError, match="yahoo blocked"):
        cli.main(["universe"])
