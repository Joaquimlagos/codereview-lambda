"""GeminiEmbeddingClient.embed_queries against a fake HTTP session: batching, request shape,
and rejection of responses that cannot be paired back with their queries."""

import json

import pytest

from integrations.embeddings import (
    EMBEDDING_DIMENSIONS,
    EMBEDDING_MODEL,
    MAX_BATCH_SIZE,
    EmbeddingError,
    GeminiEmbeddingClient,
    StubEmbeddingClient,
)


class FakeResponse:
    def __init__(self, status_code: int, body: dict):
        self.status_code = status_code
        self._body = body
        self.text = json.dumps(body)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._body


class BatchSession:
    """Answers each batch with one distinct vector per request (first value = global index),
    unless `respond` overrides the body."""

    def __init__(self, respond=None):
        self.requests: list[dict] = []
        self._respond = respond
        self._seen = 0

    def post(self, url, json, headers, timeout):
        self.requests.append({"url": url, "payload": json})
        if self._respond:
            return self._respond(json)
        body = {
            "embeddings": [
                {"values": [float(self._seen + i)] + [0.0] * (EMBEDDING_DIMENSIONS - 1)}
                for i in range(len(json["requests"]))
            ]
        }
        self._seen += len(json["requests"])
        return FakeResponse(200, body)


def _client(session) -> GeminiEmbeddingClient:
    return GeminiEmbeddingClient(
        api_base="https://gemini.test/v1beta", api_key="k", session=session
    )


def test_batch_request_shape_matches_batch_embed_contents():
    session = BatchSession()

    _client(session).embed_queries(["diff a", "diff b"])

    request = session.requests[0]
    assert request["url"] == (
        f"https://gemini.test/v1beta/models/{EMBEDDING_MODEL}:batchEmbedContents"
    )
    assert request["payload"]["requests"][1] == {
        "model": f"models/{EMBEDDING_MODEL}",
        "content": {"parts": [{"text": "diff b"}]},
        "taskType": "RETRIEVAL_QUERY",
        "outputDimensionality": EMBEDDING_DIMENSIONS,
    }


def test_more_than_one_batch_is_split_at_the_api_limit_and_order_is_kept():
    session = BatchSession()
    texts = [f"diff {i}" for i in range(MAX_BATCH_SIZE + 50)]

    vectors = _client(session).embed_queries(texts)

    assert [len(r["payload"]["requests"]) for r in session.requests] == [MAX_BATCH_SIZE, 50]
    assert [v[0] for v in vectors] == [float(i) for i in range(len(texts))]


def test_a_response_with_a_different_count_is_rejected():
    session = BatchSession(respond=lambda payload: FakeResponse(200, {"embeddings": []}))

    with pytest.raises(EmbeddingError, match="0 vectors for 2 inputs"):
        _client(session).embed_queries(["a", "b"])


def test_a_vector_of_the_wrong_length_is_rejected():
    session = BatchSession(
        respond=lambda payload: FakeResponse(200, {"embeddings": [{"values": [0.1, 0.2]}]})
    )

    with pytest.raises(EmbeddingError, match="wrong length"):
        _client(session).embed_queries(["a"])


def test_http_errors_become_embedding_errors():
    session = BatchSession(respond=lambda payload: FakeResponse(429, {"error": "quota"}))

    with pytest.raises(EmbeddingError):
        _client(session).embed_queries(["a"])


def test_stub_returns_per_text_vectors_and_records_each_batch():
    stub = StubEmbeddingClient(vector=[0.0] * EMBEDDING_DIMENSIONS)
    stub.vectors_by_text = {"b": [1.0] * EMBEDDING_DIMENSIONS}

    vectors = stub.embed_queries(["a", "b"])

    assert stub.batch_calls == [["a", "b"]]
    assert vectors[0][0] == 0.0 and vectors[1][0] == 1.0
