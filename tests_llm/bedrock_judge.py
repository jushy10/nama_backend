"""DeepEval judge backed by Claude on Bedrock — the same auth path as the app (AWS
credentials, no API key). DeepEval's metrics default to OpenAI; every metric in this
suite passes this judge explicitly."""

import json
import re

from deepeval.models import DeepEvalBaseLLM

_DEFAULT_JUDGE_MODEL = "us.anthropic.claude-sonnet-5-v1:0"


def _extract_json(text: str) -> str:
    # Judges sometimes wrap JSON in prose or code fences; keep the outermost object.
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    if fenced:
        return fenced.group(1)
    start, end = text.find("{"), text.rfind("}")
    return text[start : end + 1] if start != -1 and end > start else text


class BedrockJudge(DeepEvalBaseLLM):
    def __init__(self, model_id: str = _DEFAULT_JUDGE_MODEL, region: str = "us-east-1") -> None:
        from anthropic import AnthropicBedrock

        self._model_id = model_id
        self._client = AnthropicBedrock(aws_region=region)

    def load_model(self):
        return self._client

    def get_model_name(self) -> str:
        return self._model_id

    def generate(self, prompt: str, schema=None):
        message = self._client.messages.create(
            model=self._model_id,
            max_tokens=2048,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(
            block.text for block in message.content if getattr(block, "type", None) == "text"
        )
        if schema is not None:
            # DeepEval passes a pydantic schema when it wants structured output.
            return schema.model_validate(json.loads(_extract_json(text)))
        return text

    async def a_generate(self, prompt: str, schema=None):
        return self.generate(prompt, schema)
