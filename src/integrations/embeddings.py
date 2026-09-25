"""Gemini embeddings abstraction: turns text into a vector for RAG retrieval (Principle IV).

Query side of the contract codereview-app's `scripts/build_index.py` owns. That script embeds
whole files at index time with `taskType: RETRIEVAL_DOCUMENT`; this client embeds the PR diff
at review time with `RETRIEVAL_QUERY` — the asymmetric task types are what the model expects
for retrieval, and both sides MUST still use the same model and dimensionality, since vectors
from different models (or truncated to different sizes) live in different spaces and comparing
them yields meaningless scores. RetrieveContext enforces that with an explicit check against
the index's own `model`/`dimensions` fields before scoring anything.
"""

from abc import ABC, abstractmethod

# Must match codereview-app's build_index.py (EMBEDDING_MODEL / EMBEDDING_DIMENSIONS there).
EMBEDDING_MODEL = "gemini-embedding-001"
EMBEDDING_DIMENSIONS = 768
QUERY_TASK_TYPE = "RETRIEVAL_QUERY"


class EmbeddingError(Exception):
    """Raised when the embeddings API is unavailable or returns an invalid response."""


class EmbeddingClient(ABC):
    @property
    @abstractmethod
    def model(self) -> str:
        """The model whose vector space this client produces — checked against the index's."""

    @property
    @abstractmethod
    def dimensions(self) -> int:
        """Vector length this client produces — checked against the index's."""

    @abstractmethod
    def embed_query(self, text: str) -> list[float]:
        """Embed `text` as a retrieval *query*. Raises EmbeddingError on failure."""


class GeminiEmbeddingClient(EmbeddingClient):
    def __init__(self, api_base: str, api_key: str, session=None):
        import requests

        self._api_base = api_base
        self._api_key = api_key
        self._session = session or requests.Session()

    @property
    def model(self) -> str:
        return EMBEDDING_MODEL

    @property
    def dimensions(self) -> int:
        return EMBEDDING_DIMENSIONS

    def embed_query(self, text: str) -> list[float]:
        url = f"{self._api_base}/models/{EMBEDDING_MODEL}:embedContent"
        payload = {
            "model": f"models/{EMBEDDING_MODEL}",
            "content": {"parts": [{"text": text}]},
            "taskType": QUERY_TASK_TYPE,
            "outputDimensionality": EMBEDDING_DIMENSIONS,
        }
        headers = {"x-goog-api-key": self._api_key}

        try:
            response = self._session.post(url, json=payload, headers=headers, timeout=30)
            response.raise_for_status()
            data = response.json()
        except Exception as exc:
            raise EmbeddingError(str(exc)) from exc

        vector = (data.get("embedding") or {}).get("values")
        if not vector:
            raise EmbeddingError(f"Gemini embedding response carries no values: {data}")
        return vector


class StubEmbeddingClient(EmbeddingClient):
    """Network-free stand-in for tests (Principle IV, FR-011)."""

    def __init__(
        self,
        vector: list[float] | None = None,
        model: str = EMBEDDING_MODEL,
        dimensions: int = EMBEDDING_DIMENSIONS,
    ):
        self._vector = vector if vector is not None else [0.0] * dimensions
        self._model = model
        self._dimensions = dimensions
        self.calls: list[str] = []
        self.fail: bool = False

    @property
    def model(self) -> str:
        return self._model

    @property
    def dimensions(self) -> int:
        return self._dimensions

    def embed_query(self, text: str) -> list[float]:
        self.calls.append(text)
        if self.fail:
            raise EmbeddingError("stub configured to simulate an embeddings API failure")
        return self._vector
