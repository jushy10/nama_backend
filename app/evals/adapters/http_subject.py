"""Interface Adapter: a subject under test that posts a question to a running answer endpoint.

The only code that knows the HTTP transport exists. It implements ``AnswerUnderTest`` by POSTing
``{"question": ...}`` to a configured URL (the research endpoint by default) and reading the
answer out of the JSON response. This is what lets the harness grade the *deployed* behaviour of
any question-answering endpoint without importing its code — the coupling is a URL, not a module.

Any transport or shape failure (connection refused, non-2xx, missing field) becomes
``SubjectUnavailable`` — the one error the port documents — so the suite records a failing case
rather than aborting the run.
"""

import httpx

from app.evals.exceptions import SubjectUnavailable
from app.evals.ports import AnswerUnderTest


class HttpAnswerAdapter(AnswerUnderTest):
    """Posts each question to ``base_url + path`` and returns the ``answer_field`` from the JSON.

    ``base_url`` defaults to local dev; point it at a staging URL to grade a deployed build.
    ``answer_field`` is the response key holding the text (``answer`` for the research endpoint).
    ``client`` is an injection seam — pass a ready-made ``httpx.Client`` (a test transport) to
    bypass the network entirely.
    """

    def __init__(
        self,
        *,
        base_url: str = "http://localhost:8080",
        path: str = "/research",
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
        """Close the underlying client if this adapter created it (not an injected one)."""
        if self._owns_client:
            self._client.close()
