"""PostComment Lambda: posts the GeneratedReview as a PR comment (FR-007, FR-008)."""

from contracts.models import GeneratedReview
from integrations.github import GitHubClient, RestGitHubClient
from integrations.secrets import resolve_api_key

# Local .env/env var fallback, else Secrets Manager via this Lambda's own SSM-looked-up ARN
# (this function's execution role MUST NOT have access to route-model's or invoke-llm's
# secrets — see infra/iam_post_comment.tf).
GITHUB_TOKEN_ENV = "GITHUB_TOKEN"
GITHUB_TOKEN_SSM_ARN_PARAM = "/codereview/secrets/github-token-arn"


def _default_github_client() -> GitHubClient:
    token = resolve_api_key(GITHUB_TOKEN_ENV, GITHUB_TOKEN_SSM_ARN_PARAM)
    return RestGitHubClient(token=token)


def post_comment(event: dict, github_client: GitHubClient | None = None) -> dict:
    # Re-validating here enforces FR-008 defensively: GeneratedReview's own validation rejects
    # an empty/malformed review_text, so this MUST NOT construct/post a comment for one.
    # Read from "analysis" — the ResultPath key codereview-infra's ASL nests InvokeLLM's
    # output under (confirmed against statemachine/definition.asl.json.tpl).
    review = GeneratedReview.model_validate(event["analysis"])
    repository = event["repository"]
    github_client = github_client or _default_github_client()

    # A posting failure MUST be visible (FR-007) — left uncaught so the Lambda invocation fails
    # rather than silently discarding the error.
    comment = github_client.post_comment(
        repository=repository, pr_id=review.pr_id, review_text=review.review_text
    )
    return comment.model_dump()


def handler(event: dict, context=None) -> dict:
    return post_comment(event)
