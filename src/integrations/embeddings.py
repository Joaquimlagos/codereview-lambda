"""Gemini embeddings abstraction: turns text into a vector for RAG retrieval (Principle IV).

Query side of the contract codereview-app's `scripts/build_index.py` owns. That script embeds
the indexed code (whole files in index version 1, methods and blocks in version 2) with
`taskType: RETRIEVAL_DOCUMENT`; this client embeds the PR diff at review time with
`RETRIEVAL_QUERY` — one query for the whole diff (`embed_query`, version 1) or one per changed
file in a single batch call (`embed_queries`, version 2). The asymmetric task types are what
the model expects
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
# batchEmbedContents accepts at most this many inputs per request.
MAX_BATCH_SIZE = 100


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

    @abstractmethod
    def embed_queries(self, texts: list[str]) -> list[list[float]]:
        """Embed several retrieval queries, in as few API calls as the batch limit allows.
        Returns one vector per text, in the same order. Raises EmbeddingError on failure."""


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

    def embed_queries(self, texts: list[str]) -> list[list[float]]:
        """One `batchEmbedContents` call per MAX_BATCH_SIZE texts. The response's
        `embeddings[i]` answers `requests[i]`; a response with a different count or a vector
        of the wrong length is rejected rather than silently mis-paired with its query."""
        vectors: list[list[float]] = []
        for start in range(0, len(texts), MAX_BATCH_SIZE):
            vectors.extend(self._embed_batch(texts[start : start + MAX_BATCH_SIZE]))
        return vectors

    def _embed_batch(self, texts: list[str]) -> list[list[float]]:
        url = f"{self._api_base}/models/{EMBEDDING_MODEL}:batchEmbedContents"
        payload = {
            "requests": [
                {
                    "model": f"models/{EMBEDDING_MODEL}",
                    "content": {"parts": [{"text": text}]},
                    "taskType": QUERY_TASK_TYPE,
                    "outputDimensionality": EMBEDDING_DIMENSIONS,
                }
                for text in texts
            ]
        }
        headers = {"x-goog-api-key": self._api_key}

        try:
            response = self._session.post(url, json=payload, headers=headers, timeout=30)
            response.raise_for_status()
            data = response.json()
        except Exception as exc:
            raise EmbeddingError(str(exc)) from exc

        embeddings = data.get("embeddings") or []
        if len(embeddings) != len(texts):
            raise EmbeddingError(
                f"Gemini batch embedding returned {len(embeddings)} vectors for "
                f"{len(texts)} inputs"
            )
        vectors = [(embedding or {}).get("values") or [] for embedding in embeddings]
        wrong = [i for i, v in enumerate(vectors) if len(v) != EMBEDDING_DIMENSIONS]
        if wrong:
            raise EmbeddingError(
                f"Gemini batch embedding returned vectors of the wrong length at {wrong}"
            )
        return vectors


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
        # One entry per embed_queries call: the texts it received, in order.
        self.batch_calls: list[list[str]] = []
        # Optional per-text vectors for embed_queries, so a test can give each changed file's
        # query its own direction; texts not in the map get the default vector.
        self.vectors_by_text: dict[str, list[float]] = {}
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

    def embed_queries(self, texts: list[str]) -> list[list[float]]:
        self.batch_calls.append(list(texts))
        if self.fail:
            raise EmbeddingError("stub configured to simulate an embeddings API failure")
        return [self.vectors_by_text.get(text, self._vector) for text in texts]
