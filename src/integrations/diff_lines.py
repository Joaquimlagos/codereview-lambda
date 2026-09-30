"""Line numbers of a unified diff, shared by InvokeLLM and PostComment.

InvokeLLM numbers every line of the diff it sends to the model (`annotate_diff`), so the
model copies a line number instead of counting it from the hunk header — counting was the
cause of PR #21's comments landing a few lines above the problem (3 and 9 lines off).
PostComment checks each comment's line against the same map (`parse_diff`) before posting.

Both walk hunks by their header counts, not by a line's first character alone, so a removed
line whose text starts with `-- ` is never mistaken for the next file's `--- ` header.
"""

import re
from dataclasses import dataclass, field

_HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


@dataclass
class FileLines:
    """One file's lines in the diff, without the `+`/`-`/space marker."""

    # Post-change line number -> text, for every context and added line (GitHub's RIGHT side).
    right: dict[int, str] = field(default_factory=dict)
    # Pre-change line number -> text, for removed lines only (GitHub's LEFT side).
    left: dict[int, str] = field(default_factory=dict)


def _header_path(line: str) -> str | None:
    """The path on a `--- a/x` / `+++ b/x` line, or None for /dev/null."""
    target = line[4:].rstrip("\r\n").split("\t")[0]
    if target == "/dev/null":
        return None
    return target[2:] if target[:2] in ("a/", "b/") else target


def _walk(diff_text: str):
    """Yield (kind, line, path, old_number, new_number) for every line of the diff.

    kind is "header" (anything outside a hunk, `@@` lines included), "context", "added",
    "removed" or "note" (`\\ No newline at end of file`). Numbers are None where they don't
    apply: a removed line has no new number, an added line no old one."""
    path: str | None = None
    old_path: str | None = None
    old_left = new_left = 0
    old_n = new_n = 0
    for line in diff_text.splitlines():
        if old_left > 0 or new_left > 0:
            marker = line[:1]
            if marker == "\\":
                yield "note", line, path, None, None
                continue
            if marker == "-":
                yield "removed", line, path, old_n, None
                old_n += 1
                old_left -= 1
                continue
            if marker == "+":
                yield "added", line, path, None, new_n
                new_n += 1
                new_left -= 1
                continue
            if marker == " " or line == "":
                yield "context", line, path, old_n, new_n
                old_n += 1
                new_n += 1
                old_left -= 1
                new_left -= 1
                continue
            old_left = new_left = 0  # malformed hunk: fall through and treat it as a header

        if line.startswith("--- "):
            old_path = _header_path(line)
        elif line.startswith("+++ "):
            # A deleted file has only its old path; everything else is keyed by the new one.
            path = _header_path(line) or old_path
        elif match := _HUNK_HEADER.match(line):
            old_n, new_n = int(match[1]), int(match[3])
            old_left = int(match[2]) if match[2] is not None else 1
            new_left = int(match[4]) if match[4] is not None else 1
        yield "header", line, path, None, None


def parse_diff(diff_text: str) -> dict[str, FileLines]:
    """Map each file path in the diff (the post-change path) to its numbered lines."""
    files: dict[str, FileLines] = {}
    for kind, line, path, old_n, new_n in _walk(diff_text):
        if path is None or kind in ("header", "note"):
            continue
        lines = files.setdefault(path, FileLines())
        if kind == "removed":
            lines.left[old_n] = line[1:]
        else:
            lines.right[new_n] = line[1:]
    return files


def annotate_diff(diff_text: str) -> str:
    """The diff with each hunk line prefixed by its post-change line number and `| `.

    Removed lines get a blank number, since they don't exist after the change. Headers
    (`diff --git`, `---`/`+++`, `@@`) are kept as they are.

        @@ -22,9 +24,15 @@ public class TaskService {
          26|
          27| +    public List<Task> findOverdue(LocalDate today) {
            | -        Task created = new Task(id, task.title());
    """
    width = 4
    out = []
    for kind, line, _path, _old_n, new_n in _walk(diff_text):
        if kind == "header":
            out.append(line)
        elif new_n is None:
            out.append(f"{'':>{width}}| {line}")
        else:
            out.append(f"{new_n:>{width}}| {line}")
    return "\n".join(out) + ("\n" if diff_text.endswith("\n") else "")
