from __future__ import annotations

from sqlalchemy.orm import Session

from app.domains.research.agent import models
from app.domains.research.agent.entities import AgentRecipe
from app.domains.research.agent.repository import AgentRecipeRepository


class DbAgentRecipeRepository(AgentRecipeRepository):
    def __init__(self, session: Session) -> None:
        self._session = session

    def get(self, name: str) -> AgentRecipe | None:
        record = models.recipe_by_name(self._session, name)
        if record is None:
            return None
        return AgentRecipe(
            name=record.name,
            system_prompt=record.system_prompt,
            tool_names=tuple(str(item) for item in record.tool_names),
            max_steps=record.max_steps,
            model_id=record.model_id,
        )
