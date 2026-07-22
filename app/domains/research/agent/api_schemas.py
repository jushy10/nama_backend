import datetime

from pydantic import BaseModel, Field

from app.domains.research.agent.entities import ResearchResult

# Authored by the service, not the model: the research read is informational only.
_RESEARCH_DISCLAIMER = (
    "AI-generated for informational and educational purposes only — not financial advice. "
    "Markets carry risk; do your own research before investing."
)


class ResearchRequest(BaseModel):
    question: str = Field(min_length=1, max_length=1000)


class AgentStepResponse(BaseModel):
    tool: str
    arguments: dict
    output: str
    is_error: bool


class ResearchResponse(BaseModel):
    question: str
    answer: str
    steps: list[AgentStepResponse]
    disclaimer: str
    model: str
    generated_at: datetime.datetime

    @classmethod
    def from_result(cls, result: ResearchResult) -> "ResearchResponse":
        return cls(
            question=result.question,
            answer=result.answer,
            steps=[
                AgentStepResponse(
                    tool=step.tool,
                    arguments=step.arguments,
                    output=step.output,
                    is_error=step.is_error,
                )
                for step in result.steps
            ],
            disclaimer=_RESEARCH_DISCLAIMER,
            model=result.model,
            generated_at=result.generated_at,
        )
