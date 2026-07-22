import datetime

from pydantic import BaseModel, Field

from app.domains.research.agent.entities import RESEARCH_DISCLAIMER, ResearchResult


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
            disclaimer=RESEARCH_DISCLAIMER,
            model=result.model,
            generated_at=result.generated_at,
        )
