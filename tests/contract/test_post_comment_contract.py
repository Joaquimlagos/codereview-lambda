"""Contract test for PostComment: output MUST validate against ReviewComment (FR-012)."""

from contracts.models import ReviewComment
from post_comment.handler import post_comment


def test_post_comment_output_matches_review_comment_contract(pr_event, stub_github_client):
    event = {
        **pr_event,
        "analysis": {
            "pr_id": pr_event["pr_id"],
            "review_text": "Looks good.",
            "model_used": "gemini-flash",
        },
    }

    output = post_comment(event, github_client=stub_github_client)

    validated = ReviewComment.model_validate(output)
    assert validated.pr_id == pr_event["pr_id"]
    assert validated.posted is True
