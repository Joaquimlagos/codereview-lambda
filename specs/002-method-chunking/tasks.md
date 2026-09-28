# Tasks: Method-Level Chunking for RAG Context

**Input**: [plan.md](plan.md), [spec.md](spec.md), [research.md](research.md),
[data-model.md](data-model.md), [contracts/](contracts/), [quickstart.md](quickstart.md)

**Tests**: included. The spec gives every story an independent test, and FR-015's
"identical to baseline" can only be held by a golden test.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: can run in parallel (different files, no dependency on an unfinished task)
- **[Story]**: US1–US6 from spec.md
- Paths are prefixed with the repo: `lambda:` = codereview-lambda, `app:` = codereview-app
- 🔒 = an outward-facing step (push, PR, merge, deploy, empty commit). Stop and confirm
  with the user before doing it.

**Ordering note**: phases follow the *delivery* order (the Lambda ships before the app,
FR-029), not strict story priority. US3 comes before US1 because the v2 retrieval path is
built on per-file queries.

---

## Phase 1: Setup

- [x] T001 🔒 app: create branch `feat/method-chunking` from `origin/develop` (no push until T034)
- [x] T002 [P] app: add `scripts/requirements-index.txt` pinning `tree-sitter==0.25.2` (0.26.0 corrupted memory, research R1) and `tree-sitter-java==0.23.5` (research R1)
- [x] T003 [P] lambda: before any code change, capture golden fixtures from the current code: `tests/fixtures/index_v1_small.json` (4 files, fixed vectors), and the current `retrieve_context` output and `build_prompt` output for it, as `tests/fixtures/golden_v1_context.json` and `tests/fixtures/golden_v1_prompt.txt` (FR-015)

---

## Phase 2: Foundational (Lambda, blocks all Lambda stories)

- [x] T004 [P] lambda: `src/contracts/token_estimate.py`: `estimate_tokens(text) = ceil(len(text)/3.0)`, `SPLIT_THRESHOLD_TOKENS = 1800`, with a docstring citing baseline.md's measured ratios; tests in `tests/unit/test_token_estimate.py`
- [x] T005 [P] lambda: `src/contracts/models.py`: optional `id`, `start_line`, `end_line`, `header`, `score`, `matched_query` on `ContextChunk`; optional `index_version` on `RetrievedContext` (data-model.md); extend `tests/contract/test_retrieve_context_contract.py` so both the v1 and v2 shapes validate
- [x] T006 lambda: `src/integrations/embeddings.py`: `embed_queries(texts) -> list[vector]` on `EmbeddingClient` via `batchEmbedContents`, ≤100 per call, with count and dimension validation (research R4); `StubEmbeddingClient.embed_queries` returns per-text vectors (a mapping or callable) and records calls; tests in `tests/unit/test_embeddings.py` (payload shape, 150 texts → 2 calls, misaligned response → `EmbeddingError`)

**Checkpoint**: shared pieces in place; the existing suite is still green.

---

## Phase 3: US5, rollout cannot break reviews (P1) 🎯 first Lambda slice

**Goal**: RetrieveContext reads v1 exactly as today and recognises v2; a v1-only reader survives a v2 index.
**Independent test**: spec US5; quickstart §1.

- [x] T007 [P] [US5] lambda: `tests/unit/test_retrieve_context_versions.py`: v1 fixture → output equals `golden_v1_context.json` plus `index_version: 1`; `version: 3` → `IndexCompatibilityError`; a missing `version` field → treated as v1 (today's indexes have it, but it stays defensive)
- [x] T008 [P] [US5] lambda: `tests/fixtures/index_v2_small.json` per contracts/index-v2.md (two classes, a record `type` chunk, a split method, a `pom.xml` block), plus `tests/contract/test_index_v2_backcompat.py`: today's `_top_chunks` ranks this v2 fixture without error (FR-029 reverse order)
- [x] T009 [US5] lambda: `src/retrieve_context/handler.py`: dispatch on `version`; move today's logic unchanged into `_retrieve_v1`; route v2 to a `_retrieve_v2` stub for now; set `index_version` (depends on T005, T007, T008)

**Checkpoint**: a deploy with only this phase would already be safe and behaviour-neutral.

---

## Phase 4: US3, large diffs fully represented (P2)

**Goal**: one query per changed file, oversized files split by hunk, all in batch calls.
**Independent test**: spec US3.

- [x] T010 [P] [US3] lambda: `tests/unit/test_diff_queries.py`: split per file (add, modify, delete, rename), hunk split above 1,800 estimated tokens keeping the file header lines, a single oversized hunk split by lines, PR #8's shape producing 11 queries; changed-line set (removed lines, both neighbours of a pure insertion, context lines ignored, a new file adding nothing)
- [x] T011 [US3] lambda: `src/retrieve_context/diff_queries.py`: `split_queries(diff) -> list[DiffQuery]` and `changed_lines(diff) -> dict[path, set[int]]` (research R7, R8)
- [x] T012 [US3] lambda: `_retrieve_v2` builds the queries and embeds them with one `embed_queries` call; the empty-diff guard (`EmptyDiffError`) is kept (depends on T006, T009, T011)

---

## Phase 5: US1, reviews receive the relevant methods (P1)

**Goal**: top-8 method chunks, never overlapping changed lines, scored and logged, grouped by file in the prompt.
**Independent test**: spec US1.

- [x] T013 [P] [US1] lambda: `tests/unit/test_ranking.py`: max-fusion and `matched_query`; overlap exclusion drops only the overlapping chunks and keeps other methods of the same file; `TOP_N = 8`; tie order is stable; `rag_query` and `rag_chunk` JSON log lines (via caplog) match contracts/retrieve-context-v2.md, including `pr`, `score`, `excluded_overlapping` and `index_commit`
- [x] T014 [US1] lambda: `src/retrieve_context/ranking.py`: `rank(chunks, queries, changed_lines, top_n)` and the log emission (FR-018 to FR-022)
- [x] T015 [US1] lambda: finish `_retrieve_v2`: ranking plus `ContextChunk` v2 fields (depends on T012, T014)
- [x] T016 [P] [US1] lambda: `tests/unit/test_build_prompt_grouped.py`: v2 layout (files ordered by best score, header once per file, chunks in line order with `// lines a-b`, markers unchanged); v1 layout byte-identical to `golden_v1_prompt.txt`
- [x] T017 [US1] lambda: `src/integrations/llm_router.py` `build_prompt`: grouped layout for located (v2) chunks — chosen from the chunks themselves, so `invoke_llm/handler.py` needed no change (FR-027)
- [x] T018 [US1] lambda: extend `tests/integration/test_context_enriched_review.py` with a v2 run end to end through the stubs: no overlapping chunk, grouped prompt, ≤8 chunks

---

## Phase 6: US4, context fits whichever provider answers (P2)

**Goal**: per-attempt packing within the provider budget; skip a provider when the diff alone is too big.
**Independent test**: spec US4.

- [x] T019 [P] [US4] lambda: `tests/unit/test_llm_router_budget.py`: the medium tier's Groq prompt has fewer chunks than Cerebras's; best-first packing skips whole chunks; a diff over Groq's budget → Groq client never called, plus a skip log line; every entry skipped → `LlmPromptTooLargeError` (not an `LlmTransientError`); a skip plus a transient failure → `LlmTransientError`; an `llm_attempt` JSON line on each attempt
- [x] T020 [US4] lambda: `src/integrations/llm_router.py`: `PROMPT_BUDGET` table and `CONTEXT_TOKEN_CAP = 3000` next to the `*_MAX_COMPLETION_TOKENS` constants (research R9, with its derivation comments); `pack_context`; per-attempt prompt build; skip; `LlmPromptTooLargeError`; `llm_attempt` log (FR-023 to FR-026, FR-028) (depends on T004, T017)
- [x] ~~T021~~ Dropped: `StubLlmRouter` already records `build_prompt`'s output, which now groups v2 chunks; packing is provider-specific and is tested on the real router (`tests/unit/test_llm_router_budget.py`)

---

## Phase 7: Lambda delivery (US5 gate)

- [x] T022 lambda: update `specs/001-pr-review-pipeline/contracts/step-io-contracts.md` (RetrieveContext v1/v2, output fields, `TOP_N`, log lines, the new error class) and the RetrieveContext line in `CLAUDE.md` / `README.md` (FR-032)
- [x] T023 lambda: `ruff check src tests && pytest`; `terraform fmt/validate` unchanged
- [x] T024 🔒 lambda: push `feat/method-chunking`, open a PR → **`develop`** (never `main`)
- [x] T025 🔒 lambda: after the merge, `terraform apply` from `develop`; re-trigger PR #7; check with `measure_review.py pr:7` that it returns the baseline's top-3 in the same order and logs `index_version: 1` (quickstart §4)

**Checkpoint**: the new Lambda is live and behaviour-neutral on v1.

---

## Phase 8: US2, safe, fast, never-lossy index build (P1), codereview-app

**Goal**: v2 index built by tree-sitter chunking, a size guard, batch calls and retry; no racing builds.
**Independent test**: spec US2; quickstart §2–3.

- [x] T026 [P] [US2] app: `scripts/tests/test_chunking.py`: a record with and without methods (`type` chunk), nested classes (`Outer.Inner`), overloads (distinct ids), the trivial rule (getter, setter, empty body, assign-only constructor dropped; a validating compact constructor kept), header content (package, declaration, fields; no imports), a parse error falling back to blocks with a warning, non-Java blocks at ≤~400 tokens, a method over 1,800 estimated tokens split into parts that each repeat the header, ids identical across two runs
- [x] T027 [P] [US2] app: `scripts/tests/test_embedding.py` with a local `http.server` stub: ≤100 per batch, alignment and dimension checks, retry on 429 honouring `RetryInfo.retryDelay`/`Retry-After`, on 503, and on timeout; fail-fast on 400; after retries run out → non-zero exit and no `index.json` written
- [x] T028 [US2] app: `scripts/chunking.py`: the tree-sitter chunker, trivial rule, headers, `type` chunks, blocks, splitting, ids (FR-001 to FR-006, FR-011) (depends on T002, T026)
- [x] T029 [US2] app: `scripts/build_index.py`: collect → chunk → validate → batch embed with retry (replacing the `sys.exit` calls on transient errors) → write v2 with the v1 field names; `--dry-run` (chunk and count only); the FR-013 summary log (depends on T027, T028)
- [x] T030 [P] [US2] app: `.github/workflows/index-codebase.yml`: `concurrency: {group: index-codebase-${{ github.ref }}, cancel-in-progress: true}`; a `pip install -r scripts/requirements-index.txt` step (FR-009, research R6)
- [x] T031 [P] [US2] app: new `.github/workflows/index-script-tests.yml` (on `pull_request`, paths `scripts/**` and `.github/workflows/index-*.yml`), running `python -m unittest discover -s scripts/tests` (research R12)
- [x] T032 [US2] app: `python scripts/build_index.py --dry-run` on `develop`'s tree; write the counts by kind into lambda `research.md` R1 as FR-003's reference
- [x] T033 [US2] app: `CLAUDE.md`: index contract → v2 (link to lambda's contracts/index-v2.md), "one chunk per file" → method chunking, the stdlib-only paragraph → the approved pinned exception, the concurrency group; `README.md`, if it describes the index
- [x] T034 🔒 app: push `feat/method-chunking`, open a PR → **`develop`** (its own review still runs on the v1 index, as expected)
- [x] T035 🔒 app: before merging, note the day's value in the **daily chart** of `gemini-embedding-001` requests in AI Studio (the rate-limit page shows 28-day peaks, research R15, so it cannot be used); merge; confirm `index-codebase` logs 1 call and 0 retries; confirm the S3 object is `version 2`; record the counter again → research R4 (1 or N per batch) (user-assisted: AI Studio is a UI)

**Checkpoint**: v2 is live; rollback = restore the previous S3 object version (contracts/index-v2.md).

---

## Phase 9: US6, measure the same way (P3)

- [ ] T036 🔒 lambda: Groq budget probe (quickstart §6, research R9). Send prompts of increasing estimated size (e.g. 3,500 / 4,300 / 5,000) at `medium`; record prompt and output tokens and whether each was accepted. Pick the value, and record how many of PRs #3/#7/#8 it would skip onto Cerebras (5 RPM). **Fix the unit mismatch first** (research R9): either express every provider budget in estimate units, `(TPM − reserve) × measured factor`, or divide the estimate by a per-provider factor (gpt-oss ≈ 1.44, Gemini ≈ 1.19) before comparing; the embedding split threshold keeps the uncorrected, pessimistic estimate. Any change goes in a small PR → `develop`, then redeploy. **Then re-evaluate** whether the over-budget skip (FR-024) should also apply to reviews with no context, which today keep the pre-002 prompt and never skip (decision approved 2026-09-28: skip limited to v2 context until Groq's real limit is measured)
- [ ] T037 lambda: `specs/002-method-chunking/measure_review.py`: read `rag_query`/`rag_chunk`/`llm_attempt` lines from CloudWatch as the primary source for scores; recompute v2 scores by importing `retrieve_context.diff_queries` and `ranking` as a cross-check; report disagreements; add the score distribution (min, max, range, top-N vs rest, margin at the cut, standardised gap) over all candidates after exclusion (spec FR-033, SC-012); keep the `pr:N` selector and the section markers
- [ ] T038 🔒 lambda: run baseline.md's procedure unchanged (`trigger_runs.py`, 3 runs per PR, one at a time, 60 s apart), score each PR #3 run against the 5-defect key (mean and per-defect rate), and write `specs/002-method-chunking/after.md` with the same tables (score distribution included) plus SC-002/005/007/010/011/012; open a docs PR → `develop`

---

## Phase 10: Polish

- [ ] T039 [P] lambda: finalise research.md (R1 counts, R4 quota answer, R9 Groq answer, Gemini TPM ceiling) and tick checklists/requirements.md
- [ ] T040 [P] lambda: README "Results" section: before/after table linked to baseline.md and after.md

---

## Follow-ups (not required for this feature)

- [ ] T041 app: **token-bounded embedding batches** in `scripts/build_index.py` (research R4, R15). Besides the 100-input cap, cap each `batchEmbedContents` call at an estimated token total safely under the 30,000 TPM limit (e.g. 25,000), and when the next batch would exceed what is left of the current minute's budget, wait for the window to roll over instead of relying on 429 retries. Add a stub-server test with oversized chunks that asserts the per-call token cap and the wait. Not needed at today's size (41 chunks, ~5,100 estimated tokens, one call); needed before the indexed code grows toward ~30,000 estimated tokens

---

## Dependencies & Execution Order

```
Setup (T001–T003)
  └─ Foundational (T004–T006)
       └─ US5 (T007–T009)
            └─ US3 (T010–T012)
                 └─ US1 (T013–T018)
                      └─ US4 (T019–T021)
                           └─ Lambda delivery (T022–T025)  ← gate: behaviour-neutral on v1
                                └─ US2 app (T026–T035)     ← T026–T031 can be written earlier, merge only after T025
                                     └─ US6 (T036–T038) → Polish (T039–T040)
```

- The app work (T026–T033) may be *developed* in parallel with the Lambda phases. Only
  T034/T035 (PR and merge) wait for T025. That is FR-029's deploy order.
- Within each story, tests come first and must fail before the implementation task.

## Parallel examples

- Foundational: T004, T005 together, then T006.
- US5: T007 and T008 together.
- US1: T013 and T016 together (different files), then T014 → T015 and T017 → T018.
- App: T026, T027, T030, T031 together; then T028 → T029 → T032.

## Implementation strategy

1. **MVP (safe)**: through T009 plus the Lambda delivery tasks. The pipeline is unchanged but
   version-aware, so it can be deployed on its own.
2. **Lambda feature-complete**: US3 + US1 + US4, one Lambda PR (T024).
3. **Switch-over**: the app PR (T034/T035) turns v2 on. Nothing else needs deploying.
4. **Measure**: T036–T038.
