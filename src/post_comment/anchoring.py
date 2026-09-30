"""Checks each comment's line against the diff before the review is posted.

GitHub anchors a comment to whatever line it is given, as long as that line is in the diff, so
a line number that is off by a few lines produces a comment on the wrong line rather than an
error (PR #21: the model said 26 and 59 for problems on 29 and 68). For each comment:

1. `code_snippet` matches the text at `line` on the RIGHT side: posted as is.
2. `code_snippet` is elsewhere in that file's part of the diff: moved to the matching line
   closest to `line` (RIGHT side first; a removed line on the LEFT side only when no RIGHT
   line matches). Logged as `line_adjusted`.
3. No `code_snippet`: posted as is if `line` is on the RIGHT side of the diff.
4. Anything else: not anchored. The comment goes into the review's body instead of onto a
   line it may not be about. Logged as `comment_unanchored`.

The log lines carry the path and line numbers only, never the comment's or the snippet's text.
"""

import json
import logging
import re
from dataclasses import dataclass

from contracts.models import ReviewCommentDraft
from integrations.diff_lines import FileLines, parse_diff

logger = logging.getLogger(__name__)

# A snippet shorter than this (after whitespace is collapsed) only counts as an exact match of
# a whole line, never as part of one: `}` or `return x;` would match too many lines.
MIN_PARTIAL_SNIPPET = 12

# What the model may copy along with the line's text: the `  29| ` prefix build_prompt's diff
# carries, and the diff's +/- marker.
_NUMBER_PREFIX = re.compile(r"^\s*\d*\| (?=[+\- ])")  # not `|| cond`, a Java continuation
_DIFF_MARKER = re.compile(r"^[+-](?=\s)")


@dataclass(frozen=True)
class Anchoring:
    anchored: list[ReviewCommentDraft]
    unanchored: list[ReviewCommentDraft]


def _normalize(text: str) -> str:
    return " ".join(text.split())


def _clean_snippet(snippet: str) -> str:
    # Only the first non-blank line: the prompt asks for one line, but a model sometimes
    # returns the whole statement.
    first = next((line for line in snippet.splitlines() if line.strip()), "")
    first = _NUMBER_PREFIX.sub("", first, count=1)
    return _normalize(_DIFF_MARKER.sub("", first, count=1))


def _is_on(text: str, snippet: str, partial: bool) -> bool:
    text = _normalize(text)
    return text == snippet or (partial and len(snippet) >= MIN_PARTIAL_SNIPPET and snippet in text)


def _matches(table: dict[int, str], snippet: str) -> list[int]:
    """Lines equal to the snippet; failing that, lines containing it."""
    for partial in (False, True):
        if found := [n for n, text in table.items() if _is_on(text, snippet, partial)]:
            return found
    return []


def _resolve(comment: ReviewCommentDraft, lines: FileLines) -> tuple[int, str] | str:
    """(line, side) to post at, or the reason the comment can't be anchored."""
    snippet = _clean_snippet(comment.code_snippet or "")
    if not snippet:
        return (comment.line, "RIGHT") if comment.line in lines.right else "line_not_in_diff"
    if comment.line in lines.right and _is_on(lines.right[comment.line], snippet, partial=True):
        return comment.line, "RIGHT"
    for side, table in (("RIGHT", lines.right), ("LEFT", lines.left)):
        if candidates := _matches(table, snippet):
            # Closest to what the model said; on a tie, the earlier line.
            return min(candidates, key=lambda n: (abs(n - comment.line), n)), side
    return "snippet_not_found"


def anchor_comments(
    comments: list[ReviewCommentDraft], diff_text: str, pr: int | str
) -> Anchoring:
    files = parse_diff(diff_text)
    anchored: list[ReviewCommentDraft] = []
    unanchored: list[ReviewCommentDraft] = []
    for comment in comments:
        lines = files.get(comment.path)
        resolved = _resolve(comment, lines) if lines else "path_not_in_diff"
        if isinstance(resolved, str):
            _log("comment_unanchored", pr, comment, reason=resolved)
            unanchored.append(comment)
            continue
        line, side = resolved
        if (line, side) != (comment.line, "RIGHT"):
            _log("line_adjusted", pr, comment, line=line, side=side)
        anchored.append(comment.model_copy(update={"line": line, "side": side}))
    return Anchoring(anchored=anchored, unanchored=unanchored)


def _log(event: str, pr: int | str, comment: ReviewCommentDraft, **fields) -> None:
    logger.info(
        json.dumps(
            {
                "event": event,
                "pr": pr,
                "path": comment.path,
                "original_line": comment.line,
                **fields,
                "has_snippet": comment.code_snippet is not None,
            }
        )
    )
