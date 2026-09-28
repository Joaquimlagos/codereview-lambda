"""Unit tests for the GitHub client's comment rendering: category/severity must be visible
on the posted comment, since the Reviews API has no dedicated fields for them.
"""

from contracts.models import ReviewCommentDraft
from integrations.github import RestGitHubClient, _format_fallback_body, _render_comment_body


def _draft(**overrides) -> ReviewCommentDraft:
    defaults = {
        "path": "src/app.py",
        "line": 5,
        "body": "Password is logged in cleartext.",
        "category": "security",
        "severity": "high",
    }
    return ReviewCommentDraft(**{**defaults, **overrides})


def test_render_comment_body_prefixes_category_and_severity():
    rendered = _render_comment_body(_draft())

    assert rendered == "**[security · high]** Password is logged in cleartext."


def test_fallback_body_includes_category_and_severity_per_comment():
    body = _format_fallback_body("Summary.", [_draft(), _draft(category="bug", severity="low")])

    assert "**[security · high]** Password is logged in cleartext." in body
    assert "**[bug · low]** Password is logged in cleartext." in body


class FakeResponse:
    def __init__(self, status_code: int, body: dict):
        self.status_code = status_code
        self._body = body

    def raise_for_status(self):
        pass

    def json(self):
        return self._body


class FakeSession:
    def __init__(self, response: FakeResponse):
        self._response = response
        self.requests: list[dict] = []

    def post(self, url, headers, json, timeout):
        self.requests.append({"url": url, "headers": headers, "payload": json})
        return self._response


def test_post_review_sends_rendered_body_for_each_inline_comment():
    session = FakeSession(FakeResponse(200, {"id": 123}))
    client = RestGitHubClient(token="t", session=session)

    client.post_review(
        repository="octocat/example",
        pr_id="42",
        sha="abc123",
        summary="Overview.",
        comments=[_draft()],
    )

    payload = session.requests[0]["payload"]
    assert payload["comments"] == [
        {
            "path": "src/app.py",
            "line": 5,
            "side": "RIGHT",
            "body": "**[security · high]** Password is logged in cleartext.",
        }
    ]
