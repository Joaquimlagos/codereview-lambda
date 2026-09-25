"""Shared pydantic models for the codereview-infra Step Functions I/O contract.

Field names and nesting are reconciled against codereview-infra's
statemachine/definition.asl.json.tpl and lambda_arns.tf — see
specs/001-pr-review-pipeline/contracts/step-io-contracts.md.

`RoutingDecision.needsContext` MUST keep this exact name: codereview-infra's ASL Choice
state branches on `$.routing.needsContext` literally. See
tests/contract/test_route_model_contract.py for a test that fails if this diverges again.
"""

from enum import StrEnum

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
    """One indexed file from codereview-app's RAG index, carried inline to InvokeLLM."""

    path: str = Field(min_length=1)
    text: str = Field(min_length=1)


class RetrievedContext(BaseModel):
    pr_id: str = Field(min_length=1)
    # The top-K most similar chunks; empty when the index could not be read.
    chunks: list[ContextChunk] = Field(default_factory=list)
    # False when index/develop/index.json is absent (e.g. no merge to develop yet): the run
    # still proceeds, InvokeLLM just reviews the diff without project context (FR-004's
    # "whatever context is available" Edge Case).
    index_available: bool = True


class ReviewCommentDraft(BaseModel):
    """One inline observation the model tied to a specific line of the diff, before posting."""

    path: str = Field(min_length=1)
    # Line number in the file AFTER the change (the diff's "+"/right side) — this is what
    # GitHub's Reviews API expects paired with `side: "RIGHT"` (see integrations/github.py).
    line: int = Field(gt=0)
    body: str = Field(min_length=1)


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
