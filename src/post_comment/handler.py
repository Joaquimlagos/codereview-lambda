"""PostComment Lambda: posts the GeneratedReview as an inline, diff-anchored PR review
(FR-007, FR-008).

Uses GitHub's Reviews API (not a plain issue comment) so each entry in
`GeneratedReview.comments` lands next to the diff line it's about; `summary` becomes the
review's top-level body. The review is advisory (`event: "COMMENT"`) — never
APPROVE/REQUEST_CHANGES — matching the AI validation job's non-blocking status. If GitHub
rejects the review because a comment references a line outside the diff, the GitHubClient
falls back to a single conversational comment rather than losing the review (see
integrations/github.py).

Posts as a GitHub App installation, so the review appears as the App's bot account rather
than as whoever owns a personal access token (see research.md, "GitHub App authentication").
"""

from contracts.models import GeneratedReview
from integrations.config import require_env
from integrations.github import GitHubAppAuth, GitHubClient, RestGitHubClient
from integrations.secrets import resolve_secret_file

# App ID and installation ID are identifiers, not secrets: plain env vars (FR-009).
GITHUB_APP_ID_ENV = "GITHUB_APP_ID"
GITHUB_APP_INSTALLATION_ID_ENV = "GITHUB_APP_INSTALLATION_ID"
# The App's private key: a local .pem file for development, else Secrets Manager via the
# secret ARN Terraform sets as an env var (this function's execution role MUST NOT have
# access to route-model's or invoke-llm's secrets — see infra/iam_post_comment.tf).
GITHUB_APP_PRIVATE_KEY_PATH_ENV = "GITHUB_APP_PRIVATE_KEY_PATH"
GITHUB_APP_PRIVATE_KEY_SECRET_ARN_ENV = "GITHUB_APP_PRIVATE_KEY_SECRET_ARN"


def _default_github_client() -> GitHubClient:
    auth = GitHubAppAuth(
        app_id=require_env(GITHUB_APP_ID_ENV),
        installation_id=require_env(GITHUB_APP_INSTALLATION_ID_ENV),
        private_key_pem=resolve_secret_file(
            GITHUB_APP_PRIVATE_KEY_PATH_ENV, GITHUB_APP_PRIVATE_KEY_SECRET_ARN_ENV
        ),
    )
    # Cached across warm invocations until ~5 min before its 1-hour expiry.
    return RestGitHubClient(token=auth.installation_token())


def post_comment(event: dict, github_client: GitHubClient | None = None) -> dict:
    # Re-validating here enforces FR-008 defensively: GeneratedReview's own validation rejects
    # an empty/malformed summary, so this MUST NOT construct/post a review for one.
    # Read from "analysis" — the ResultPath key codereview-infra's ASL nests InvokeLLM's
    # output under (confirmed against statemachine/definition.asl.json.tpl).
    review = GeneratedReview.model_validate(event["analysis"])
    repository = event["repository"]
    # The review is posted against the exact commit InvokeLLM reviewed. "sha" is an original
    # PullRequestEvent field, still present at the top level of the accumulated event (Step
    # Functions ResultPath only adds nested keys — it never removes earlier ones).
    sha = event["sha"]
    github_client = github_client or _default_github_client()

    # A posting failure MUST be visible (FR-007) — left uncaught so the Lambda invocation fails
    # rather than silently discarding the error. The GitHubClient itself already falls back
    # from a rejected inline review to a plain comment before raising.
    comment = github_client.post_review(
        repository=repository,
        pr_id=review.pr_id,
        sha=sha,
        summary=review.summary,
        comments=review.comments,
    )
    return comment.model_dump()


def handler(event: dict, context=None) -> dict:
    return post_comment(event)
