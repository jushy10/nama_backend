from abc import ABC, abstractmethod
from app.evals.entities import EvalCase, Grade


class JudgeAdapter(ABC):
    @abstractmethod
    def grade(self, case: EvalCase, answer: str) -> Grade:
        raise NotImplementedError
