import threading

from fastapi import Response

from app.stocks.endpoints import background_sync


class _FakeRunner:
    def __init__(self, *, boom: bool = False) -> None:
        self.calls: list[int] = []
        self._boom = boom

    def __call__(self, limit: int) -> str:
        self.calls.append(limit)
        if self._boom:
            raise RuntimeError("simulated sweep failure")
        return "ok"


def _drain(lock: threading.Lock) -> None:
    assert lock.acquire(timeout=2), "background sweep did not finish in time"
    lock.release()


def test_starts_the_sweep_and_reports_accepted():
    lock = threading.Lock()
    runner = _FakeRunner()
    result = background_sync.trigger_sync(lock, runner, 50, Response(), label="test")
    assert result.status == "accepted"
    assert result.limit == 50
    _drain(lock)
    assert runner.calls == [50]  # the runner ran, with the given limit


def test_a_trigger_while_a_sweep_runs_is_a_noop():
    lock = threading.Lock()
    runner = _FakeRunner()
    assert lock.acquire(blocking=False)  # simulate a sweep already in flight
    try:
        response = Response()
        response.status_code = 202  # the route's default, which the no-op path overrides
        result = background_sync.trigger_sync(lock, runner, 50, response, label="test")
        assert result.status == "already_running"
        assert response.status_code == 200  # overridden to 200 for the no-op
        assert runner.calls == []  # nothing started
    finally:
        lock.release()


def test_a_runner_error_is_swallowed_and_the_guard_released():
    lock = threading.Lock()
    runner = _FakeRunner(boom=True)
    result = background_sync.trigger_sync(lock, runner, 10, Response(), label="test")
    assert result.status == "accepted"
    # The thread must release the guard even though the runner raised — otherwise this
    # barrier would hang — and the exception must not have propagated out of the thread.
    _drain(lock)
    assert runner.calls == [10]
