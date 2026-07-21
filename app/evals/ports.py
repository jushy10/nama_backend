"""Application ports: the two abstractions the eval-suite use case depends on.

``AnswerUnderTest`` is the thing being graded — anything that turns a question into an answer
string. A concrete adapter posts to a running endpoint; a test injects a canned one. This
inversion is what lets the harness grade *any* answer producer (the research agent, an analysis
endpoint, a future one) without the use case knowing which.

``Judge`` is the grader — given a case (its rubric) and an answer, it returns a ``Grade``. The
default judge is a model scoring against the rubric (LLM-as-judge); it too is behind a port, so
the whole suite runs offline against a deterministic fake.
"""

from abc import ABC, abstractmethod

from app.evals.entities import EvalCase, Grade


class AnswerUnderTest(ABC):
    """A gateway for the subject being evaluated: one question in, one answer out."""

    @abstractmethod
    def answer(self, question: str) -> str:
        """Produce the subject's answer to ``question``.

        Raises:
            SubjectUnavailable: the subject could not be reached or gave no usable answer.
        """
        raise NotImplementedError


class Judge(ABC):
    """A gateway for grading one answer against a case's rubric."""

    @abstractmethod
    def grade(self, case: EvalCase, answer: str) -> Grade:
        """Score ``answer`` against ``case``'s rubric.

        Raises:
            JudgeUnavailable: the grader itself failed (e.g. the grading model call errored).
        """
        raise NotImplementedError
