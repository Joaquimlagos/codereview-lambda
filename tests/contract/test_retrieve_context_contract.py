"""Contract test for RetrieveContext: output MUST validate against RetrievedContext (FR-012),
plus the index-compatibility and missing-index paths the handler guards (FR-004).
"""

import json

import pytest

from contracts.models import RetrievedContext
from integrations.embeddings import StubEmbeddingClient
from integrations.storage import StubStorage
from retrieve_context.handler import (
    INDEX_KEY,
    TOP_K,
    IndexCompatibilityError,
    retrieve_context,
)


def test_retrieve_context_output_matches_contract(
    pr_event, stub_storage, stub_embedding_client
):
    output = retrieve_context(
        pr_event, storage=stub_storage, embedding_client=stub_embedding_client
    )

    validated = RetrievedContext.model_validate(output)
    assert validated.pr_id == str(pr_event["prNumber"])
    assert validated.index_available is True
    assert len(validated.chunks) == TOP_K
    # The diff is embedded as the retrieval query — the index is never re-embedded here.
    assert stub_embedding_client.calls == [stub_storage.get_text(pr_event["diffKey"])]


def test_retrieve_context_returns_most_similar_chunks_first(
    pr_event, stub_storage, stub_embedding_client
):
    """Ranking is by cosine similarity, not by the index's own chunk order: the fixture lists
    Delta (similarity 0.0) first, and it must be the one chunk TOP_K leaves out."""
    output = retrieve_context(
        pr_event, storage=stub_storage, embedding_client=stub_embedding_client
    )

    paths = [chunk["path"] for chunk in output["chunks"]]
    assert paths == ["src/Alpha.java", "src/Beta.java", "src/Gamma.java"]


def test_retrieve_context_degrades_gracefully_when_index_missing(
    pr_event, small_diff, stub_embedding_client
):
    """develop not indexed yet is not an error — the review just runs without project
    context, rather than failing the whole run."""
    storage = StubStorage(initial={pr_event["diffKey"]: small_diff})

    output = retrieve_context(
        pr_event, storage=storage, embedding_client=stub_embedding_client
    )

    validated = RetrievedContext.model_validate(output)
    assert validated.index_available is False
    assert validated.chunks == []


def test_retrieve_context_rejects_index_built_with_another_model(
    pr_event, small_diff, rag_index, query_vector
):
    rag_index["model"] = "text-embedding-004"
    storage = StubStorage(
        initial={pr_event["diffKey"]: small_diff, INDEX_KEY: json.dumps(rag_index)}
    )

    with pytest.raises(IndexCompatibilityError, match="not comparable"):
        retrieve_context(
            pr_event,
            storage=storage,
            embedding_client=StubEmbeddingClient(vector=query_vector),
        )


def test_retrieve_context_rejects_index_with_another_dimensionality(
    pr_event, small_diff, rag_index, query_vector
):
    rag_index["dimensions"] = 1536
    storage = StubStorage(
        initial={pr_event["diffKey"]: small_diff, INDEX_KEY: json.dumps(rag_index)}
    )

    with pytest.raises(IndexCompatibilityError, match="dimensions"):
        retrieve_context(
            pr_event,
            storage=storage,
            embedding_client=StubEmbeddingClient(vector=query_vector),
        )
