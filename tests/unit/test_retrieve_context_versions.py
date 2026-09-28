"""RetrieveContext reads index versions 1 and 2 (specs/002-method-chunking, FR-014/FR-015).

The v1 path must stay byte-for-byte what it was before 002: `golden_v1_context.json` was
captured from the pre-002 code (commit 8e0ee47's tree) on `index_v1_small.json` and
`pr_small.diff`, so any drift in ranking, chunk content or output shape fails here.
"""

import json
from pathlib import Path

import pytest

from integrations.embeddings import StubEmbeddingClient
from integrations.storage import StubStorage
from retrieve_context.handler import INDEX_KEY, IndexCompatibilityError, retrieve_context

from ..conftest import vector

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


def _storage(event: dict, diff: str, index: dict) -> StubStorage:
    return StubStorage(initial={event["diffKey"]: diff, INDEX_KEY: json.dumps(index)})


def _v1_index() -> dict:
    return json.loads((FIXTURES / "index_v1_small.json").read_text(encoding="utf-8"))


def test_v1_output_is_unchanged_from_before_002(pr_small_event, pr_small_diff):
    golden = json.loads((FIXTURES / "golden_v1_context.json").read_text(encoding="utf-8"))
    embedding_client = StubEmbeddingClient(vector=vector(1.0))

    output = retrieve_context(
        pr_small_event,
        storage=_storage(pr_small_event, pr_small_diff, _v1_index()),
        embedding_client=embedding_client,
    )

    # Identical, plus the one new top-level field saying which format it came from.
    assert output == {**golden, "index_version": 1}
    # Still one query for the whole diff, through the single-query call, never the batch one.
    assert embedding_client.calls == [pr_small_diff]
    assert embedding_client.batch_calls == []


def test_index_without_a_version_field_is_read_as_v1(pr_small_event, pr_small_diff):
    index = _v1_index()
    del index["version"]

    output = retrieve_context(
        pr_small_event,
        storage=_storage(pr_small_event, pr_small_diff, index),
        embedding_client=StubEmbeddingClient(vector=vector(1.0)),
    )

    assert output["index_version"] == 1


def test_v2_index_is_read_with_the_v2_path(pr_small_event, pr_small_diff, rag_index_v2):
    embedding_client = StubEmbeddingClient(vector=vector(1.0))

    output = retrieve_context(
        pr_small_event,
        storage=_storage(pr_small_event, pr_small_diff, rag_index_v2),
        embedding_client=embedding_client,
    )

    assert output["index_version"] == 2
    assert embedding_client.calls == []  # v2 embeds per-file queries through the batch call
    assert len(embedding_client.batch_calls) == 1


@pytest.mark.parametrize("version", [0, 3, "2"])
def test_unknown_index_version_fails_before_any_embedding_call(
    pr_small_event, pr_small_diff, version
):
    index = _v1_index()
    index["version"] = version
    embedding_client = StubEmbeddingClient(vector=vector(1.0))

    with pytest.raises(IndexCompatibilityError, match="version"):
        retrieve_context(
            pr_small_event,
            storage=_storage(pr_small_event, pr_small_diff, index),
            embedding_client=embedding_client,
        )

    assert embedding_client.calls == [] and embedding_client.batch_calls == []


def test_missing_index_output_carries_no_version(pr_small_event, pr_small_diff):
    storage = StubStorage(initial={pr_small_event["diffKey"]: pr_small_diff})

    output = retrieve_context(
        pr_small_event, storage=storage, embedding_client=StubEmbeddingClient()
    )

    assert output["index_available"] is False
    assert "index_version" not in output


def test_v2_output_excludes_the_changed_method_and_ranks_the_rest(
    pr_small_event, pr_small_diff, rag_index_v2
):
    """pr_small.diff inserts a line inside AuthController.login (old lines 16/17), so the
    login chunk (lines 14-20) is left out even though it scores highest; everything else is
    ranked by similarity and carries its location, header and score."""
    output = retrieve_context(
        pr_small_event,
        storage=_storage(pr_small_event, pr_small_diff, rag_index_v2),
        embedding_client=StubEmbeddingClient(vector=vector(1.0)),
    )

    ids = [chunk["id"].split("#", 1)[1] for chunk in output["chunks"]]
    assert ids == [
        "InMemoryUsers.isValid:10-12",
        "JwtValidator.isValid:40-60#part-1",
        "LoginResponse:3-4",
        "JwtValidator.isValid:61-75#part-2",
        "TaskService.findAll:8-10",
        "L1-12",
    ]
    first = output["chunks"][0]
    assert first["start_line"] == 10 and first["end_line"] == 12
    assert first["header"].startswith("package com.codereview.app.auth;")
    assert first["score"] == 0.9949  # 0.99 / sqrt(0.99² + 0.1²)
    assert first["matched_query"] == "src/main/java/com/codereview/app/auth/AuthController.java"
    # A block's empty header is kept as "", not dropped: only None is excluded from the dump.
    assert output["chunks"][-1]["header"] == ""
