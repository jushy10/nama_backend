import logging
from collections.abc import Sequence

from app.evals.entities import CaseResult, EvalCase, EvalReport, Grade
from app.evals.exceptions import EvalError
from app.evals.ports import AnswerUnderTest, Judge

logger = logging.getLogger(__name__)


class RunEvalSuite:
    def __init__(self, subject: AnswerUnderTest, judge: Judge) -> None:
        self._subject = subject
        self._judge = judge

    def execute(self, cases: Sequence[EvalCase]) -> EvalReport:
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
    return CaseResult(
        case=case,
        answer=answer,
        grade=Grade(passed=False, score=0.0, reasoning=reasoning),
        errored=True,
    )
