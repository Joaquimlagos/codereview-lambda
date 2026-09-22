"""Integration test: when needsContext is false, InvokeLLM runs from the diff alone (FR-005)."""

from invoke_llm.handler import invoke_llm


def test_context_not_needed_runs_without_retrieved_context(pr_event, stub_storage, stub_llm_router):
    event_after_route = {**pr_event, "routing": {"complexity": "low", "needsContext": False}}

    # No "context" key present in the event — RetrieveContext was never invoked (FR-005).
    invoke_llm(event_after_route, storage=stub_storage, llm_router=stub_llm_router)

    call = stub_llm_router.calls[0]
    assert call["context_text"] is None
