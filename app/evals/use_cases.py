"""Application Business Rules: the eval-suite runner.

``RunEvalSuite`` runs each case through the subject under test and the judge, and aggregates the
per-case results into an ``EvalReport``. It owns the harness's one policy decision: a failure at
the *run* level — the subject endpoint being down, or the judge model erroring — is recorded as
a failing, ``errored`` result rather than allowed to abort the whole run. So a suite always
produces a full report (every case accounted for), which is what makes it usable as a CI gate:
one flaky endpoint call fails its case, not the build's ability to report at all.

Depends only on its two ports — the subject and the judge — never a vendor or a transport.
"""

import logging
from collections.abc import Sequence

from app.evals.entities import CaseResult, EvalCase, EvalReport, Grade
from app.evals.exceptions import EvalError
from app.evals.ports import AnswerUnderTest, Judge

logger = logging.getLogger(__name__)


class RunEvalSuite:
    """Use case: grade a set of cases against a subject, returning the aggregate report."""

    def __init__(self, subject: AnswerUnderTest, judge: Judge) -> None:
        self._subject = subject
        self._judge = judge

    def execute(self, cases: Sequence[EvalCase]) -> EvalReport:
        """Run every case and aggregate the results. Never raises for a per-case failure — a
        broken subject or judge call becomes an ``errored`` result so the report stays complete."""
        return EvalReport(results=tuple(self._run_case(case) for case in cases))

    def _run_case(self, case: EvalCase) -> CaseResult:
        try:
            answer = self._subject.answer(case.question)
        except EvalError as exc:
            logger.warning("eval subject failed on case %s: %s", case.id, exc)
            return _errored(case, "", f"subject unavailable: {exc}")
        try:
            grade = self._judge.grade(case, answer)
        except EvalError as exc:
            logger.warning("eval judge failed on case %s: %s", case.id, exc)
            return _errored(case, answer, f"judge unavailable: {exc}")
        return CaseResult(case=case, answer=answer, grade=grade)


def _errored(case: EvalCase, answer: str, reasoning: str) -> CaseResult:
    """A run-level failure as a failing result — score 0, marked ``errored`` so the report can
    tell a broken run apart from an answer that simply lost on the merits."""
    return CaseResult(
        case=case,
        answer=answer,
        grade=Grade(passed=False, score=0.0, reasoning=reasoning),
        errored=True,
    )
