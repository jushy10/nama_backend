import datetime

from pydantic import BaseModel, Field


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
