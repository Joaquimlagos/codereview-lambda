"""Turns a PR diff into retrieval queries and into the set of lines it changes.

Two things RetrieveContext needs from the unified diff for a version 2 index
(specs/002-method-chunking, research R7 and R8):

- **Queries**: one per changed file, instead of one for the whole diff. The embedding model
  silently drops input beyond 2,048 tokens, and embedding the whole diff at once meant PR
  #8's retrieval was decided by its first 26% only (baseline.md). A file whose own diff is
  still too big is split at hunk boundaries, and a single oversized hunk by lines; every
  piece keeps the file's header lines so it still says which file it is about.
- **Changed lines**: the pre-change line numbers each hunk removes or inserts next to. The
  index mirrors `develop`, i.e. the pre-change side, so this is what a chunk's
  `startLine`/`endLine` is compared against to leave out code the diff already shows.
"""

import re
from dataclasses import dataclass

from contracts.token_estimate import (
    CHARS_PER_TOKEN,
    EMBEDDING_SPLIT_THRESHOLD_TOKENS,
    estimate_tokens,
)

_FILE_START = re.compile(r"^diff --git ", re.MULTILINE)
_HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_DEV_NULL = "/dev/null"


@dataclass(frozen=True)
class DiffQuery:
    """One retrieval query: a changed file's diff, or one part of it when it was split."""

    path: str
    text: str
    # 1-based position and total, set only when the file's diff was split.
    part: int | None = None
    parts: int | None = None


@dataclass(frozen=True)
class _FileSection:
    old_path: str | None  # None for a new file
    new_path: str | None  # None for a deleted file
    header: str  # everything before the first hunk: diff --git, index, ---/+++ lines
    hunks: list[str]  # each starts with its @@ line

    @property
    def query_path(self) -> str:
        return self.new_path or self.old_path or ""


def _strip_prefix(value: str) -> str | None:
    value = value.strip()
    if value == _DEV_NULL:
        return None
    return value[2:] if value[:2] in ("a/", "b/") else value


def _parse_section(text: str) -> _FileSection:
    lines = text.splitlines(keepends=True)
    first_hunk = next((i for i, line in enumerate(lines) if line.startswith("@@")), len(lines))
    header_lines, body = lines[:first_hunk], lines[first_hunk:]

    old_path = new_path = None
    git_line = header_lines[0] if header_lines else ""
    if match := re.match(r"diff --git a/(.+?) b/(.+)$", git_line.rstrip("\n")):
        old_path, new_path = match.group(1), match.group(2)
    # The ---/+++ lines are authoritative when present: they say /dev/null for added and
    # deleted files, which the `diff --git` line never does.
    for line in header_lines:
        if line.startswith("--- "):
            old_path = _strip_prefix(line[4:])
        elif line.startswith("+++ "):
            new_path = _strip_prefix(line[4:])

    hunks: list[str] = []
    for line in body:
        if line.startswith("@@") or not hunks:
            hunks.append(line)
        else:
            hunks[-1] += line
    return _FileSection(old_path, new_path, "".join(header_lines), hunks)


def _sections(diff: str) -> list[str]:
    starts = [m.start() for m in _FILE_START.finditer(diff)]
    if not starts:
        return []
    return [diff[a:b] for a, b in zip(starts, starts[1:] + [len(diff)], strict=True)]


def _fits(text: str) -> bool:
    return estimate_tokens(text) <= EMBEDDING_SPLIT_THRESHOLD_TOKENS


def _cut_long_line(prefix: str, line: str) -> list[str]:
    """Last resort for a single line that alone exceeds the threshold (a minified file, a
    long literal): cut it by characters so that no piece is ever silently truncated by
    the embedding API instead."""
    room = int(EMBEDDING_SPLIT_THRESHOLD_TOKENS * CHARS_PER_TOKEN) - len(prefix)
    if room <= 0 or _fits(prefix + line):
        return [line]
    return [line[i : i + room] for i in range(0, len(line), room)]


def _split_hunk(header: str, hunk: str) -> list[str]:
    """Split one oversized hunk by lines; every piece repeats the hunk's @@ line."""
    at_line, *rest = hunk.splitlines(keepends=True)
    rest = [piece for line in rest for piece in _cut_long_line(header + at_line, line)]
    pieces, current = [], header + at_line
    for line in rest:
        if not _fits(current + line) and current != header + at_line:
            pieces.append(current)
            current = header + at_line
        current += line
    pieces.append(current)
    return pieces


def _split_section(section: _FileSection) -> list[str]:
    """Pack whole hunks into pieces that fit, each starting with the file's header."""
    pieces, current = [], section.header
    for hunk in section.hunks:
        if _fits(current + hunk):
            current += hunk
            continue
        if current != section.header:
            pieces.append(current)
            current = section.header
        if _fits(current + hunk):
            current += hunk
        else:
            pieces.extend(_split_hunk(section.header, hunk))
    if current != section.header:
        pieces.append(current)
    return pieces


def split_queries(diff: str) -> list[DiffQuery]:
    """One query per changed file, in diff order; oversized files split into parts.

    Text that is not a git diff at all (no `diff --git` line) becomes a single query for the
    whole text, with an empty path, so retrieval degrades to the pre-002 behaviour instead
    of failing.
    """
    raw_sections = _sections(diff)
    if not raw_sections:
        return [DiffQuery(path="", text=diff)]

    queries: list[DiffQuery] = []
    for raw in raw_sections:
        section = _parse_section(raw)
        if _fits(raw):
            queries.append(DiffQuery(path=section.query_path, text=raw))
            continue
        pieces = _split_section(section)
        queries.extend(
            DiffQuery(path=section.query_path, text=piece, part=i, parts=len(pieces))
            for i, piece in enumerate(pieces, start=1)
        )
    return queries


def _hunk_changed_lines(hunk: str) -> set[int]:
    lines = hunk.splitlines()
    match = _HUNK_HEADER.match(lines[0]) if lines else None
    if not match:
        return set()
    old_start = int(match.group(1))
    old_count = int(match.group(2)) if match.group(2) is not None else 1
    if old_count == 0:
        # Zero-context insertion: `-a,0` means "inserted after old line a".
        return {line for line in (old_start, old_start + 1) if line >= 1}

    changed: set[int] = set()
    old_line = old_start
    previous = " "
    for line in lines[1:]:
        kind = line[:1]
        if kind == "\\":  # "\ No newline at end of file"
            continue
        if kind == "-":
            changed.add(old_line)
            old_line += 1
        elif kind == "+":
            if previous not in ("-", "+"):
                # Start of a pure insertion: it lands between old_line - 1 and old_line.
                changed.update(line for line in (old_line - 1, old_line) if line >= 1)
        else:
            old_line += 1
        previous = kind
    return changed


def changed_lines(diff: str) -> dict[str, set[int]]:
    """Old-side line numbers each file's hunks remove, plus both neighbours of every pure
    insertion (research R8). Context lines never count. New files are absent: nothing in the
    index can overlap them."""
    result: dict[str, set[int]] = {}
    for raw in _sections(diff):
        section = _parse_section(raw)
        if section.old_path is None:
            continue
        lines = set().union(*(_hunk_changed_lines(h) for h in section.hunks))
        if lines:
            result.setdefault(section.old_path, set()).update(lines)
    return result
