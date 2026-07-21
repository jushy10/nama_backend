from abc import ABC, abstractmethod
from collections.abc import Sequence

from app.stocks.ai.agent.entities import Message, ModelTurn, ToolSpec


class ConversationModel(ABC):
    @abstractmethod
    def respond(
        self,
        *,
        system: str,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec],
    ) -> ModelTurn:
        raise NotImplementedError


class Tool(ABC):
    @property
    @abstractmethod
    def spec(self) -> ToolSpec:
        raise NotImplementedError

    @abstractmethod
    def run(self, arguments: dict) -> str:
        raise NotImplementedError
