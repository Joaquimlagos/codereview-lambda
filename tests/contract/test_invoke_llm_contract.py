"""Contract test for InvokeLLM: output MUST validate against GeneratedReview (FR-012)."""

from contracts.models import GeneratedReview
from invoke_llm.handler import invoke_llm


def test_invoke_llm_output_matches_generated_review_contract(
    pr_event, stub_storage, stub_llm_router
):
    event = {**pr_event, "routing": {"complexity": "low", "needsContext": False}}

    output = invoke_llm(event, storage=stub_storage, llm_router=stub_llm_router)

    validated = GeneratedReview.model_validate(output)
    assert validated.pr_id == pr_event["pr_id"]
    assert validated.review_text
