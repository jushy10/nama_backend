from abc import ABC, abstractmethod

from app.stocks.ai.agent.entities import AgentRecipe


class AgentRecipeRepositoryAdapter(ABC):
    @abstractmethod
    def get(self, name: str) -> AgentRecipe | None:
        """Return the stored recipe for ``name``, or None when none is configured."""
        raise NotImplementedError
