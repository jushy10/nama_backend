import httpx

from app.evals.exceptions import SubjectUnavailable
from app.evals.interfaces import AnswerUnderTestAdapter


class AnswerUnderTestAdapterImpl(AnswerUnderTestAdapter):
    def __init__(
        self,
        *,
        base_url: str = "http://localhost:8080",
        path: str = "/agents/research",
        answer_field: str = "answer",
        timeout: float = 60.0,
        client: httpx.Client | None = None,
    ) -> None:
        self._path = path
        self._answer_field = answer_field
        self._owns_client = client is None
        self._client = client or httpx.Client(base_url=base_url, timeout=timeout)

    def answer(self, question: str) -> str:
        try:
            response = self._client.post(self._path, json={"question": question})
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:  # transport error or non-JSON body
            raise SubjectUnavailable(f"request to {self._path} failed: {exc}") from exc
        value = payload.get(self._answer_field) if isinstance(payload, dict) else None
        if not isinstance(value, str) or not value.strip():
            raise SubjectUnavailable(
                f"response from {self._path} had no '{self._answer_field}' text"
            )
        return value

    def close(self) -> None:
        if self._owns_client:
            self._client.close()
