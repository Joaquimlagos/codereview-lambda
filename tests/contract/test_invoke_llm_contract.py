"""Contract test for InvokeLLM: output MUST validate against GeneratedReview (FR-012),
including the structured summary/comments shape and the defensive parse-fallback path.
"""

from contracts.models import GeneratedReview
from invoke_llm.handler import invoke_llm


def test_invoke_llm_output_matches_generated_review_contract(
    pr_event, stub_storage, stub_llm_router
):
    event = {**pr_event, "routing": {"complexity": "low", "needsContext": False}}

    output = invoke_llm(event, storage=stub_storage, llm_router=stub_llm_router)

    validated = GeneratedReview.model_validate(output)
    assert validated.pr_id == str(pr_event["prNumber"])
    assert validated.summary
    assert validated.comments == []
    assert validated.parse_fallback is False


def test_invoke_llm_output_carries_inline_comments(pr_event, stub_storage, stub_llm_router):
    stub_llm_router.comments = [
        {"path": "src/app.py", "line": 5, "body": "Consider logging the exception too."}
    ]
    event = {**pr_event, "routing": {"complexity": "low", "needsContext": False}}

    output = invoke_llm(event, storage=stub_storage, llm_router=stub_llm_router)

    validated = GeneratedReview.model_validate(output)
    assert len(validated.comments) == 1
    assert validated.comments[0].path == "src/app.py"
    assert validated.comments[0].line == 5


def test_invoke_llm_degrades_gracefully_on_malformed_llm_response(
    pr_event, stub_storage, stub_llm_router
):
    """A response that isn't the instructed JSON shape MUST NOT break the run — it falls back
    to treating the raw response as the summary, with no inline comments, and flags that on
    the output via `parse_fallback` (task instruction: observable, not silently swallowed)."""
    stub_llm_router.malformed_response = True
    event = {**pr_event, "routing": {"complexity": "low", "needsContext": False}}

    output = invoke_llm(event, storage=stub_storage, llm_router=stub_llm_router)

    validated = GeneratedReview.model_validate(output)
    assert validated.summary
    assert validated.comments == []
    assert validated.parse_fallback is True
