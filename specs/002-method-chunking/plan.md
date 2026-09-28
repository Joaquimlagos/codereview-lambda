# Implementation Plan: Method-Level Chunking for RAG Context

**Branch**: `feat/method-chunking` in both repos, from `develop` | **Date**: 2026-09-28 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `specs/002-method-chunking/spec.md`, baseline in
[baseline.md](baseline.md)

## Summary

Replace the RAG index's one-vector-per-file layout with one vector per non-trivial Java
method (plus `type` chunks and non-Java blocks). Each chunk carries its package, type and
field header and a stable identifier, and the index is published as `version: 2` at the
same S3 key. The build uses tree-sitter, batch embedding, a size guard and retries.

On the query side, RetrieveContext embeds each changed file's diff as its own query in one
batch call. It drops chunks overlapping the lines the diff changes, and returns the
top-8 chunks by max-similarity, with scores logged as JSON. InvokeLLM packs that context
separately for each model attempt, within the provider's budget, groups it by file, and
skips a provider without calling it when instructions plus diff alone exceed its budget.

Rollout order: Lambda first (it reads v1 exactly as today, plus v2), then the app (which
starts publishing v2). The v2 schema keeps the v1 field names, so the reverse order also
works.

## Technical Context

**Language/Version**: codereview-lambda: Python 3.14 (Lambda runtime). codereview-app
index script: Python 3.12 (`actions/setup-python` in `index-codebase.yml`).

**Primary Dependencies**: lambda: `pydantic`, `requests` (unchanged; no new dependency).
App script: standard library + `tree-sitter==0.26.0`, `tree-sitter-java==0.23.5`, pinned
in `scripts/requirements-index.txt` (approved exception to the stdlib-only rule).

**Storage**: S3 `codereview-artifacts`, key `index/develop/index.json` (versioned bucket).

**Testing**: lambda: `pytest` + `ruff` (existing CI). App script: stdlib `unittest` in
`scripts/tests/`, run by a new path-filtered workflow (research R12).

**Target Platform**: AWS Lambda (python3.14), GitHub Actions `ubuntu-latest`.

**Project Type**: a cross-repo data pipeline (build-time indexer + runtime retrieval
Lambdas).

**Performance Goals**: the index build's embedding phase is no slower than today's 16-file
build (SC-003), with 1 batch call for about 60 chunks. RetrieveContext adds one batch
embedding call per review (today: one single call).

**Constraints**: embedding input ≤ 2,048 tokens (guarded at 1,800 estimated); ≤ 100 inputs
per batch; Step Functions state payload ≤ 256 KB (worst case about 55 KB, research R11);
free tiers only: Groq 8,000 TPM, Cerebras 30,000 TPM, Gemini large (Principle III);
Lambda zip unchanged in dependencies.

**Scale/Scope**: about 60 chunks on `develop` today, about 100+ once PR #8's module lands.
Diffs up to about 8,000 embedding tokens / 11 files measured.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-checked after Phase 1 design: still passing.*

| Principle | Check | Status |
|---|---|---|
| I. English-only | All code, docs and specs in English; `baseline.md`/`spec.md` already are | ✅ |
| II. One Lambda per state | No new state. Changes stay inside RetrieveContext and InvokeLLM. The shared estimator lives in `src/contracts/` (shared models), not in a handler | ✅ |
| III. Cost-conscious | Free tier only; no new generative calls; batch embedding reduces calls; context budgets keep prompts inside provider limits | ✅ |
| IV. Integrations behind abstractions | `EmbeddingClient` gains `embed_queries(texts)` (batch) on both the real client and the `StubEmbeddingClient`; the router's budget logic is pure code, tested with the existing stub clients | ✅ |
| V. LocalStack | No new AWS service (S3 read unchanged) | ✅ |
| VI. Clarity | New logic in small, named modules (`diff_queries.py`, `ranking.py`, `token_estimate.py`) with docstrings citing the measurement behind each number | ✅ |

No violations, so Complexity Tracking is empty.

## Project Structure

### Documentation (this feature)

```text
specs/002-method-chunking/
├── baseline.md            # "before" measurement (done)
├── baseline-results.json  # raw data for baseline.md
├── measure_review.py      # measurement script (baseline and after)
├── spec.md
├── plan.md                # this file
├── research.md            # Phase 0
├── data-model.md          # Phase 1
├── quickstart.md          # Phase 1
├── contracts/
│   ├── index-v2.md
│   └── retrieve-context-v2.md
├── checklists/requirements.md
├── tasks.md               # /speckit-tasks
└── after.md               # written by the after-measurement task
```

### Source Code

codereview-lambda (this repo):
```text
src/contracts/
├── models.py              # ContextChunk / RetrievedContext: optional v2 fields
└── token_estimate.py      # NEW: estimate_tokens(text) = ceil(len/3.0), split threshold
src/integrations/
├── embeddings.py          # + embed_queries(texts) via batchEmbedContents; stub too
└── llm_router.py          # per-attempt packing, budgets, skip, grouped build_prompt,
                           #   LlmPromptTooLargeError, llm_attempt log line
src/retrieve_context/
├── handler.py             # version dispatch; v1 path untouched; v2 path
├── diff_queries.py        # NEW: split diff per file/hunk; changed-line set
└── ranking.py             # NEW: max-fusion, overlap exclusion, top-N, rag_* log lines
tests/unit/                # new unit tests per module above
tests/contract/            # retrieve_context / invoke_llm contract tests extended
tests/integration/         # v2 end-to-end through stubs
specs/001-pr-review-pipeline/contracts/step-io-contracts.md   # updated per FR-032
```

codereview-app:
```text
scripts/
├── build_index.py         # orchestration: collect → chunk → validate → batch embed → write
├── chunking.py            # NEW: tree-sitter Java chunker, trivial rule, headers, blocks, splitting
├── requirements-index.txt # NEW: pinned tree-sitter packages
└── tests/                 # NEW: unittest suite + stub HTTP server for retry tests
.github/workflows/
├── index-codebase.yml     # + concurrency group, + pip install -r scripts/requirements-index.txt
└── index-script-tests.yml # NEW: PR-time tests for scripts/**
CLAUDE.md                  # index contract → v2; stdlib-only paragraph → pinned exception
```

**Structure Decision**: keep each repo's existing layout. The only new top-level items are
the app's `scripts/tests/` and one workflow. On the Lambda side, the new modules stay inside
the package of the one Lambda that uses them (Principle II). The estimator is the one piece
two Lambdas share, so it goes in the existing shared `contracts` package.

## Delivery and rollout

| Step | Repo | Action | Gate before the next step |
|---|---|---|---|
| 1 | lambda | PR `feat/method-chunking` → `develop` | CI green; review |
| 2 | lambda | merge; `terraform apply` from `develop` | PR #7 re-run returns the baseline's top-3 in the same order, and logs `index_version: 1` (quickstart §4) |
| 3 | app | PR `feat/method-chunking` → `develop` (this PR's own review still runs on v1) | `index-script-tests` green; dry-run counts recorded in research R1 |
| 4 | app | merge → `index-codebase` publishes v2 | v2 object in S3; AI Studio counter recorded (research R4) |
| 5 | lambda | Groq probe (research R9); adjust the budget if needed with a follow-up PR → `develop` | — |
| 6 | lambda | after-measurement → `after.md`, in a docs PR → `develop` | — |

**Never a PR to `main`** (FR-031). Rollback at any point after step 4: restore the previous
index object version (contracts/index-v2.md); no Lambda change is needed.

## Risks

| Risk | Mitigation |
|---|---|
| Batch counts as N requests against the embedding quota | sized for N; measured at step 4 (R4) |
| Groq budget of 4,300 too high (pre-flight accounting) | a too-high budget costs one fast 413, as today; probe at step 5 (R9) |
| Feasibility count (59) not reproducible | FR-003 re-anchored to the counts recorded at step 3 (R1) |
| One run per PR is noisy; the answering model varies (Gemini 503s) | baseline.md Limitations: re-trigger up to 2× to get the same model; optional second run |
| Index commit ≠ PR merge base shifts line numbers for FR-020 | the mismatch is logged (`index_commit`); a mis-exclusion costs one slot, never a failure |

## Complexity Tracking

None: no constitution violations.
