from abc import ABC, abstractmethod

from app.evals.entities import EvalCase, Grade


class AnswerUnderTest(ABC):
    @abstractmethod
    def answer(self, question: str) -> str:
        raise NotImplementedError


class Judge(ABC):
    @abstractmethod
    def grade(self, case: EvalCase, answer: str) -> Grade:
        raise NotImplementedError
