# Data Model: AI PR Review Pipeline

Phase 1 output for [plan.md](./plan.md). Entities are drawn from the spec's Key Entities section;
fields reflect the functional requirements they exist to satisfy. These are the shapes implemented
as `pydantic` models in `src/contracts/models.py` (see [research.md](./research.md)).

## Pull Request Event

The trigger for a pipeline run. Carries lightweight metadata and a reference to the full diff,
never the diff content itself (FR-013).

| Field | Type | Notes |
|---|---|---|
| `pr_id` | string | Stable identifier for the pull request |
| `repository` | string | Repository the PR belongs to |
| `revision` | string | Commit SHA / revision the run applies to (used for dedup, see research.md) |
| `diff_ref` | string | Storage key resolving to the full diff content (S3) |

**Validation**: all fields required and non-empty; `diff_ref` MUST be a resolvable storage key,
not inline diff text.

## Routing Decision

Output of `RouteModel`; consumed by `RetrieveContext` (conditionally) and `InvokeLLM`.

| Field | Type | Notes |
|---|---|---|
| `complexity` | enum: `low` \| `medium` \| `high` | Drives which Gemini model tier `InvokeLLM` selects |
| `needsContext` | boolean | Gates whether `RetrieveContext` runs (FR-004/FR-005). Named `needsContext` to match the exact field the `codereview-infra` ASL's `CheckNeedsContext` Choice state branches on (`$.routing.needsContext`) — this has been renamed upstream once already (was `needsRag`, and before that an assumed `context_needed`); see contracts/step-io-contracts.md's authoritative-source note |

**Validation**: `complexity` MUST be one of the three tiers — no free-text values. On decision
failure, `RouteModel` MUST substitute the documented fallback (`medium`, `false`) rather than
omit the fields (see research.md).

**State transitions**: produced once per run by `RouteModel`; immutable afterward — no downstream
stage re-derives or overwrites it (per spec Acceptance Scenario, User Story 2 #3).

## Retrieved Context

Produced only when `needsContext` is true; consumed by `InvokeLLM`.

| Field | Type | Notes |
|---|---|---|
| `pr_id` | string | Correlates back to the originating Pull Request Event |
| `context_ref` | string | Storage key resolving to the assembled context (S3), mirroring `diff_ref`'s by-reference pattern |
| `sources` | list of string | Identifiers/paths of the related project material the context was assembled from; MAY be empty when nothing relevant was found (spec Edge Case) |

**Validation**: only produced/consumed when the Routing Decision's `needsContext` is true; an
empty `sources` list is valid and MUST NOT block review generation (spec Edge Case).

## Generated Review

Output of `InvokeLLM`; consumed by `PostComment`.

| Field | Type | Notes |
|---|---|---|
| `pr_id` | string | Correlates back to the originating Pull Request Event |
| `review_text` | string | Natural-language review content |
| `model_used` | string | Which of the three free-tier Gemini models generated it (Principle III traceability) |

**Validation**: `review_text` MUST be non-empty and non-malformed; if generation produces no
usable output, the pipeline MUST NOT construct a Review Comment from it (FR-008, spec Edge Case).

## Review Comment

The posted, user-visible artifact; the terminal output of `PostComment`.

| Field | Type | Notes |
|---|---|---|
| `pr_id` | string | The pull request the comment was posted to |
| `comment_id` | string | Identifier returned by the GitHub API once posted |
| `posted` | boolean | `true` only on confirmed successful post; a failed post MUST surface visibly (FR-007, spec Edge Case) rather than being silently discarded |

**Validation**: never constructed when `Generated Review.review_text` is empty/malformed
(FR-008); a posting failure MUST be raised/flagged, not swallowed.

## Relationships & flow

```text
Pull Request Event
      │
      ▼
Routing Decision  ──(needsContext = false)──────────────────┐
      │                                                     │
      │ (needsContext = true)                               │
      ▼                                                     │
Retrieved Context                                            │
      │                                                     │
      └───────────────────────┬─────────────────────────────┘
                               ▼
                       Generated Review
                               │
                               ▼
                       Review Comment
```

One Routing Decision per run; Retrieved Context is optional and produced at most once per run;
exactly one Generated Review feeds exactly one Review Comment attempt per run (spec Assumption:
one comment per PR revision).
