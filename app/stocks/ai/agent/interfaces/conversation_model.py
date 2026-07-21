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
