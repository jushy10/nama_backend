from abc import ABC, abstractmethod


class AnswerUnderTestAdapter(ABC):
    @abstractmethod
    def answer(self, question: str) -> str:
        raise NotImplementedError
