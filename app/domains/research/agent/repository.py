from abc import ABC, abstractmethod

from app.domains.research.agent.entities import AgentRecipe


class AgentRecipeRepository(ABC):
    @abstractmethod
    def get(self, name: str) -> AgentRecipe | None:
        """Return the stored recipe for ``name``, or None when none is configured."""
        raise NotImplementedError
