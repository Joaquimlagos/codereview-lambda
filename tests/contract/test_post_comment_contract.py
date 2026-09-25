"""Contract test for PostComment: output MUST validate against ReviewComment (FR-012),
covering an inline-comment review, a summary-only review, and the 422-invalid-line fallback.
"""

from contracts.models import ReviewComment
from post_comment.handler import post_comment


def _event_with_analysis(pr_event: dict, analysis: dict) -> dict:
    return {**pr_event, "analysis": analysis}


def test_post_comment_output_matches_review_comment_contract(pr_event, stub_github_client):
    event = _event_with_analysis(
        pr_event,
        {
            "pr_id": str(pr_event["prNumber"]),
            "summary": "Looks good.",
            "comments": [],
            "model_used": "gemini-flash",
        },
    )

    output = post_comment(event, github_client=stub_github_client)

    validated = ReviewComment.model_validate(output)
    assert validated.pr_id == str(pr_event["prNumber"])
    assert validated.posted is True
    assert len(stub_github_client.posted_reviews) == 1


def test_post_comment_posts_inline_comments(pr_event, stub_github_client):
    event = _event_with_analysis(
        pr_event,
        {
            "pr_id": str(pr_event["prNumber"]),
            "summary": "One observation below.",
            "comments": [{"path": "src/app.py", "line": 5, "body": "Log the exception."}],
            "model_used": "gemini-flash",
        },
    )

    output = post_comment(event, github_client=stub_github_client)

    validated = ReviewComment.model_validate(output)
    assert validated.posted is True
    assert len(stub_github_client.posted_reviews) == 1
    assert len(stub_github_client.posted_comments) == 0


def test_post_comment_falls_back_to_conversational_comment_on_invalid_line(
    pr_event, stub_github_client
):
    """GitHub rejects (422) the whole review if any comment's line isn't part of the diff.
    PostComment MUST still succeed — degrading to a single conversational comment instead of
    losing the review entirely."""
    stub_github_client.reject_inline_comments = True
    event = _event_with_analysis(
        pr_event,
        {
            "pr_id": str(pr_event["prNumber"]),
            "summary": "One observation below.",
            "comments": [{"path": "src/app.py", "line": 999, "body": "Out-of-diff line."}],
            "model_used": "gemini-flash",
        },
    )

    output = post_comment(event, github_client=stub_github_client)

    validated = ReviewComment.model_validate(output)
    assert validated.posted is True
    assert len(stub_github_client.posted_reviews) == 0
    assert len(stub_github_client.posted_comments) == 1
