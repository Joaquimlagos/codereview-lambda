# Feature Specification: Method-Level Chunking for RAG Context

> **Historical record.** This spec records the decisions as they were made and is not
> updated as the code changes. For the current state of the pipeline, see the
> [README](../../README.md).

**Feature Branch**: `feat/method-chunking` (codereview-lambda) · a branch of the same name in codereview-app, both from `develop`

**Created**: 2026-09-28

**Status**: Draft

**Input**: User description (summarised in English per Principle I): replace the RAG
index's one-chunk-per-file layout with per-method chunks, across both repositories.
**Indexing** (codereview-app, `scripts/build_index.py`): tree-sitter method chunking with a
context header, trivial methods dropped, non-Java files in blocks, a size check that splits
anything over the embedding limit, batch embedding, retry with backoff instead of
`sys.exit`, a concurrency group on `index-codebase`, and index `version: 2` with a per-chunk
identifier. **Retrieval** (codereview-lambda, RetrieveContext): top-N chunks grouped by file,
one diff embedding per changed file, a token budget aware of three providers' limits.
**Transition**: the Lambda reads v1 and v2, with a defined deploy order, and PRs go to
`develop` only. Includes a before/after measurement on codereview-app PRs #3, #7 and #8.

**Inputs carried in**:
- [baseline.md](baseline.md): the "before" measurement.
- The feasibility test already run on the current codebase: 59 chunks, largest ~680 tokens,
  batch embedding 9.4× faster than one call per chunk, and the embedding API silently drops
  input beyond 2,048 tokens. It is not recorded in either repo yet; see Assumptions.

## Why

The baseline shows three concrete problems with whole-file chunks:

1. **The query is silently truncated.** The whole PR diff is embedded as one query, and
   `gemini-embedding-001` drops everything past 2,048 tokens. PR #8's retrieval was decided
   by its first 26% only. 5,870 of 7,918 embedding tokens, including `ProjectService` (the
   change's core logic) and all three test files, never reached the query. PR #3 lost its
   last ~80 tokens.
2. **The context is mostly redundant.** For PR #3 and #7, all three retrieved files are files
   the PR itself modifies, so the context repeats code the diff already shows. That is up to
   38% of the prompt (PR #7: 1,212 of 3,187 tokens).
3. **Whole-file scores barely discriminate.** Every indexed file scores between 0.67 and
   0.83 against every one of the three diffs. In PR #3, ranks 3 and 4 differ by 0.0006.
4. **A file is too coarse a unit.** One file costs 120 to 2,488 characters of context
   regardless of how much of it is relevant, and a file past 2,048 tokens would be embedded
   truncated, with nothing to warn about it.

## User Scenarios & Testing *(mandatory)*

The "user" throughout is the maintainer who opens a PR on codereview-app and reads the
automated review, and who operates the pipeline.

### User Story 1 - Reviews receive the relevant methods, not whole files (Priority: P1)

When a PR is reviewed with context, the reviewer model receives the handful of methods most
related to the change. Each comes with enough surrounding structure (package, type
signature, fields) to be understood, grouped by file, and does not duplicate what the diff
already shows.

**Why this priority**: This is the point of the feature. Everything else exists to make it
correct, safe to roll out, or measurable.

**Independent Test**: With a version 2 index published and the new Lambda deployed, trigger
a review of PR #7. In the execution's `context`, every chunk is a method, type or block with
an identifier and line range. Chunks from the same file are grouped under one file heading
in the prompt. The context is within the budget in FR-023.

**Acceptance Scenarios**:

1. **Given** a version 2 index, **When** a PR needing context is reviewed, **Then**
   RetrieveContext returns at most N ranked chunks (FR-018), each with its identifier, file,
   line range and similarity score.
2. **Given** several selected chunks from the same file, **When** the prompt is built,
   **Then** that file's path and header appear once, followed by its chunks in line order.
3. **Given** a chunk whose lines overlap lines the diff changes, **When** context is
   selected, **Then** that chunk is left out, while unchanged methods of the same class
   remain eligible (FR-020).

---

### User Story 2 - The index build is safe, fast and never silently lossy (Priority: P1)

Every push to `develop` produces a complete index or fails loudly, leaving the previous
index in place. No chunk is ever embedded truncated. A momentary rate limit or outage no
longer fails the build. Two builds never race to publish.

**Why this priority**: Method chunking multiplies the number of embedding inputs (16 → ~59).
Without batching, retry and a size guard, it would make the build slower, more fragile, and
exposed to the same silent truncation it is meant to fix.

**Independent Test**: Run the build against a stub embedding endpoint that answers 429 then
503 then success, fed with one oversized synthetic method. The build succeeds. The
oversized method is split into parts, each under the limit. The number of embedding calls
equals ⌈chunks / 100⌉ plus retries.

**Acceptance Scenarios**:

1. **Given** a method whose text exceeds the embedding limit, **When** the index is built,
   **Then** it is split into consecutive parts that each fit, each part keeps the header,
   and no input over the limit is ever sent.
2. **Given** the embedding API answers 429, 503 or times out, **When** the build retries,
   **Then** it waits with exponential backoff (honouring `Retry-After` when present) and
   continues. It fails only after the retry budget is spent, and then no index is uploaded.
3. **Given** two pushes to `develop` in quick succession, **When** both trigger
   `index-codebase`, **Then** at most one build runs at a time and the index finally
   published reflects the newer commit.
4. **Given** a Java file tree-sitter cannot parse, **When** the index is built, **Then** that
   file is chunked by blocks instead, with a warning, and the build continues.

---

### User Story 3 - Large diffs are fully represented in retrieval (Priority: P2)

Every changed file, including the tail of a large diff, influences which context is
retrieved.

**Why this priority**: This fixes baseline problem 1. It is P2 only because it is
independent of chunking and could ship on its own.

**Independent Test**: Review PR #8 (11 files, 6,531 tokens). The RetrieveContext log shows
one query per changed file, and no query input over the limit.

**Acceptance Scenarios**:

1. **Given** a diff touching K files, **When** context is retrieved, **Then** K queries are
   embedded (more if a file's diff is itself split per FR-016), in as few batch calls as the
   100-per-call limit allows.
2. **Given** several queries, **When** chunks are ranked, **Then** each chunk's score is its
   best similarity against any query (FR-019), and the log records which query produced it.

---

### User Story 4 - Context fits whichever provider answers (Priority: P2)

The context sent to each model attempt is sized to that provider's limits. A prompt that
Groq's 8,000 tokens/minute would reject is not built for Groq, while Cerebras or Gemini can
still receive more.

**Why this priority**: More, smaller chunks make it easy to overshoot Groq's limit.
InvokeLLM's fallback already tolerates a 413, but losing a model to oversized *context* is a
self-inflicted failure.

**Independent Test**: With the medium tier, give InvokeLLM (stubbed clients) a context whose
estimated size exceeds Groq's budget. The Groq attempt's prompt contains fewer chunks, and
the Cerebras attempt's prompt contains more. Neither prompt exceeds its provider budget.
Then give it a diff larger than Groq's budget: the stubbed Groq client is never called, and
the skip is logged.

**Acceptance Scenarios**:

1. **Given** ranked chunks and a provider budget, **When** a model attempt's prompt is
   built, **Then** chunks are added best-first while they fit. A chunk that does not fit is
   skipped whole, never truncated.
2. **Given** a prompt over a provider's budget, **When** that attempt is built, **Then**
   the RAG context is reduced first, dropping the lowest-scored chunks until it fits.
3. **Given** instructions plus diff alone already exceed a provider's budget, **When** that
   provider's turn comes, **Then** it is skipped without being called, a log line records
   the model, the estimate and the budget, and the router moves to the next model. The
   diff itself is never trimmed.
4. **Given** every model in the tier was skipped for budget, **When** the list is
   exhausted, **Then** the run fails with a non-retryable error that names the budgets.
   Retrying cannot shrink the diff, so no Step Functions retry is spent on it.

---

### User Story 5 - Rollout cannot break reviews, in any deploy order (Priority: P1)

The two repositories deploy independently, so every combination of old or new Lambda with
a v1 or v2 index keeps reviews working.

**Why this priority**: This is a cross-repo contract change, and a broken contract here
fails every review until both sides are fixed.

**Independent Test**: The unit tests feed RetrieveContext a v1 index and a v2 index. The v1
result equals today's (same top-3 files, same text). Separately, the *current* Lambda code
is fed a v2 index and completes, degraded but without error (FR-029).

**Acceptance Scenarios**:

1. **Given** the new Lambda and a v1 index, **When** a PR is reviewed, **Then** behaviour
   is identical to the baseline: one diff query, top-3 whole files, same prompt.
2. **Given** the old Lambda and a v2 index (wrong-order deploy), **When** a PR is reviewed,
   **Then** the review completes with the top-3 chunks as context, without error.
3. **Given** an index whose `version` is neither 1 nor 2, **When** RetrieveContext reads it,
   **Then** the run fails visibly, like today's model or dimension mismatch.

---

### User Story 6 - The improvement is measured the same way it was baselined (Priority: P3)

After rollout, PRs #3, #7 and #8 are re-measured with the baseline's procedure and script,
and the results sit next to the baseline.

**Independent Test**: `after.md` exists, has the same tables as `baseline.md`, and records
the Lambda `CodeSha256` and index commit it measured.

### Edge Cases

- **A type with no non-trivial methods** (records such as `LoginRequest`, DTOs,
  interfaces): the type still gets one `type` chunk made of its header alone, or it would
  vanish from the index.
- **Nested and inner types**: the header names the enclosing type chain.
- **Overloaded methods**: identifiers include parameter types and line range, so they stay
  unique.
- **A single statement larger than the limit** (e.g. a huge literal): split by lines. The
  last resort is a hard character cut, logged as a warning. Never skipped silently.
- **Deleted files in the diff**: they still produce a query, since the removed code is a
  real signal.
- **A diff with more than 100 query inputs**: split across several batch calls.
- **A PR whose diff is only non-Java files**: the non-Java blocks compete normally in
  ranking.
- **An index built from a different commit than the PR's merge base**: the overlap rule in
  FR-020 compares line numbers on the pre-change side, so it holds exactly only when the
  index `commit` is the PR's merge base. When it isn't, line numbers may have drifted. The
  rule is then still applied, but a log line flags the commit mismatch, and a mis-excluded
  chunk costs one context slot, not a failure.
- **A pure insertion** (added lines, nothing removed): it changes the chunk that contains
  the insertion point, so that chunk counts as overlapping. A new file overlaps nothing in
  the index.
- **Step Functions' 256 KB payload limit**: N chunks of up to ~2,048 tokens each must stay
  far below it. At N=8, the worst case is ~70 KB.

## Requirements *(mandatory)*

### Indexing: codereview-app, `scripts/build_index.py`

- **FR-001**: Java files MUST be chunked per method (including constructors) using a
  parser that supports Java 21 syntax, records included.
- **FR-002**: Each Java chunk MUST carry a context header, stored separately from its body:
  package, the enclosing type's declaration line or lines (annotations, modifiers,
  `extends`/`implements`, record components), and that type's field declarations. The
  embedded input is header plus body.
- **FR-003**: Trivial methods MUST be excluded: plain getters and setters, empty bodies,
  and constructors that only assign parameters to fields. Record compact constructors that
  validate are not trivial. The rule MUST be written down in `research.md` with its chunk
  counts on a named commit. The implementation MUST reproduce those counts, and the build
  log (FR-013) reports them on every run.
- **FR-004**: A type left with no chunks after FR-003 MUST get one `type` chunk made of its
  header.
- **FR-005**: Non-Java indexed files (`pom.xml`, `application.yml`, …) MUST be chunked into
  blocks split at blank-line or top-level-element boundaries and packed up to a target size.
  Proposed target: ~400 tokens.
- **FR-006**: Before any embedding call, every chunk's size MUST be checked against the
  limit. A chunk over the split threshold MUST be split into consecutive parts at statement
  or line boundaries, each keeping the header. No input over 2,048 tokens may ever be sent.
  The size check MUST NOT under-count relative to the API. Proposed: split above an
  estimated 1,800 tokens, estimating at 3 characters per token. The embedding model's own
  tokenizer measured about 3.4 chars/token on this code, so the worst real size of a chunk
  that passes is about 1,590 tokens.
- **FR-007**: Chunks MUST be embedded with batch calls of at most 100 inputs each.
- **FR-008**: HTTP 429, HTTP 503 and timeouts or connection errors MUST be retried with
  exponential backoff and jitter, honouring `Retry-After`. Proposed: 5 attempts per call,
  2 s base, and a 5-minute ceiling for the whole build. Other errors (400, 401/403, a
  malformed response) MUST still fail immediately. After the retries run out, the build
  MUST exit non-zero without writing or uploading an index.
- **FR-009**: `index-codebase.yml` MUST declare a concurrency group per branch, so at most
  one build runs at a time and a newer push supersedes an in-flight one. Proposed:
  `cancel-in-progress: true`.
- **FR-010**: The index MUST be published as `version: 2` at the same key
  (`index/develop/index.json`), with the same top-level fields as v1.
- **FR-011**: Every v2 chunk MUST have a stable identifier built from its file, type,
  method (with parameter types) and line range. Proposed:
  `src/…/JwtValidator.java#JwtValidator.isValid(String):54-67`, with `#part-2` appended
  for split parts and `path#L1-40` for blocks. The same code MUST yield the same
  identifiers across builds.
- **FR-012**: Every v2 chunk MUST keep the v1 field names `path`, `text` and `vector` with
  the same meanings (`text` = the body), so that a v1 reader still works (FR-029).
- **FR-013**: The build log MUST report the number of chunks by kind, the largest chunk's
  estimated size, how many chunks were split, the number of embedding calls, and retries.

### Retrieval: codereview-lambda, RetrieveContext

- **FR-014**: RetrieveContext MUST read index versions 1 and 2. Any other version MUST fail
  the run with an explicit compatibility error.
- **FR-015**: With a v1 index, behaviour MUST be identical to the baseline: one query for
  the whole diff, top-3, and the same output shape.
- **FR-016**: With a v2 index, the diff MUST be split per changed file and each file's diff
  embedded as its own query. A file diff over the split threshold MUST be split at hunk
  boundaries, and a single oversized hunk by lines.
- **FR-017**: Query embeddings MUST use batch calls of at most 100 inputs.
- **FR-018**: RetrieveContext MUST return the top-**N** chunks by score. **Proposed N = 8**:
  at the largest measured chunk (~680 tokens) that is ≤ 5,440 tokens before the budget
  trims it. The typical size depends on the average chunk, which the feasibility test did
  not report, and FR-023 bounds it either way. N MUST be a single named constant, and the
  "after" measurement reports how many of the N survived packing.
- **FR-019**: A chunk's score MUST be its maximum cosine similarity over all queries.
- **FR-020**: Before taking the top-N, RetrieveContext MUST exclude every v2 chunk whose
  line range, in the same file, overlaps the lines the diff changes: the removed or
  modified lines on the pre-change side of each hunk, plus the insertion point of pure
  additions. Hunk context lines do not count. Only the overlapping chunks are excluded, not
  the whole file: unchanged methods of a changed class stay eligible, since they are often
  the most useful context (callers, collaborators, invariants). This does not apply to v1
  indexes, whose chunk is the whole file (FR-015).
- **FR-021**: Each returned chunk MUST carry its identifier, path, line range, header, body
  and score. The new fields MUST be optional in the contract, so outputs produced from a v1
  index still validate.
- **FR-022**: RetrieveContext MUST log, for every chunk it selects, the chunk's identifier,
  its similarity score and the query (changed file) that produced that score. The log line
  MUST be machine-readable (one JSON object per selection, carrying the PR number), so the
  "after" measurement reads the scores straight from CloudWatch instead of recomputing
  them. It MUST also log how many chunks FR-020 excluded. Today similarities are not
  observable at all, and the baseline had to recompute them.

### Context budget: codereview-lambda, InvokeLLM

- **FR-023**: Context MUST be packed per model attempt, not once per review, within
  `min(context cap, provider prompt budget − estimated(instructions + diff))`.
  Proposed values:

  | Provider | Free-tier limit | Output reserve | Prompt budget |
  |---|---|---|---|
  | Groq | 8,000 TPM | 3,700 at `medium` (max measured 3,668) · 1,200 at `low` (max measured 1,073) | 4,300 · 6,800 |
  | Cerebras | 30,000 TPM | 12,000 (`max_completion_tokens`) | 18,000 |
  | Gemini | far larger | — | a configured ceiling (proposed 100,000; confirm against the current free-tier TPM in research) |

  **Context cap: 3,000 tokens** for every provider. More context dilutes a review as much as
  too little starves it, and 3,000 is about 2.5× the largest baseline context. Budgets MUST
  live next to the per-provider output caps they are derived from (`GROQ_/CEREBRAS_MAX_COMPLETION_TOKENS`).
- **FR-024**: When a prompt would exceed an attempt's budget, the RAG context MUST be
  reduced first, dropping chunks from the lowest score up. If instructions plus diff alone
  exceed the budget, that provider MUST be skipped without being called, and a log line
  MUST record the model, the estimated prompt size and the budget. A skip is neither a
  transient failure nor a missing model. If *every* model in the tier was skipped, the
  router MUST raise a non-retryable error naming the budgets. If some were skipped and the
  rest failed transiently, today's rule applies (retryable).
- **FR-025**: Token estimates MUST NOT under-count for any of the three providers.
  Proposed: **3.0 characters per token** for every provider, with no tokenizer dependency
  added to the Lambda package. The baseline measured 4.1–4.4 on gpt-oss's tokenizer but
  only about 3.4 on Gemini's, so any ratio at or above 3.4 would under-count for Gemini. 3.0
  over-estimates gpt-oss prompts by about 30%, which is the price of one shared, safe
  estimator.
- **FR-026**: Packing MUST go best-score-first, skip whole chunks that do not fit (never
  truncate one), and then order the kept chunks by file and line for presentation.
- **FR-027**: The prompt MUST group chunks by file: the file path and header once, then
  each chunk with its line range. The existing "not part of the change" framing MUST be
  kept.
- **FR-028**: Each attempt MUST log the context size it used (chunks kept and dropped,
  estimated tokens).

### Transition and delivery

- **FR-029**: Deploy order: **(1)** codereview-lambda first. It is merged to `develop` and
  deployed. Its v1 path is unchanged (FR-015), so this deploy is behaviour-neutral, which
  one re-run of PR #7 (same top-3 as the baseline) confirms. **(2)** Then codereview-app.
  Merging to `develop` runs `index-codebase`, which replaces the v1 index with v2. If the
  order is ever reversed, FR-012 keeps the old Lambda working on a v2 index in degraded
  mode (top-3 method bodies, ungrouped).
- **FR-030**: Rollback MUST be possible without a Lambda redeploy: restore the previous
  object version of `index/develop/index.json` (bucket versioning is on), or revert the app
  merge, which rebuilds v1.
- **FR-031**: Each repo's changes MUST go on a branch created from `develop`, with a PR into
  `develop`. No PR may target `main`.
- **FR-032**: The index contract section of codereview-app's `CLAUDE.md` and this repo's
  `contracts/step-io-contracts.md` MUST be updated in the same PRs that change the schema.

### Measurement

- **FR-033**: The "after" measurement MUST use PRs #3, #7 and #8 and the procedure and
  script in [baseline.md](baseline.md), unchanged except as that file allows, including
  **3 runs per PR** triggered one at a time (`trigger_runs.py`). It happens after both
  deploys, and the results go in `after.md`. Similarity scores come from the
  FR-022 log lines. The script's recompute is kept as a cross-check, and any disagreement
  is reported. It MUST also report the distribution of similarity scores over all scored
  candidates (min, max, range, top-N vs rest, margin at the cut, and the standardised gap
  used in baseline.md), to show whether method chunks separate relevant from irrelevant
  context better than whole files.
- **FR-034**: PR #3 MUST be scored against the baseline's answer key: **5 defects, counted
  per defect, not per line.** Both fail-open `catch` blocks are one defect, and so is the
  password logged on two lines. The detection rule is also the baseline's.

### Key Entities

- **Chunk (v2)**: `id`, `path`, `kind` (`method` | `type` | `block`), `symbol` (type and
  method, nullable), `startLine`, `endLine`, `part` (nullable), `header`, `text`, `vector`.
- **Index v2**: the v1 top-level fields (`version: 2`, `branch`, `commit`, `generatedAt`,
  `model`, `dimensions`) plus `chunks: Chunk[]`.
- **Query**: one per changed file, or per part of an oversized file diff. It is identified
  by path and embedded with `RETRIEVAL_QUERY`.
- **Ranked context chunk**: an index chunk plus its score and the query that produced it.
  It is what RetrieveContext returns and InvokeLLM packs.
- **Provider budget**: the prompt-token ceiling for one model attempt (FR-023).

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: No embedding input, at index time or query time, exceeds the 2,048-token
  limit. The build and retrieval logs show the largest input, and split counts where
  splitting happened.
- **SC-002**: For PR #8, 100% of changed files contribute a query, versus 26% of the diff's
  embedding tokens today.
- **SC-003**: The index build's embedding phase completes in no more wall-clock time than
  today's 16-file build, despite about 3.7× more chunks.
- **SC-004**: A build hit by one 429 and one 503 still publishes a complete index. A build
  whose retries run out publishes nothing and leaves the previous index in place.
- **SC-005**: For PRs #3, #7 and #8, every model attempt's context is within its FR-023
  budget, and no attempt fails with 413 because of context.
- **SC-006**: Over 3 runs, PR #3's mean number of detected defects is at least the
  baseline's (2.67 of 5, range 2–4; baseline.md §5). Its per-defect detection rate is also
  reported against the baseline's (fail-open 3/3, skew 1/3, password 3/3, enumeration 1/3,
  inverted test 0/3).
- **SC-007**: 0 selected chunks overlap lines the diff changes. Baseline: 3 of 3 files for
  PR #3 and 3 of 3 for PR #7 were files the PR modifies.
- **SC-008**: Across the rollout, 0 reviews fail because of the index version, whether the
  deploy order is followed or reversed.
- **SC-010**: Every "after" similarity score is read from RetrieveContext's own logs, with
  no recompute needed.
- **SC-012**: The standardised gap between the selected top-N and the remaining candidates
  is higher than the baseline's (1.78 / 1.93 / 1.78 for PRs #3 / #7 / #8). This is reported
  either way. A lower value is a finding, not a failure of the run.
- **SC-011**: No provider is ever called with a prompt over its budget. Every skip appears
  in the logs with the estimate and the budget.
- **SC-009**: PR #7 and #8 inline-comment counts (mean and range over 3 runs; baseline 1.33 and
  1.00), categories and severities are reported
  next to the baseline. `parse_fallback` stays at 0.

## Assumptions

- **Feasibility numbers** (59 chunks, largest ~680 tokens, 9.4× batch speed-up) come from
  the test already run and are taken as given for sizing. Its chunk count could not be
  reproduced during planning (see research.md, R1), so FR-003 is anchored to the rule and
  counts recorded there instead.
- **tree-sitter over javalang**, because javalang does not parse Java 21 `record`. This
  **breaks a documented codereview-app constraint**: its `CLAUDE.md` says
  `build_index.py` uses the standard library only, with nothing to `pip install`. The
  feature therefore adds a pinned `pip install` step (tree-sitter plus the Java grammar) to
  `index-codebase.yml`. **Approved.** That paragraph of codereview-app's `CLAUDE.md` is
  updated in the implementation PR. The Lambda side adds no dependency.
- **Batch quota accounting is unknown**: it is not known whether `batchEmbedContents`
  counts as one request or one per input against the daily quota. The design MUST fit the
  free tier under the worst case (one per input): about 60 inputs per index build and K per
  review. The first real build MUST record the AI Studio usage counter before and after, to
  settle it (see plan).
- The Lambdas are still deployed by hand with `terraform apply` from the merged `develop`.
- Query-side splitting by hunks, and the 1,800-token threshold, reuse the index side's
  estimator, so both sides share one definition of "too big".

## Out of scope

- Trimming or summarising the diff itself when it exceeds a provider's budget.
- Changing the embedding model, its dimensionality, or the index's S3 location.
- Re-ranking with a second model, hybrid lexical search, or similarity thresholds (the
  logs from FR-022 would inform a threshold later).
- Chunking languages other than Java beyond generic blocks.
