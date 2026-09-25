"""GitHub PR-review abstraction (used by PostComment).

Posts the generated review as inline, line-anchored comments via GitHub's Reviews API rather
than one undifferentiated issue comment, so each observation sits next to the diff line it's
about. See RestGitHubClient.post_review for the endpoint, event type, and 422 fallback.

Authenticates as a GitHub App installation (GitHubAppAuth), not a personal access token, so
reviews appear as the App's bot account.
"""

import time
from abc import ABC, abstractmethod
from datetime import datetime

from contracts.models import ReviewComment, ReviewCommentDraft

GITHUB_API = "https://api.github.com"

# The App JWT is backdated to absorb clock drift between Lambda and GitHub, and its expiry
# is set so the whole window (backdate + lifetime) is exactly GitHub's 10-minute maximum.
JWT_BACKDATE_SECONDS = 60
JWT_LIFETIME_SECONDS = 9 * 60

# Installation tokens live 1 hour; refresh this long before GitHub's stated expiry so a token
# is never used right as it lapses mid-request.
TOKEN_REFRESH_MARGIN_SECONDS = 5 * 60

# installation id -> (installation access token, expiry as epoch seconds). Module-level so a
# warm Lambda execution environment reuses the token across invocations.
_installation_token_cache: dict[str, tuple[str, float]] = {}


class GitHubClientError(Exception):
    """Raised when posting fails outright — including the fallback (spec Edge Case) — and
    MUST surface visibly (FR-007)."""


class GitHubAppAuth:
    """Authenticates as one installation of a GitHub App.

    Two steps: a short-lived JWT signed with the App's private key (RS256, `iss` = App ID)
    proves the caller is the App; GitHub exchanges it at
    POST /app/installations/{installation_id}/access_tokens for an installation access
    token, valid 1 hour and limited to the repositories and permissions granted to that
    installation. The installation token is what the review calls use, and it is cached
    until TOKEN_REFRESH_MARGIN_SECONDS before expiry.
    """

    def __init__(
        self,
        app_id: str,
        installation_id: str,
        private_key_pem: str,
        session=None,
        clock=time.time,
    ):
        import requests

        self._app_id = str(app_id)
        self._installation_id = str(installation_id)
        self._private_key_pem = private_key_pem
        self._session = session or requests.Session()
        self._clock = clock

    def app_jwt(self) -> str:
        import jwt

        now = int(self._clock())
        claims = {
            "iat": now - JWT_BACKDATE_SECONDS,
            "exp": now + JWT_LIFETIME_SECONDS,
            "iss": self._app_id,
        }
        return jwt.encode(claims, self._private_key_pem, algorithm="RS256")

    def installation_token(self) -> str:
        cached = _installation_token_cache.get(self._installation_id)
        if cached and self._clock() < cached[1] - TOKEN_REFRESH_MARGIN_SECONDS:
            return cached[0]

        url = f"{GITHUB_API}/app/installations/{self._installation_id}/access_tokens"
        headers = {
            "Authorization": f"Bearer {self.app_jwt()}",
            "Accept": "application/vnd.github+json",
        }
        try:
            response = self._session.post(url, headers=headers, timeout=15)
            response.raise_for_status()
            data = response.json()
            token = data["token"]
            expires_at = datetime.fromisoformat(data["expires_at"]).timestamp()
        except Exception as exc:
            raise GitHubClientError(
                f"GitHub App installation token exchange failed: {exc}"
            ) from exc

        _installation_token_cache[self._installation_id] = (token, expires_at)
        return token


def _format_fallback_body(summary: str, comments: list[ReviewCommentDraft]) -> str:
    """Summary + inline comments concatenated into one conversational-comment body — used
    when GitHub rejects the structured review outright (422 on an invalid line). A single
    generic comment beats losing the review entirely.
    """
    if not comments:
        return summary
    observations = "\n\n".join(f"**{c.path}:{c.line}** — {c.body}" for c in comments)
    return f"{summary}\n\n---\n{observations}"


class GitHubClient(ABC):
    @abstractmethod
    def post_review(
        self,
        repository: str,
        pr_id: str,
        sha: str,
        summary: str,
        comments: list[ReviewCommentDraft],
    ) -> ReviewComment:
        """Post `summary` (+ any inline `comments`) as a PR review (FR-007).

        Implementations MUST use an advisory review (GitHub's `event: "COMMENT"`) — never
        APPROVE/REQUEST_CHANGES — so the AI review stays non-blocking. GitHub rejects the
        WHOLE review with 422 if any comment's `line` isn't part of the diff; implementations
        MUST fall back to posting a single conversational comment (summary + comments
        concatenated as text, `_format_fallback_body`) in that case rather than losing the
        review outright. Raises GitHubClientError if both the review and the fallback fail.
        """

    @abstractmethod
    def post_comment(self, repository: str, pr_id: str, review_text: str) -> ReviewComment:
        """Post a single plain conversational comment. Used directly by the 422 fallback
        above, and kept as its own method so that fallback is itself testable in isolation.
        Raises GitHubClientError on failure.
        """


class RestGitHubClient(GitHubClient):
    def __init__(self, token: str, session=None):
        import requests

        self._token = token
        self._session = session or requests.Session()

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.github+json",
        }

    def post_review(
        self,
        repository: str,
        pr_id: str,
        sha: str,
        summary: str,
        comments: list[ReviewCommentDraft],
    ) -> ReviewComment:
        url = f"{GITHUB_API}/repos/{repository}/pulls/{pr_id}/reviews"
        payload = {
            "commit_id": sha,
            "body": summary,
            # Advisory only: the AI validation job is non-blocking, so this MUST NOT approve
            # or request changes on the PR.
            "event": "COMMENT",
            "comments": [
                {"path": c.path, "line": c.line, "side": "RIGHT", "body": c.body}
                for c in comments
            ],
        }
        try:
            response = self._session.post(url, headers=self._headers(), json=payload, timeout=15)
            if response.status_code == 422:
                # GitHub rejects the whole review when any comment's line isn't part of the
                # diff (e.g. the model referenced a stale/removed line despite build_prompt's
                # instructions). Degrade to one conversational comment instead of losing the
                # review entirely — see research.md for the decision.
                return self.post_comment(
                    repository=repository,
                    pr_id=pr_id,
                    review_text=_format_fallback_body(summary, comments),
                )
            response.raise_for_status()
            data = response.json()
            return ReviewComment(pr_id=pr_id, comment_id=str(data["id"]), posted=True)
        except GitHubClientError:
            raise
        except Exception as exc:
            raise GitHubClientError(str(exc)) from exc

    def post_comment(self, repository: str, pr_id: str, review_text: str) -> ReviewComment:
        url = f"{GITHUB_API}/repos/{repository}/issues/{pr_id}/comments"
        try:
            response = self._session.post(
                url, headers=self._headers(), json={"body": review_text}, timeout=15
            )
            response.raise_for_status()
            data = response.json()
            return ReviewComment(pr_id=pr_id, comment_id=str(data["id"]), posted=True)
        except Exception as exc:
            raise GitHubClientError(str(exc)) from exc


class StubGitHubClient(GitHubClient):
    """Network-free stand-in for tests (Principle IV, FR-011)."""

    def __init__(self):
        self.posted_reviews: list[ReviewComment] = []
        self.posted_comments: list[ReviewComment] = []
        self.fail: bool = False
        # Simulates GitHub's 422 rejection of a review with an out-of-diff comment line, so
        # tests can exercise the same conversational-comment fallback the real client falls
        # back to, without a real HTTP call.
        self.reject_inline_comments: bool = False

    def post_review(
        self,
        repository: str,
        pr_id: str,
        sha: str,
        summary: str,
        comments: list[ReviewCommentDraft],
    ) -> ReviewComment:
        if self.fail:
            raise GitHubClientError("stub configured to simulate GitHub API failure")
        if comments and self.reject_inline_comments:
            return self.post_comment(
                repository=repository,
                pr_id=pr_id,
                review_text=_format_fallback_body(summary, comments),
            )
        review = ReviewComment(
            pr_id=pr_id, comment_id=f"stub-review-{len(self.posted_reviews) + 1}", posted=True
        )
        self.posted_reviews.append(review)
        return review

    def post_comment(self, repository: str, pr_id: str, review_text: str) -> ReviewComment:
        if self.fail:
            raise GitHubClientError("stub configured to simulate GitHub API failure")
        comment = ReviewComment(
            pr_id=pr_id, comment_id=f"stub-comment-{len(self.posted_comments) + 1}", posted=True
        )
        self.posted_comments.append(comment)
        return comment
