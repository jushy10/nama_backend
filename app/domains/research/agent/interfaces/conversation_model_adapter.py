from abc import ABC, abstractmethod
from collections.abc import Sequence

from app.domains.research.agent.entities import Message, ModelTurn, ToolSpec


class ConversationModelAdapter(ABC):
    @abstractmethod
    def respond(
        self,
        *,
        system: str,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec],
    ) -> ModelTurn:
        raise NotImplementedError
