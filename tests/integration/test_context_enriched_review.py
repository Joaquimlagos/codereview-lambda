"""Integration test: context-needed routing enriches the InvokeLLM call (quickstart Scenario 3)."""

from invoke_llm.handler import invoke_llm
from retrieve_context.handler import retrieve_context


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
