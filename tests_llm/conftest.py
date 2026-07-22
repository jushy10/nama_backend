"""Shared fixtures for the metered LLM-eval suite.

These tests call a LIVE research agent and grade its answers with a Bedrock judge —
every run costs real model tokens. They are deliberately outside the offline suite
(pyproject testpaths = ["tests"]); run them explicitly:

    RESEARCH_AGENT_URL=http://localhost:8080 pytest tests_llm
"""

import os

import pytest


@pytest.fixture(scope="session")
def agent_url() -> str:
    url = os.environ.get("RESEARCH_AGENT_URL")
    if not url:
        pytest.skip("RESEARCH_AGENT_URL is not set — the LLM evals need a live server.")
    return url.rstrip("/")


@pytest.fixture(scope="session")
def judge():
    from tests_llm.bedrock_judge import BedrockJudge

    return BedrockJudge(region=os.environ.get("BEDROCK_REGION", "us-east-1"))


@pytest.fixture(scope="session")
def ask(agent_url):
    import httpx

    def _ask(question: str) -> dict:
        resp = httpx.post(
            f"{agent_url}/agents/research", json={"question": question}, timeout=120
        )
        resp.raise_for_status()
        return resp.json()

    return _ask
