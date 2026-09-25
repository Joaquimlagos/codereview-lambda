# Data Model: AI PR Review Pipeline

Phase 1 output for [plan.md](./plan.md). Entities are drawn from the spec's Key Entities section;
fields reflect the functional requirements they exist to satisfy. These are the shapes implemented
as `pydantic` models in `src/contracts/models.py` (see [research.md](./research.md)).

## Pull Request Event

The trigger for a pipeline run — the real event published by `codereview-app`, camelCase on the
wire (`src/contracts/models.py`'s `alias_generator`). Carries lightweight metadata, the diff's
S3 location by reference (never the diff content itself, FR-013), and the lightweight diff
stats `RouteModel` sends to Jev (files_changed/lines_added/lines_removed/paths) — no field on
this event requires reading the diff body.

| Field | Type | Notes |
|---|---|---|
| `pr_number` | int | Stable identifier for the pull request (wire alias `prNumber`) |
| `repository` | string | Repository the PR belongs to |
| `sha` | string | Commit SHA the run applies to |
| `diff_bucket` | string | S3 bucket holding the full diff (wire alias `diffBucket`) |
| `diff_key` | string | S3 key within `diff_bucket` resolving to the full diff content (wire alias `diffKey`) |
| `files_changed` | int | Count of files touched by the diff (wire alias `filesChanged`) |
| `lines_added` | int | Count of added lines (wire alias `linesAdded`) |
| `lines_removed` | int | Count of removed lines (wire alias `linesRemoved`) |
| `paths` | list of string | File paths touched by the diff |

**Validation**: all fields required; string fields non-empty. `diff_bucket` + `diff_key` together
MUST resolve to a storage object, not inline diff text.

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
| `chunks` | list of Context Chunk | The retrieved project material, most relevant first; MAY be empty when nothing relevant was found (spec Edge Case). Carried inline rather than by reference (see below) |
| `index_available` | boolean | `false` when the RAG index does not exist yet, which is a graceful degradation rather than a failure; always paired with an empty `chunks` |

Each Context Chunk is `{ path, text }`: the indexed file's repository path and its content, both
non-empty. The chunks travel **inline** in the Step Functions payload rather than as an S3
reference: `TOP_K = 3` whole files stay well inside the state payload limit, and `InvokeLLM` then
needs no S3 read of its own for context (its IAM role is scoped to `prs/*` only — see
`infra/iam_invoke_llm.tf`).

**Validation**: only produced/consumed when the Routing Decision's `needsContext` is true; an
empty `chunks` list is valid and MUST NOT block review generation (spec Edge Case). Before any
chunk is scored, `RetrieveContext` verifies the index's `model` and `dimensions` against its own
embedding client and fails the run on a mismatch — vectors from a different model, or truncated to
a different length, would produce a meaningless ranking rather than a wrong-but-detectable one.

## Generated Review

Output of `InvokeLLM`; consumed by `PostComment`.

| Field | Type | Notes |
|---|---|---|
| `pr_id` | string | Correlates back to the originating Pull Request Event |
| `summary` | string | 2-3 sentence overview of the change, becomes the review's top-level body |
| `comments` | list of Review Comment Draft | Inline, diff-line-anchored observations; MAY be empty — a PR with nothing line-specific to flag is a valid, complete review (spec Edge Case), not an error |
| `model_used` | string | The model that actually generated it (Principle III traceability). With per-tier fallback lists this may not be the tier's first choice |
| `fell_back` | boolean | `true` when the tier's first-choice model failed transiently and a later entry in its `LLM_MODELS_{tier}` list generated the review. Makes the fallback rate observable in the Step Functions output |
| `parse_fallback` | boolean | `true` when Gemini's raw response could not be parsed as the instructed structured-JSON shape, so the whole raw response was used as `summary` with `comments` forced empty. Not an error by itself, but a persistently `true` value signals the prompt needs adjustment |

Each Review Comment Draft is `{ path, line, body }`: `path` is the file exactly as it appears
in the diff, `line` is the line number on the file's state *after* the change (the diff's "+"
side) — the side GitHub's Reviews API expects paired with `side: "RIGHT"` — and `body` is the
observation text. `InvokeLLM` instructs Gemini to only reference lines that actually appear in
the diff, but `PostComment`/GitHub is what actually enforces this (see Review Comment below).

**Validation**: `summary` MUST be non-empty and non-malformed; if generation produces no usable
output at all (no candidates, or a genuinely empty response), the pipeline MUST NOT construct a
Review Comment from it (FR-008, spec Edge Case) — this is distinct from a malformed-but-present
response, which degrades to `parse_fallback: true` instead of failing the run.

**Payload size**: `comments` is carried inline (like Retrieved Context's `chunks`), not by
reference. Step Functions state I/O is capped at 256 KB; a PR large enough to generate very
many long inline comments could in principle approach that, though no fixture or scenario here
has come close. Flagged as a known, unmitigated risk rather than solved — there is no
truncation/capping logic on `comments` today.

## Review Comment

The posted, user-visible artifact; the terminal output of `PostComment`.

| Field | Type | Notes |
|---|---|---|
| `pr_id` | string | The pull request the review/comment was posted to |
| `comment_id` | string | Identifier GitHub returned for whatever was actually posted — the review's id on success, or the fallback plain comment's id if GitHub rejected the review (see below) |
| `posted` | boolean | `true` only on confirmed successful post; a failed post MUST surface visibly (FR-007, spec Edge Case) rather than being silently discarded |

**Fallback behavior**: `PostComment` posts `summary` + `comments` as one advisory GitHub PR
review (`event: "COMMENT"` — never approves or requests changes, per the AI validation job's
non-blocking status). GitHub rejects the *entire* review with HTTP 422 if any comment's `line`
isn't part of the diff; when that happens, `PostComment` falls back to a single plain
conversational comment (`summary` + each comment's `path:line — body` concatenated as text)
instead of losing the review outright.

**Validation**: never constructed when `Generated Review.summary` is empty/malformed
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
