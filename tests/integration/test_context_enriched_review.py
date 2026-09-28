"""Integration test: context-needed routing enriches the InvokeLLM call (quickstart Scenario 3)."""

import json

from integrations.embeddings import StubEmbeddingClient
from integrations.storage import StubStorage
from invoke_llm.handler import invoke_llm
from retrieve_context.handler import INDEX_KEY, retrieve_context
from retrieve_context.ranking import TOP_N

from ..conftest import vector


def test_context_enriched_review_includes_retrieved_context(
    pr_event, stub_storage, stub_embedding_client, stub_llm_router
):
    event_after_route = {**pr_event, "routing": {"complexity": "high", "needsContext": True}}

    retrieved = retrieve_context(
        event_after_route, storage=stub_storage, embedding_client=stub_embedding_client
    )
    event_after_retrieve = {**event_after_route, "context": retrieved}

    invoke_llm(event_after_retrieve, storage=stub_storage, llm_router=stub_llm_router)

    call = stub_llm_router.calls[0]
    assert call["diff_text"]
    # The retrieved chunks reach Gemini as prompt text, labelled as reference-only material
    # rather than part of the diff (build_prompt in integrations/llm_router.py).
    assert [chunk.path for chunk in call["context_chunks"]] == [
        chunk["path"] for chunk in retrieved["chunks"]
    ]
    assert "=== ADDITIONAL PROJECT CONTEXT ===" in call["prompt"]
    assert retrieved["chunks"][0]["text"] in call["prompt"]


def test_v2_index_reaches_the_prompt_as_located_chunks_grouped_by_file(
    pr_small_event, pr_small_diff, rag_index_v2, stub_llm_router
):
    """Version 2 end to end: per-file query, the changed method left out, the rest grouped
    by file in the prompt with line ranges (specs/002-method-chunking US1)."""
    storage = StubStorage(
        initial={
            pr_small_event["diffKey"]: pr_small_diff,
            INDEX_KEY: json.dumps(rag_index_v2),
        }
    )
    event = {**pr_small_event, "routing": {"complexity": "medium", "needsContext": True}}

    retrieved = retrieve_context(
        event, storage=storage, embedding_client=StubEmbeddingClient(vector=vector(1.0))
    )
    invoke_llm({**event, "context": retrieved}, storage=storage, llm_router=stub_llm_router)

    prompt = stub_llm_router.calls[0]["prompt"]
    context = prompt.split("=== ADDITIONAL PROJECT CONTEXT ===\n", 1)[1]
    assert 0 < len(retrieved["chunks"]) <= TOP_N
    assert "login(LoginRequest request)" not in context  # overlaps the diff: excluded
    assert context.count("--- src/main/java/com/codereview/app/auth/JwtValidator.java ---") == 1
    assert "[lines 40-60]" in context and "[lines 61-75]" in context
    assert context.count("package com.codereview.app.auth;") == 3  # once per auth file
