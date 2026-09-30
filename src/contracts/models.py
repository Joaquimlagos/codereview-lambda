"""Shared pydantic models for the codereview-infra Step Functions I/O contract.

Field names and nesting are reconciled against codereview-infra's
statemachine/definition.asl.json.tpl and lambda_arns.tf — see
specs/001-pr-review-pipeline/contracts/step-io-contracts.md.

`RoutingDecision.needsContext` MUST keep this exact name: codereview-infra's ASL Choice
state branches on `$.routing.needsContext` literally. See
tests/contract/test_route_model_contract.py for a test that fails if this diverges again.
"""

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel


class Complexity(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class PullRequestEvent(BaseModel):
    """The real event published by codereview-app — camelCase on the wire. `alias_generator`
    derives each field's camelCase alias automatically (e.g. `files_changed` -> `filesChanged`);
    `populate_by_name=True` also still accepts the snake_case Python names directly, so
    internal code/tests can construct one either way.
    """

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    pr_number: int
    repository: str = Field(min_length=1)
    sha: str = Field(min_length=1)
    diff_bucket: str = Field(min_length=1)
    diff_key: str = Field(min_length=1)
    files_changed: int
    lines_added: int
    lines_removed: int
    paths: list[str]


class RoutingDecision(BaseModel):
    complexity: Complexity
    needsContext: bool


class ContextChunk(BaseModel):
    """One retrieved chunk of codereview-app's RAG index, carried inline to InvokeLLM.

    From a version 1 index, a chunk is a whole file and only `path`/`text` are set. From a
    version 2 index it is one method, type or block (specs/002-method-chunking/data-model.md)
    and also carries where it sits in the file, the header that makes it readable on its
    own, and how it was ranked. Every v2 field is optional, so v1 output still validates;
    RetrieveContext dumps with `exclude_none`, so a v1 chunk stays exactly `{path, text}`.
    """

    path: str = Field(min_length=1)
    # v1: the whole file. v2: the chunk body only — its header is carried separately so the
    # prompt can print it once per file rather than once per chunk.
    text: str = Field(min_length=1)
    id: str | None = None
    start_line: int | None = None
    end_line: int | None = None
    header: str | None = None
    # Best cosine similarity against any of the diff's per-file queries, and which changed
    # file that query came from.
    score: float | None = None
    matched_query: str | None = None


class RetrievedContext(BaseModel):
    pr_id: str = Field(min_length=1)
    # The top-ranked chunks; empty when the index could not be read.
    chunks: list[ContextChunk] = Field(default_factory=list)
    # False when index/develop/index.json is absent (e.g. no merge to develop yet): the run
    # still proceeds, InvokeLLM just reviews the diff without project context (FR-004's
    # "whatever context is available" Edge Case).
    index_available: bool = True
    # Which index format the chunks came from (1 or 2) — InvokeLLM lays the prompt out
    # differently for each. None only when no index was available.
    index_version: int | None = None


class CommentCategory(StrEnum):
    """What kind of real problem an inline comment is about — never praise or description,
    which belong in GeneratedReview.summary instead (research.md's "Review quality rubric")."""

    BUG = "bug"
    SECURITY = "security"
    PERFORMANCE = "performance"
    MAINTAINABILITY = "maintainability"


class CommentSeverity(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ReviewCommentDraft(BaseModel):
    """One inline observation the model tied to a specific line of the diff, before posting.

    `category` and `severity` are required, not optional metadata: build_prompt's rubric
    instructs the model that every entry in `comments` must be a real problem in one of the
    four categories, with a severity — never praise or a description of what the code does.
    A response missing either on some comment fails ReviewCommentDraft validation, which
    parse_review_response treats the same as any other shape mismatch (falls back to
    summary-only, `parse_fallback: true`), rather than silently accepting an unclassified
    comment.
    """

    path: str = Field(min_length=1)
    # Line number in the file AFTER the change (the diff's "+"/right side) — this is what
    # GitHub's Reviews API expects paired with `side: "RIGHT"` (see integrations/github.py),
    # unless PostComment moved the comment to a removed line (`side` below).
    line: int = Field(gt=0)
    body: str = Field(min_length=1)
    category: CommentCategory
    severity: CommentSeverity
    # The text of the line the model means (build_prompt asks for it). PostComment re-anchors
    # the comment to where this text actually is when `line` points somewhere else. Optional,
    # so a response without it still parses; the comment is then only checked against the
    # diff's line numbers.
    code_snippet: str | None = None
    # Set by PostComment, never by the model: LEFT only when `code_snippet` matched a removed
    # line, in which case `line` is that line's pre-change number.
    side: Literal["RIGHT", "LEFT"] = "RIGHT"


class GeneratedReview(BaseModel):
    pr_id: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    # Inline, line-anchored observations; MAY be empty — a PR with nothing line-specific to
    # flag is a valid, complete review (spec Edge Case), not an error.
    comments: list[ReviewCommentDraft] = Field(default_factory=list)
    # The model that actually produced this review — with per-tier fallback lists, not
    # necessarily the tier's first choice.
    model_used: str = Field(min_length=1)
    # True when the tier's first-choice model failed transiently or no longer exists and a
    # later entry in its LLM_MODELS_{tier} list produced this review
    # (integrations/llm_router.py). Carried through to the Step Functions output so how often
    # fallback happens is observable.
    fell_back: bool = False
    # True when the model's raw response could not be parsed as the structured JSON shape
    # build_prompt instructs (integrations/llm_router.py's parse_review_response): the whole
    # raw response was used as `summary` instead, with `comments` forced empty. Not itself an
    # error — the review still posts — but a persistently true value is a signal the prompt
    # needs adjustment, so it's carried through rather than only logged.
    parse_fallback: bool = False


class ReviewComment(BaseModel):
    pr_id: str = Field(min_length=1)
    comment_id: str = Field(min_length=1)
    posted: bool
