"""HTTP request/response models for the research-agent endpoint.

Pydantic is a web/serialization detail, so these DTOs live at the edge — separate from the
entities so the core stays framework-agnostic. The request is just the question; the response
carries the answer plus the ordered ``steps`` the agent took (the tools it called and what they
returned), so a client can show — and a reviewer can audit — how the answer was reached. The
disclaimer is attached by the presenter, not authored by the model: the read is informational.
"""

import datetime

from pydantic import BaseModel, Field


class ResearchRequest(BaseModel):
    """A plain-English stock-research question for the agent to answer."""

    question: str = Field(min_length=1, max_length=1000)


class AgentStepResponse(BaseModel):
    """One tool call the agent made on the way to its answer — the transcript that makes the
    agent's reasoning path inspectable."""

    tool: str
    arguments: dict
    output: str
    is_error: bool


class ResearchResponse(BaseModel):
    """The agent's answer, the steps it took, and the informational-use disclaimer."""

    question: str
    answer: str
    steps: list[AgentStepResponse]
    disclaimer: str
    model: str
    generated_at: datetime.datetime
