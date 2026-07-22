from abc import ABC, abstractmethod
from typing import Generic, TypeVar

T = TypeVar("T")


class AiAnalysisCacheAdapter(ABC, Generic[T]):
    @abstractmethod
    def get(self, key: str) -> T | None:
        raise NotImplementedError

    @abstractmethod
    def put(self, key: str, analysis: T) -> None:
        raise NotImplementedError
