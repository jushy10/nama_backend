"""Tests for the eval-suite runner (RunEvalSuite).

Offline and vendor-free: a fake subject returns canned answers (or raises), and a fake judge
returns canned grades (or raises). This exercises the runner's one policy — that a subject or
judge failure becomes a recorded ``errored`` result rather than aborting the run — plus the
report aggregation, with no Bedrock and no server.
"""

from app.evals.entities import EvalCase, Grade
from app.evals.exceptions import JudgeUnavailable, SubjectUnavailable
from app.evals.use_cases import RunEvalSuite


class _FakeSubject:
    """Returns a per-question canned answer, or raises for a question in ``fail_on``."""

    def __init__(self, answers=None, fail_on=()) -> None:
        self._answers = answers or {}
        self._fail_on = set(fail_on)

    def answer(self, question: str) -> str:
        if question in self._fail_on:
            raise SubjectUnavailable(f"down for {question}")
        return self._answers.get(question, "a canned answer")


class _FakeJudge:
    """Grades by a per-case-id table of Grades, or raises for an id in ``fail_on``."""

    def __init__(self, grades=None, fail_on=()) -> None:
        self._grades = grades or {}
        self._fail_on = set(fail_on)
        self.seen: list[tuple[str, str]] = []

    def grade(self, case: EvalCase, answer: str) -> Grade:
        self.seen.append((case.id, answer))
        if case.id in self._fail_on:
            raise JudgeUnavailable(f"judge down for {case.id}")
        return self._grades.get(case.id, Grade(passed=True, score=1.0, reasoning="ok"))


def _case(cid, question="q", rubric="r", tags=()) -> EvalCase:
    return EvalCase(id=cid, question=question, rubric=rubric, tags=tags)


def test_aggregates_pass_and_fail_into_a_report():
    cases = [_case("a", "qa"), _case("b", "qb")]
    judge = _FakeJudge(
        grades={
            "a": Grade(passed=True, score=0.9, reasoning="good"),
            "b": Grade(passed=False, score=0.2, reasoning="bad"),
        }
    )
    report = RunEvalSuite(_FakeSubject(), judge).execute(cases)

    assert report.total == 2
    assert report.passed == 1
    assert report.failed == 1
    assert report.pass_rate == 0.5
    assert report.average_score == 0.55
    assert [r.case.id for r in report.failures] == ["b"]
    # The judge saw each case's answer.
    assert judge.seen == [("a", "a canned answer"), ("b", "a canned answer")]


def test_a_subject_failure_is_a_recorded_errored_result():
    cases = [_case("a", "qa"), _case("b", "qb")]
    report = RunEvalSuite(_FakeSubject(fail_on=["qa"]), _FakeJudge()).execute(cases)

    a = next(r for r in report.results if r.case.id == "a")
    assert a.errored is True
    assert a.grade.passed is False
    assert a.grade.score == 0.0
    assert "subject unavailable" in a.grade.reasoning
    # The other case still ran and passed — one failure doesn't abort the run.
    assert report.total == 2 and report.passed == 1 and report.errored == 1


def test_a_judge_failure_is_a_recorded_errored_result():
    judge = _FakeJudge(fail_on=["a"])
    report = RunEvalSuite(_FakeSubject(), judge).execute([_case("a")])

    result = report.results[0]
    assert result.errored is True
    assert result.grade.passed is False
    assert "judge unavailable" in result.grade.reasoning
    # The subject's answer is still captured even though grading failed.
    assert result.answer == "a canned answer"


def test_meets_threshold_gate():
    judge = _FakeJudge(
        grades={
            "a": Grade(True, 1.0, "ok"),
            "b": Grade(True, 1.0, "ok"),
            "c": Grade(False, 0.0, "no"),
        }
    )
    report = RunEvalSuite(_FakeSubject(), judge).execute(
        [_case("a"), _case("b"), _case("c")]
    )
    assert report.pass_rate == 2 / 3
    assert report.meets(0.6) is True
    assert report.meets(0.7) is False


def test_empty_run_is_a_zero_report():
    report = RunEvalSuite(_FakeSubject(), _FakeJudge()).execute([])
    assert report.total == 0
    assert report.pass_rate == 0.0
    assert report.average_score == 0.0
    assert report.meets(0.1) is False
