"""Shared pydantic models for the codereview-infra Step Functions I/O contract.

Field names and nesting are reconciled against codereview-infra's
statemachine/definition.asl.json.tpl and lambda_arns.tf — see
specs/001-pr-review-pipeline/contracts/step-io-contracts.md.

`RoutingDecision.needsContext` MUST keep this exact name: codereview-infra's ASL Choice
state branches on `$.routing.needsContext` literally. See
tests/contract/test_route_model_contract.py for a test that fails if this diverges again.
"""

from enum import StrEnum

from pydantic import BaseModel, Field


class Complexity(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class PullRequestEvent(BaseModel):
    pr_id: str = Field(min_length=1)
    repository: str = Field(min_length=1)
    revision: str = Field(min_length=1)
    diff_ref: str = Field(min_length=1)


class RoutingDecision(BaseModel):
    complexity: Complexity
    needsContext: bool


class RetrievedContext(BaseModel):
    pr_id: str = Field(min_length=1)
    context_ref: str = Field(min_length=1)
    sources: list[str] = Field(default_factory=list)


class GeneratedReview(BaseModel):
    pr_id: str = Field(min_length=1)
    review_text: str = Field(min_length=1)
    model_used: str = Field(min_length=1)


class ReviewComment(BaseModel):
    pr_id: str = Field(min_length=1)
    comment_id: str = Field(min_length=1)
    posted: bool
