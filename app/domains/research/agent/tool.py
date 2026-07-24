from abc import ABC, abstractmethod

from app.domains.research.agent.entities import ToolSpec


class Tool(ABC):
    @property
    @abstractmethod
    def spec(self) -> ToolSpec:
        raise NotImplementedError

    @abstractmethod
    def run(self, arguments: dict) -> str:
        raise NotImplementedError
