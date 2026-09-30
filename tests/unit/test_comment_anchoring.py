"""Line anchoring of inline comments, with PR #21 as the fixture.

`pr21.diff` is the diff the pipeline reviewed for codereview-app PR #21 (S3
prs/21/742e29be….diff) and `pr21_invoke_llm_output.json` is InvokeLLM's real output for it
(Step Functions execution of 2026-09-29 19:21 UTC). The model put both comments a few lines
above the problem: 26 for the NPE on 29, 59 for the division by zero on 68.
"""

import json
from pathlib import Path

import pytest

from contracts.models import GeneratedReview, ReviewCommentDraft
from integrations.diff_lines import annotate_diff, parse_diff
from integrations.github import RestGitHubClient
from integrations.llm_router import build_prompt, parse_review_response
from integrations.storage import StubStorage
from post_comment.anchoring import anchor_comments
from post_comment.handler import post_comment

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
TASKS = "src/main/java/com/codereview/app/tasks/"
SERVICE = TASKS + "TaskService.java"
STATS = TASKS + "TaskStats.java"
SERVICE_TEST = "src/test/java/com/codereview/app/tasks/TaskServiceTest.java"

NPE_LINE = ".filter(task -> !task.completed() && !task.dueDate().isAfter(today))"
DIV_LINE = "long completionPercentage = completed * 100 / total;"


@pytest.fixture
def pr21_diff() -> str:
    return (FIXTURES / "pr21.diff").read_text(encoding="utf-8")


@pytest.fixture
def pr21_review() -> GeneratedReview:
    return GeneratedReview.model_validate(
        json.loads((FIXTURES / "pr21_invoke_llm_output.json").read_text(encoding="utf-8"))
    )


def _draft(line, snippet=None, path=SERVICE) -> ReviewCommentDraft:
    return ReviewCommentDraft(
        path=path, line=line, code_snippet=snippet, body="Problem.", category="bug",
        severity="high",
    )


def _events(caplog, name):
    return [json.loads(r.getMessage()) for r in caplog.records
            if r.getMessage().startswith(f'{{"event": "{name}"')]


# --- the diff's line map -----------------------------------------------------------------


def test_parse_diff_numbers_pr21_bug_lines_on_the_right_side(pr21_diff):
    service = parse_diff(pr21_diff)[SERVICE]

    assert service.right[29].strip() == NPE_LINE
    assert service.right[68].strip() == DIV_LINE
    # Where the model put them: the blank line before findOverdue, and completeAll's `}`.
    assert service.right[26].strip() == ""
    assert service.right[59].strip() == "}"


def test_parse_diff_keeps_removed_lines_on_the_left_side_only(pr21_diff):
    service = parse_diff(pr21_diff)[SERVICE]

    args = "(id, task.title(), task.description(), task.completed());"
    assert service.left == {
        27: f"        Task created = new Task{args}",
        36: f"        Task updated = new Task{args}",
    }
    assert service.right[35].endswith("task.completed(), task.dueDate());")


def test_parse_diff_numbers_a_new_file_from_one(pr21_diff):
    stats = parse_diff(pr21_diff)[STATS]

    assert sorted(stats.right) == [1, 2, 3, 4] and stats.left == {}
    assert stats.right[3].startswith("public record TaskStats(")


def test_parse_diff_does_not_read_a_removed_dash_dash_line_as_a_file_header():
    diff = (
        "--- a/q.sql\n+++ b/q.sql\n"
        "@@ -1,2 +1,2 @@\n"
        "--- old comment\n"
        "+-- new comment\n"
        " select 1;\n"
    )
    lines = parse_diff(diff)["q.sql"]

    assert lines.left == {1: "-- old comment"}
    assert lines.right == {1: "-- new comment", 2: "select 1;"}


def test_parse_diff_keys_a_deleted_file_by_its_old_path():
    diff = "--- a/gone.txt\n+++ /dev/null\n@@ -1 +0,0 @@\n-bye\n"

    assert parse_diff(diff)["gone.txt"].left == {1: "bye"}


# --- what the model now reads --------------------------------------------------------------


def test_annotated_diff_prints_the_right_number_in_front_of_each_pr21_bug_line(pr21_diff):
    annotated = annotate_diff(pr21_diff).splitlines()

    assert f"  29| +                {NPE_LINE}" in annotated
    assert f"  68| +        {DIV_LINE}" in annotated
    # Removed lines carry no number; headers are untouched.
    assert "    | -        Task created = new Task(id, task.title(), task.description(), " \
           "task.completed());" in annotated
    assert "@@ -22,9 +24,15 @@ public class TaskService {" in annotated
    assert "+++ b/src/main/java/com/codereview/app/tasks/TaskService.java" in annotated


def test_annotated_diff_numbers_a_new_file(pr21_diff):
    annotated = annotate_diff(pr21_diff)

    assert "   1| +package com.codereview.app.tasks;" in annotated
    assert "   4| +}" in annotated


def test_prompt_carries_the_numbered_diff_and_asks_for_code_snippet(pr21_diff):
    prompt = build_prompt(pr21_diff)

    assert f"  29| +                {NPE_LINE}" in prompt
    assert '"code_snippet": "<the exact text of that line>"' in prompt
    assert "do not work it out from the hunk header" in prompt


def test_code_snippet_is_parsed_and_optional():
    raw = json.dumps({"summary": "S.", "comments": [
        {"path": SERVICE, "line": 29, "code_snippet": NPE_LINE, "body": "B.",
         "category": "bug", "severity": "high"},
        {"path": SERVICE, "line": 68, "body": "B.", "category": "bug", "severity": "high"},
        {"path": SERVICE, "line": 68, "code_snippet": 7, "body": "B.", "category": "bug",
         "severity": "high"},
    ]})

    review = parse_review_response(raw, pr_id="21", model_used="m")

    assert [c.code_snippet for c in review.comments] == [NPE_LINE, None, None]
    assert all(c.side == "RIGHT" for c in review.comments)


# --- anchoring: PR #21 --------------------------------------------------------------------


def test_pr21_comments_move_to_the_line_their_snippet_is_on(pr21_diff, pr21_review, caplog):
    """The real model output, plus the snippet the new prompt asks for: 26 -> 29, 59 -> 68."""
    npe, div = pr21_review.comments
    comments = [
        npe.model_copy(update={"code_snippet": NPE_LINE}),
        div.model_copy(update={"code_snippet": DIV_LINE}),
    ]

    with caplog.at_level("INFO"):
        result = anchor_comments(comments, pr21_diff, pr=21)

    assert [(c.line, c.side) for c in result.anchored] == [(29, "RIGHT"), (68, "RIGHT")]
    assert result.unanchored == []
    assert _events(caplog, "line_adjusted") == [
        {"event": "line_adjusted", "pr": 21, "path": SERVICE, "original_line": 26, "line": 29,
         "side": "RIGHT", "has_snippet": True},
        {"event": "line_adjusted", "pr": 21, "path": SERVICE, "original_line": 59, "line": 68,
         "side": "RIGHT", "has_snippet": True},
    ]


def test_pr21_output_without_snippets_is_kept_because_26_and_59_are_in_the_diff(
    pr21_diff, pr21_review, caplog
):
    """Why `code_snippet` is needed: without it the wrong lines are indistinguishable from
    right ones, since both are in the diff. The numbered diff is what fixes these at the
    source; this only documents what PostComment can and can't check on its own."""
    with caplog.at_level("INFO"):
        result = anchor_comments(pr21_review.comments, pr21_diff, pr=21)

    assert [c.line for c in result.anchored] == [26, 59]
    assert _events(caplog, "line_adjusted") == []


def test_a_snippet_that_matches_the_reported_line_is_left_alone(pr21_diff, caplog):
    with caplog.at_level("INFO"):
        result = anchor_comments([_draft(29, NPE_LINE), _draft(68, f"  {DIV_LINE}")], pr21_diff, 21)

    assert [c.line for c in result.anchored] == [29, 68]
    assert _events(caplog, "line_adjusted") == []


def test_snippet_copied_with_the_number_prefix_and_marker_still_matches(pr21_diff):
    result = anchor_comments([_draft(26, f"  29| +                {NPE_LINE}")], pr21_diff, 21)

    assert [c.line for c in result.anchored] == [29]


def test_a_multi_line_snippet_is_matched_on_its_first_line(pr21_diff):
    snippet = f"{DIV_LINE}\n        return new TaskStats(total, completed, pending, overdue, " \
              "completionPercentage);"

    assert [c.line for c in anchor_comments([_draft(60, snippet)], pr21_diff, 21).anchored] == [68]


# --- anchoring: edge cases -----------------------------------------------------------------


def test_line_outside_the_diff_without_snippet_goes_to_the_body(pr21_diff, caplog):
    with caplog.at_level("INFO"):
        result = anchor_comments([_draft(200)], pr21_diff, 21)

    assert result.anchored == [] and [c.line for c in result.unanchored] == [200]
    [event] = _events(caplog, "comment_unanchored")
    assert event["reason"] == "line_not_in_diff" and event["original_line"] == 200


def test_line_outside_the_diff_with_a_findable_snippet_is_moved(pr21_diff):
    result = anchor_comments([_draft(200, DIV_LINE)], pr21_diff, 21)

    assert [c.line for c in result.anchored] == [68]


def test_snippet_not_in_the_diff_is_not_anchored_even_if_the_line_is(pr21_diff, caplog):
    with caplog.at_level("INFO"):
        result = anchor_comments([_draft(29, "return tasks.size() / 0;")], pr21_diff, 21)

    assert result.anchored == [] and len(result.unanchored) == 1
    assert _events(caplog, "comment_unanchored")[0]["reason"] == "snippet_not_found"


def test_path_not_in_the_diff_is_not_anchored(pr21_diff, caplog):
    with caplog.at_level("INFO"):
        result = anchor_comments([_draft(3, "x", path="src/Other.java")], pr21_diff, 21)

    assert result.anchored == []
    assert _events(caplog, "comment_unanchored")[0]["reason"] == "path_not_in_diff"


def test_snippet_on_several_lines_picks_the_one_closest_to_the_reported_line(pr21_diff):
    snippet = 'Task created = taskService.create(new Task(null, "Title", "desc", false, ' \
              "TODAY.plusDays(3)));"
    right = parse_diff(pr21_diff)[SERVICE_TEST].right
    same = [n for n, text in right.items() if text.strip() == snippet]
    assert len(same) == 2  # the fixture really has this line twice

    first, second = same
    near_second = [_draft(second - 2, snippet, path=SERVICE_TEST)]
    near_first = [_draft(first + 1, snippet, path=SERVICE_TEST)]

    assert anchor_comments(near_second, pr21_diff, 21).anchored[0].line == second
    assert anchor_comments(near_first, pr21_diff, 21).anchored[0].line == first


def test_partial_snippet_in_several_lines_picks_the_closest(pr21_diff):
    # `!task.completed()` is on 29 (findOverdue), 64 and 66 (getStats).
    def anchored_at(line):
        return anchor_comments([_draft(line, "!task.completed() &&")], pr21_diff, 21).anchored

    assert anchored_at(27)[0].line == 29
    assert anchored_at(67)[0].line == 66


def test_short_snippet_only_matches_whole_lines(pr21_diff):
    # `total` is inside several lines, but no line is exactly `total`.
    result = anchor_comments([_draft(40, "total")], pr21_diff, 21)

    assert result.anchored == [] and len(result.unanchored) == 1


def test_comment_on_a_new_file(pr21_diff, caplog):
    snippet = "public record TaskStats(long total, long completed, long pending, long overdue, " \
              "long completionPercentage) {"

    with caplog.at_level("INFO"):
        result = anchor_comments([_draft(3, snippet, path=STATS), _draft(1, path=STATS),
                                  _draft(9, path=STATS)], pr21_diff, 21)

    assert [(c.line, c.side) for c in result.anchored] == [(3, "RIGHT"), (1, "RIGHT")]
    assert [c.line for c in result.unanchored] == [9]  # the file has 4 lines


def test_snippet_of_a_removed_line_anchors_on_the_left_side(pr21_diff, caplog):
    removed = "Task created = new Task(id, task.title(), task.description(), task.completed());"

    with caplog.at_level("INFO"):
        result = anchor_comments([_draft(35, removed)], pr21_diff, 21)

    assert [(c.line, c.side) for c in result.anchored] == [(27, "LEFT")]
    [event] = _events(caplog, "line_adjusted")
    assert event["side"] == "LEFT" and event["original_line"] == 35 and event["line"] == 27


def test_anchoring_logs_never_contain_the_comment_or_snippet_text(pr21_diff, caplog):
    with caplog.at_level("INFO"):
        anchor_comments([_draft(26, NPE_LINE), _draft(29, "not there at all")], pr21_diff, 21)

    lines = [r.getMessage() for r in caplog.records]
    assert len(lines) == 2
    assert not any(NPE_LINE in line or "not there" in line or "Problem." in line for line in lines)


# --- PostComment end to end, with the real GitHub payload ----------------------------------


class FakeResponse:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body

    def raise_for_status(self):
        pass

    def json(self):
        return self._body


class FakeSession:
    def __init__(self):
        self.requests = []

    def post(self, url, headers=None, json=None, timeout=None):
        self.requests.append({"url": url, "json": json})
        return FakeResponse(201, {"id": 1})


def test_post_comment_sends_pr21_comments_on_29_and_68_and_unanchored_ones_in_the_body(
    pr21_diff, pr21_review
):
    npe, div = pr21_review.comments
    analysis = pr21_review.model_copy(update={"comments": [
        npe.model_copy(update={"code_snippet": NPE_LINE}),
        div.model_copy(update={"code_snippet": DIV_LINE}),
        _draft(500, "nowhere in this diff"),
    ]}).model_dump()
    event = {
        "prNumber": 21, "repository": "Joaquimlagos/codereview-app",
        "sha": "742e29be090b32b93a33dfa05db066d42ca79ab4",
        "diffBucket": "codereview-artifacts", "diffKey": "prs/21/742e29be.diff",
        "analysis": analysis,
    }
    session = FakeSession()

    post_comment(event, github_client=RestGitHubClient(token="t", session=session),
                 storage=StubStorage({"prs/21/742e29be.diff": pr21_diff}))

    [request] = session.requests
    payload = request["json"]
    assert [(c["path"], c["line"], c["side"]) for c in payload["comments"]] == [
        (SERVICE, 29, "RIGHT"), (SERVICE, 68, "RIGHT"),
    ]
    assert payload["body"].startswith(pr21_review.summary)
    assert f"**{SERVICE}** — **[bug · high]** Problem." in payload["body"]


def test_post_comment_does_not_read_the_diff_for_a_summary_only_review():
    event = {
        "prNumber": 21, "repository": "o/r", "sha": "s", "diffBucket": "b", "diffKey": "missing",
        "analysis": {"pr_id": "21", "summary": "Nothing to flag.", "comments": [],
                     "model_used": "m"},
    }
    session = FakeSession()

    post_comment(event, github_client=RestGitHubClient(token="t", session=session),
                 storage=StubStorage({}))

    assert session.requests[0]["json"]["comments"] == []
