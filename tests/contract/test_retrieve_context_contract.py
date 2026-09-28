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
    EmptyDiffError,
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


@pytest.mark.parametrize("blank_diff", ["", "   ", "\n\n\t"])
def test_retrieve_context_rejects_empty_or_blank_diff(
    pr_event, rag_index, query_vector, blank_diff
):
    """An S3 object that exists but is empty/whitespace-only (e.g. a truncated write) MUST
    fail the run visibly rather than send Gemini empty text (its own opaque HTTP 400) or
    silently degrade to "no context" — a real diff always has at least a `diff --git` header,
    so this can only be a data problem, never a legitimate docs-only PR."""
    storage = StubStorage(
        initial={pr_event["diffKey"]: blank_diff, INDEX_KEY: json.dumps(rag_index)}
    )

    with pytest.raises(EmptyDiffError, match="empty"):
        retrieve_context(
            pr_event,
            storage=storage,
            embedding_client=StubEmbeddingClient(vector=query_vector),
        )


def test_context_chunk_accepts_both_the_v1_and_the_v2_shape():
    """v1 chunks carry only path/text; v2 chunks add their location, header and ranking
    (specs/002-method-chunking/contracts/retrieve-context-v2.md). Both must validate, so a
    RetrievedContext built from either index version reaches InvokeLLM intact."""
    v1 = RetrievedContext.model_validate(
        {"pr_id": "3", "chunks": [{"path": "src/A.java", "text": "class A {}"}]}
    )
    v2 = RetrievedContext.model_validate(
        {
            "pr_id": "3",
            "index_version": 2,
            "chunks": [
                {
                    "path": "src/A.java",
                    "text": "void run() {}",
                    "id": "src/A.java#A.run():3-3",
                    "start_line": 3,
                    "end_line": 3,
                    "header": "package a;\n\nclass A {",
                    "score": 0.81,
                    "matched_query": "src/B.java",
                }
            ],
        }
    )

    assert v1.chunks[0].id is None and v1.index_version is None
    assert v2.chunks[0].start_line == 3 and v2.index_version == 2
    # A v1 chunk dumped the way RetrieveContext dumps it stays exactly {path, text}.
    assert v1.chunks[0].model_dump(exclude_none=True) == {
        "path": "src/A.java",
        "text": "class A {}",
    }
