"""GitHub PR-comment abstraction (used by PostComment)."""

from abc import ABC, abstractmethod

from contracts.models import ReviewComment


class GitHubClientError(Exception):
    """Raised when posting the comment fails (spec Edge Case) — MUST surface visibly (FR-007)."""


class GitHubClient(ABC):
    @abstractmethod
    def post_comment(self, repository: str, pr_id: str, review_text: str) -> ReviewComment:
        """Post `review_text` on `pr_id` in `repository`. Raises GitHubClientError on failure."""


class RestGitHubClient(GitHubClient):
    def __init__(self, token: str, session=None):
        import requests

        self._token = token
        self._session = session or requests.Session()

    def post_comment(self, repository: str, pr_id: str, review_text: str) -> ReviewComment:
        url = f"https://api.github.com/repos/{repository}/issues/{pr_id}/comments"
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.github+json",
        }
        try:
            response = self._session.post(
                url, headers=headers, json={"body": review_text}, timeout=15
            )
            response.raise_for_status()
            data = response.json()
            return ReviewComment(pr_id=pr_id, comment_id=str(data["id"]), posted=True)
        except Exception as exc:
            raise GitHubClientError(str(exc)) from exc


class StubGitHubClient(GitHubClient):
    """Network-free stand-in for tests (Principle IV, FR-011)."""

    def __init__(self):
        self.posted_comments: list[ReviewComment] = []
        self.fail: bool = False

    def post_comment(self, repository: str, pr_id: str, review_text: str) -> ReviewComment:
        if self.fail:
            raise GitHubClientError("stub configured to simulate GitHub API failure")
        comment = ReviewComment(
            pr_id=pr_id, comment_id=f"stub-comment-{len(self.posted_comments) + 1}", posted=True
        )
        self.posted_comments.append(comment)
        return comment
