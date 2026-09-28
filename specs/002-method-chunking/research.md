# Research: Method-Level Chunking for RAG Context

**Feature**: [spec.md](spec.md) · **Plan**: [plan.md](plan.md) · **Date**: 2026-09-28

Each entry: **Decision**, **Rationale**, **Alternatives considered**. Entries marked
*verify during implementation* name a check a task has to perform, because the answer
cannot be settled by reading documentation.

## R1. Java parser and chunk counts

- **Decision**: `tree-sitter==0.25.2` + `tree-sitter-java==0.23.5`, pinned, installed in
  `index-codebase.yml` with `pip install` (approved). Chunking walks
  `class_declaration`, `record_declaration`, `interface_declaration` and `enum_declaration`
  nodes, and chunks `method_declaration`, `constructor_declaration` and
  `compact_constructor_declaration` children.
- **Rationale**: javalang cannot parse Java 21 `record`. A probe during planning parsed all
  14 `.java` files on codereview-app `develop@88801e4` (3 of them records) with zero
  `ERROR` nodes. It also parsed all 25 files on `test/projects-module` (PR #8). Both
  packages ship prebuilt wheels for CPython 3.12 on Linux, the runner's version, so no
  compiler is needed.
- **Trivial-method rule**: a method is trivial when its body is empty (`{}`), is a single
  `return <field>;` / `return this.<field>;`, or is a single `this.<field> = <param>;`. A
  constructor is trivial when every statement is `this.<field> = <param>;`. Compact record
  constructors are never trivial.
- **Counts under that rule** (planning probe):

  | Commit | Java files | Methods + constructors | Non-trivial |
  |---|---:|---:|---:|
  | `develop@88801e4` | 14 | 38 | 34 |
  | `test/projects-module@cf32768` | 25 | 112 | 100 |

- **Reconciliation with the feasibility test's 59 chunks.** The feasibility script (since
  deleted) ran while `README.md` was still indexed, and counted:

  | Kind | Feasibility test | Now (`develop@88801e4`) |
  |---|---:|---:|
  | Java methods (non-trivial) | 35 | 34 (this rule) |
  | Java files with no methods (records) → `type` chunks | 3 | 3 (`LoginRequest`, `LoginResponse`, `Task`) |
  | Markdown sections | 8 | 0: `README.md` is no longer indexed |
  | XML blocks (`pom.xml`) | 10 | to be counted by the dry run (T032) |
  | YAML blocks (`application.yml`) | 3 | to be counted by the dry run (T032) |
  | **Total** | **59** | ~50 expected (34 + 3 + ~13) |

  The one-method difference fits a slightly different trivial rule, or a method removed
  since then. It is too small to matter for sizing.
- **FR-003's reference: the dry run (T032)**, `python scripts/build_index.py --dry-run` on
  codereview-app `develop@88801e4` with the implemented chunker:

  | Kind | Count | Notes |
  |---|---:|---|
  | `method` | 34 | the planning probe's count; the feasibility test's 35 minus one |
  | `type` | 4 | the 3 records (`LoginRequest`, `LoginResponse`, `Task`) plus `CodereviewAppApplicationTests`, whose only method (`contextLoads() {}`) is empty, hence trivial |
  | `block` | 3 | `pom.xml` ×2, `application.yml` ×1 (the feasibility test cut XML into 10 and YAML into 3 with a smaller target size; this packs blank-line paragraphs up to ~400 estimated tokens) |
  | **Total** | **41** | 0 split into parts; largest 361 estimated tokens (`pom.xml#L31-65`) |

  Against the feasibility test's 59: −1 method, +1 type, −8 Markdown sections (README no
  longer indexed), −10 XML blocks and +2 −3 YAML blocks from the different block size.
- **tree-sitter 0.25.2, not 0.26.0** (found while implementing T026): on Windows / CPython
  3.12.2, 0.26.0 corrupted memory while `chunk_java` walked a large (400-statement) method.
  Access violations surfaced at random points of unrelated Python code (a `str.split`, a
  dataclass constructor) in 6–11 of 20 runs of the same script. Keeping the `Language`
  object alive made no difference (11/20). The same script under 0.25.2 crashed 0 of 20
  times, and the full suite is green. Not verified on Linux (Docker was unavailable); the
  PR's `index-script-tests` run on ubuntu will exercise it.
- **Alternatives**: javalang (no records); a regex-based splitter (breaks on nested types,
  annotations, and lambdas with braces); running a JVM-based parser (JavaParser) on the
  runner (a Java step in a Python script, for no gain over tree-sitter).

## R2. Header (context prefix) format

- **Decision**: the header is plain Java-shaped text: `package …;`, then for each enclosing
  type from the outermost down, its declaration line up to `{` (annotations, modifiers,
  `extends`/`implements`, record components), then that type's field declarations, one
  per line, with initialisers kept only when they fit on one line. The header is stored
  once per chunk (`header`) and embedded as `header + "\n" + text`.
- **Rationale**: plain source reads naturally to both the embedding model and the reviewer
  model, and repeating the header in every chunk is what makes a method understandable on
  its own. Storing it separately lets the prompt print it once per file (FR-027) instead of
  once per chunk.
- **Alternatives**: imports in the header (noise that costs tokens and adds no meaning to a
  method); a synthetic summary ("method X of class Y") (loses field types, which are what
  make a method's code readable).

## R3. Token estimation and split thresholds

- **Decision**: one estimator on both sides: `ceil(len(text) / 3.0)`.
  - Index side: split any chunk estimated above **1,800** tokens.
  - Query side: split any per-file diff estimated above **1,800**, at hunk boundaries.
  - Prompt budget: the same estimator (FR-025).
- **Rationale**: measured on this codebase, gpt-oss (o200k) gives 4.12–4.36 chars/token and
  Gemini's tokenizer about 3.4 (diffs; `baseline.md` §1). 3.0 is below every measured
  ratio, so it never under-counts here. The worst real size of a chunk that passes the
  1,800 check is about 1,590 embedding tokens, well inside 2,048.
- **Alternatives**: `countTokens` API calls per chunk (one extra call per chunk, which
  defeats batching); bundling a tokenizer in the Lambda (a new dependency and a larger zip,
  and still the wrong tokenizer for two of the three providers).

## R4. Batch embedding API

- **Decision**: `POST {base}/models/gemini-embedding-001:batchEmbedContents` with
  `{"requests": [{"model": "models/gemini-embedding-001", "content": {"parts": [{"text": …}]},
  "taskType": "RETRIEVAL_DOCUMENT" | "RETRIEVAL_QUERY", "outputDimensionality": 768}, …]}`,
  at most 100 requests per call. The response `embeddings[i].values` is aligned with
  `requests[i]`. Both repos validate `len(embeddings) == len(requests)` and every vector's
  length, and fail loudly otherwise.
- **Rationale**: the feasibility test measured a 9.4× wall-clock gain. It also removes the
  1 s pacing sleep per file.
- **Quota accounting**: it is unknown whether a batch counts as 1 request or N against the
  daily quota (1,000 RPD, R15). *Verify during implementation (T035)*: AI Studio's rate-limit
  page shows the **peak over 28 days**, not the day's total, so it cannot answer this.
  Compare the **daily chart** of `gemini-embedding-001` requests for the day of the first
  real v2 build against the days around it. A jump of ~1 per build means per call, a jump of
  ~41 means per input. Write the result here. The design is sized for the worst case (N):
  41 inputs per build today, plus K per review (one per changed file).
- **Batches are limited by count only, not by tokens.** The 30,000 TPM limit (R15) is the
  constraint that binds first: today's whole index is ~5,100 estimated tokens in one call,
  but a batch of 100 chunks at the 1,800-token split threshold would be ~180,000. See T041.
- **Alternatives**: keep `embedContent` with parallel calls (hits the RPM limit, and no
  quota benefit).

## R5. Retry policy (index build)

- **Decision**: retry on HTTP 429, HTTP 503, socket timeouts and `URLError`. Up to 5 attempts
  per call, with a delay of `min(60, 2 * 2**attempt) + random(0, 1)` seconds. When the
  response carries a `Retry-After` header, or a `google.rpc.RetryInfo.retryDelay` in the
  error body (Gemini's 429 format), that value is used instead. A 5-minute ceiling covers
  the whole build. Any other HTTP status fails immediately. On exhaustion, the build exits
  non-zero before writing `index.json`, so the upload step never runs and the previous index
  stays.
- **Rationale**: 429 and 503 are the two statuses actually seen from Google's API in this
  project (research 001). Fail-fast on 4xx keeps a wrong key or bad request from burning
  five minutes.
- **Alternatives**: retrying every 5xx (500 and 502 were never observed here; they can be
  added later with evidence); retrying in the workflow YAML (it would re-run the whole build
  and re-spend quota).

## R6. Workflow concurrency

- **Decision**:
  ```yaml
  concurrency:
    group: index-codebase-${{ github.ref }}
    cancel-in-progress: true
  ```
- **Rationale**: only the newest `develop` state matters. Cancelling an older run saves
  quota, and the upload (`aws s3 cp`, a single PUT) is atomic, so a cancelled run never
  leaves a half-written index.
- **Alternatives**: `cancel-in-progress: false`, which queues runs. It is also correct, but
  it spends a full build of quota on a commit that is already superseded.

## R7. Query side: per-file diff splitting and fusion

- **Decision**: split the diff on `^diff --git ` boundaries, one query per file. A file whose
  section is estimated above 1,800 tokens is split at `@@` hunk boundaries into consecutive
  parts, each prefixed with the file's `diff --git`/`---`/`+++` lines. A single hunk still
  above the threshold is split by lines. All queries go in one `batchEmbedContents` call per
  100. A chunk's score is `max(cosine(chunk, q) for q in queries)` (FR-019), and the log
  records the argmax query's path.
- **Rationale**: in the baseline, PR #8's largest single-file diff is 1,816 embedding tokens,
  so per-file queries alone would already have covered it. Hunk splitting covers the
  remaining tail. Max-fusion keeps any one strongly matching file from being averaged away
  by the others.
- **Alternatives**: mean of query vectors (dilution again: the problem being fixed);
  reciprocal-rank fusion (better when score scales differ, but all queries here share one
  model and task type, so cosine scores are directly comparable); a per-query top-k quota
  (with 11 files and N=8 it degenerates to less than 1 chunk per file).

## R8. Changed-line overlap (FR-020)

- **Decision**: parse each hunk header `@@ -a,b +c,d @@` and walk its lines, tracking the
  old-side line number. A line starting with `-` marks old line `n` as changed. An insertion
  (a run of `+` lines with no `-` line immediately before it) marks the old-side position
  where it lands: the line before it (n-1) and the line after (n). Context lines (` `) mark
  nothing. A v2 chunk is excluded when `path` matches the diff's `a/` path and
  `[startLine, endLine]` intersects the changed set. Renamed files use the `a/` (old) path;
  new files (`--- /dev/null`) change nothing in the index.
- **Rationale**: the index mirrors `develop`, so the pre-change side is where the index's
  line numbers live. Marking both neighbours of an insertion catches an insertion at a
  method's first or last line without guessing which side it belongs to.
- **Index/merge-base drift**: the rule is exact only when the index `commit` is the PR's
  merge base. RetrieveContext does not know the merge base; it logs the index `commit` so
  the mismatch is visible after the fact. A mis-exclusion costs one context slot, never a
  failure.
- **Alternatives**: exclude whole changed files (rejected by the user: unchanged methods of
  a changed class are useful context); a text match of the chunk against removed lines
  (fragile with whitespace, and it misses insertions).

## R9. Provider budgets and the Groq accounting question

- **Decision**: prompt budgets per provider and effort live in `llm_router.py`, next to the
  output caps they are derived from:

  | Provider | Effort | Budget (estimated prompt tokens) | Derivation |
  |---|---|---:|---|
  | groq | medium | 4,300 | 8,000 TPM − 3,700 reserve (max measured output 3,668) |
  | groq | low / none | 6,800 | 8,000 − 1,200 (max measured 1,073) |
  | cerebras | any | 18,000 | 30,000 TPM − 12,000 `max_completion_tokens` |
  | gemini | any | 100,000 | a ceiling well under the 1M context window; *verify during implementation* against the current free-tier TPM for `gemini-3.5-flash` |

  Context cap: 3,000 estimated tokens, for all providers.
- **Groq's accounting is not a strict pre-flight on `prompt + max_completion_tokens`.**
  The baseline's PR #7 review (2026-09-28, `7df059d3`) was answered by Groq at `medium`
  with 3,258 prompt tokens and `max_completion_tokens: 5,500`: **8,758 reserved, and
  accepted.** `llm_router.py` also records 8,294 accepted (a 2,794-token prompt). Research
  001's two 413s (8,249 and 8,283 "requested") came from PR #8, whose prompt alone was
  ~7,000 tokens. The reading consistent with all three data points: the limit is charged
  on something closer to actual usage (prompt + output actually generated, possibly within
  a rolling minute), not on the reservation. The "pre-flight" description in research 001
  is therefore not reliable.
- **Decision: keep 4,300 as the starting point**, and let T036 decide with measurements.
  Send prompts of increasing estimated size (e.g. 3,500 / 4,300 / 5,000) at `medium` and
  record, for each, the prompt tokens, the output tokens and whether it was accepted. Note
  that with the 3.0 estimator, 4,300 estimated is only about 3,300 real gpt-oss tokens.
- **Why the Groq budget must not be set too low: Cerebras allows 5 requests per minute.**
  Every attempt Groq skips (FR-024) goes to Cerebras, the next entry in the medium tier and
  the second in the high tier. Cerebras' free tier is 5 RPM (research 001, "Cerebras as a
  third fallback provider"). A conservative Groq budget would route most medium-tier
  reviews to Cerebras. A handful of PRs pushed in the same minute, plus Step Functions'
  retries, would then exhaust Cerebras and push reviews on to Gemini `:low`, the last and
  weakest medium-tier entry, or fail them. The budget is a trade-off: too high costs one
  fast 413 from Groq; too low moves load onto the provider with the smallest request
  rate. T036 records how many of the three measured PRs would be skipped on Groq at the
  chosen value.
- **The estimate over-counts gpt-oss by ~44%, so the budgets above mix units.** Measured on
  the T025 run (R13): the PR #7 prompt that Groq accepted with **3,258** real
  `prompt_tokens` was estimated at **4,677** (`llm_attempt` log). That is a factor of 1.44.
  The same prompt measures 3,944 on Gemini's own `countTokens` (baseline.md §1), a factor of
  1.19. The derivations in the table subtract *real* output reserves from *real* TPM
  limits, but the result is compared against an *estimate*. In real tokens, Groq medium's
  4,300 is therefore only about 3,000. That is below the 3,258-token prompt Groq already
  accepts today, so under v2 packing Groq would drop context it would have accepted.
- **T036 must fix the units**, choosing one of:
  1. **Budgets in estimate units**: `budget = (TPM − output reserve) × factor` per
     provider, with the factor measured (gpt-oss ≈ 1.44, Gemini ≈ 1.19 on these prompts);
     or
  2. **A per-provider factor applied to the estimate**: `estimate / factor` compared
     against a budget kept in real tokens. This keeps the table readable in the providers'
     own units.

  Either way the estimate must stay pessimistic for the *embedding* split threshold (R3),
  where under-counting means silent truncation. Only the prompt budget gets the
  per-provider correction.
- **Rationale**: these numbers restate limits already measured in research 001. The budget
  only decides how much *context* to include, and a too-generous Groq budget costs at most
  one fast 413 before falling back, which is today's behaviour.
- **Alternatives**: one global budget (would starve Cerebras and Gemini to fit Groq);
  measuring provider tokens with a live `countTokens` call per attempt (latency, and no such
  endpoint on Groq).

## R10. Over-budget handling inside the router (FR-024)

- **Decision**: `MultiProviderLlmRouter.generate_review` builds the prompt per attempt:
  `pack_context(chunks, budget − estimate(instructions + diff))`. If the remaining room is
  below zero, it logs `Skipping <label> for PR <n>: estimated prompt <e> > budget <b>` and
  continues without calling the client. At exhaustion, if every entry was skipped, it
  raises `LlmPromptTooLargeError(LlmRouterError)`, which is non-retryable. Otherwise the
  existing classification applies, and skips count as neither "model gone" nor transient.
- **Rationale**: a new subclass of `LlmRouterError` (not of `LlmTransientError`) keeps
  Step Functions from retrying a run that can never fit. Its class name is new, so nothing
  in codereview-infra's Retry matcher changes.
- **Alternatives**: treating a skip as transient (Step Functions would retry a hopeless
  run); trimming the diff (out of scope in the spec).

## R11. Step Functions payload size

- **Decision**: no change needed. With N=8 and chunks capped at about 1,800 estimated tokens
  (5,400 chars), the worst-case `context` is about 8 × (5.4 KB text + ~1 KB header +
  metadata), roughly 55 KB, plus the event, well under the 256 KB state payload limit.
  Vectors are never passed between states.

## R12. Where tests for the index script run

- **Decision**: add `scripts/tests/` (stdlib `unittest`, plus tree-sitter) and a new workflow,
  `.github/workflows/index-script-tests.yml`, on `pull_request` with `paths: [scripts/**,
  .github/workflows/index-*.yml]`.
- **Rationale**: `pr-checks.yml` is the review-trigger workflow, and CLAUDE.md fences its
  `trigger-review` job. A separate workflow avoids touching it, and the path filter keeps
  it off Java-only PRs. It is not added to branch protection's required checks (only `test`
  gates merging), matching the repo's current policy.
- **Alternatives**: running the tests inside `index-codebase.yml` (too late: only after
  merge); adding a job to `pr-checks.yml` (widens a workflow whose scope CLAUDE.md
  deliberately keeps narrow).

## R13. Rollout check: the Lambda deploy is neutral on v1 (T025)

- **What was checked**: after PR #9 merged, `terraform apply` from `develop@ce7b3b2`
  deployed all four functions (`CodeSha256 AG6+S/49t9aU0bSlb9oDO1b5bk34rocBiMCtu259c1M=`,
  2026-09-28T18:46–18:47Z). The index stayed the baseline's S3 object (`version 1`,
  `88801e4`, version id `Gbq3QxTKViw51IFuKCo5BwzgPQs_zodO`). An empty commit (`0e7cb6a`) on
  PR #7's branch produced execution `f0ce8108`, measured with `measure_review.py pr:7`.
- **Result: identical to the baseline where the code decides.**

  | | Baseline (`7df059d3`) | T025 (`f0ce8108`) |
  |---|---|---|
  | Top-3 files, in order | TaskControllerTest, TaskServiceTest, TaskService | same |
  | Similarities | 0.8004 / 0.7904 / 0.7850 | same |
  | Prompt tokens (instructions / diff / context, o200k) | 525 / 1,451 / 1,212 | same |
  | Groq `prompt_tokens` | 3,258 | 3,258 |
  | `rag_*` log lines | — | none (the v1 path emits none) |
  | `llm_attempt` | — | 1 line: budget 4,300, estimated 4,677, 3 kept, 0 dropped, not skipped |

- **What differed**: the review itself. It had 1 inline comment (maintainability/low)
  instead of the baseline's 2, from a byte-identical prompt to the same model. That is pure
  run-to-run variance, and it is why the baseline was extended to several runs per PR
  (baseline.md).
- **Side finding**: the `llm_attempt` line exposed the estimate/real-token mismatch
  recorded in R9.


## R14. The high tier is answered by Cerebras in practice

- **Observation**: `LLM_MODELS_HIGH` leads with `gemini:gemini-3.5-flash:high`, but Gemini
  returned **HTTP 503** ("high demand") on **all six** high-tier runs of the baseline (PR #3:
  `209bc841`, `7ad12e48`, `cf840128`; PR #8: `ea864ec9`, `6c549856`, `840e2f46`;
  2026-09-28). Every one fell back to the second entry, `cerebras:gpt-oss-120b:medium`,
  which answered. So in practice the high tier today is Cerebras gpt-oss-120b at `medium`,
  not Gemini at `high`. Research 001's comparison (6 comments at Gemini `:high` vs. 2 at
  `:low` on the same prompt) describes a model that is not currently answering.
- **Cost of the current order**: each high-tier review spends one Gemini call (a fast 503,
  seconds, not the 90 s read timeout) before reaching the model that answers.
- **Decision: do not change the order now.** Re-evaluate the high tier's order only
  **after** the method-chunking "after" measurement (T038). Changing the model list now
  would change two variables at once, retrieval and the answering model, and the
  before/after comparison would no longer isolate the effect of chunking. The baseline and
  the "after" measurement both run with the order as configured today. baseline.md's rule
  (an "after" run answered by a different model than its PR's baseline runs is
  re-triggered) keeps the comparison on the same model even if Gemini recovers in between.
- **Gemini is not a reliable fallback at 20 requests per day (R15).** In the high tier it is
  the *lead* entry, so every high-tier review spends one of the 20 daily requests before
  reaching Cerebras, whether Gemini answers 503 or 429. In the low and medium tiers it is the
  *last* entry, the one meant to catch a Groq-and-Cerebras outage, and it may have no
  requests left for the day exactly when it is needed. The re-evaluation after T038 has to
  treat Gemini as best-effort rather than as a guaranteed step in any tier, and should
  consider whether it belongs in the lead position at all. Until then the order stays as
  it is (see the decision above). For the "after" measurement: when Gemini's daily quota is
  used up, it answers 429 instead of 503, and the review still falls through to Cerebras,
  the same model as the baseline runs.

## R15. Official free-tier limits (AI Studio, project `codereview`)

Read from the AI Studio rate-limit page of the `codereview` project on 2026-09-28. These
replace the estimates used earlier in this file and in research 001 for Google's models.

| Model | RPM | TPM | RPD | Observed |
|---|---:|---:|---:|---|
| `gemini-embedding-001` | 100 | 30,000 | 1,000 | 28-day peak **33.55K TPM**, above the limit |
| Gemini 3.5 Flash (`gemini-3.5-flash`) | 5 | 250,000 | **20** | 28-day peak **21 of 20** requests in one day; **8 of 20** on the last day (1-day view) |

- **The page's default view shows 28-day peaks, not the current day.** The "21 of 20" first
  recorded here as 2026-09-28's usage was the peak of one day within those 28 days. With the
  1-day interval, the page shows 8 of 20 for the last day. Any before/after comparison of usage
  (T035) must use the per-day chart, not these peak figures.
- **Embedding TPM is the binding limit for the index build.** The 33.55K peak shows the
  30,000 TPM limit has already been exceeded, at least momentarily. The build's retry on
  429 (R5) absorbs a short overshoot, but it is not a plan for a larger codebase, where
  one batch alone can exceed 30,000 tokens (R4). Hence **T041**: cap each batch by
  estimated tokens, not only by count, and wait between batches when the minute's budget
  is spent.
- **The review-time query side is far below the limits**: one batch call per review, one
  input per changed file. PR #8's 11 files are about 8,000 embedding tokens (baseline.md §2).
- **Gemini 3.5 Flash's 20 RPD is the scarcest resource in the pipeline**, and the high
  tier leads with it; consequences in R14. The quota has already been exceeded on at least
  one day in the last 28 (peak 21 of 20); the last day used 8. Every PR #3 and #8 run tries
  Gemini first, so each high-tier review costs one of the 20 whether it answers or not. Whether `countTokens`
  counts against RPD is not known, which is another reason to keep keyed measurement runs
  to one per PR (baseline.md, step 4).
- The research-001 budget ceiling for Gemini (100,000 estimated prompt tokens, R9) stays
  under its 250,000 TPM, so the budget table needs no change for Gemini's TPM. RPD, not
  TPM, is what limits Gemini.

## R16. Switch-over check: index version 2 published (T035)

- **Build**: codereview-app PR #16 merged into `develop` (`5a0eb9b`). `index-codebase`
  run #7 (9 s) installed `tree-sitter 0.25.2` and logged `41 chunks: 34 method, 4 type,
  3 block; 0 split into parts; largest 361 estimated tokens`, then `Embedded with 1 call(s),
  0 retr(y/ies)` and `Wrote index.json (version 2) with 41 chunks`. It uploaded at
  2026-09-28T20:03:46Z (S3 version `8oFWxwzN9wuEFdBcxzpBXFCqTl7c8fYt`, 461,253 bytes). The
  counts match the dry run recorded in R1, and this is also the first Linux run of the
  pinned tree-sitter.
- **Contract check**: the published object was checked against contracts/index-v2.md.
  Top-level fields are exactly the contract's: `version: 2`, `branch: develop`, `commit`
  equal to `develop`'s `5a0eb9b…`, `gemini-embedding-001`, 768. All 41 ids are unique and
  start with their `path`. Method and type ids end with their line range, block ids are
  `#L<start>-<end>`, and `symbol` matches `kind`. Every chunk has non-empty `text` and no
  `part`. Every vector has 768 floats, is non-zero, and all 41 are distinct. No embedded
  input is over the 1,800-token split threshold. **All checks passed.**
- **Read by the deployed Lambda code, offline** (no embedding call): `_index_version` returns
  2. `ranking.rank` with one chunk's own vector as the query ranks that chunk first at
  1.0000 and selects 8 of 41. The pre-002 v1 ranking (`_top_chunks`) also reads the index
  without error, which confirms the reversed-deploy-order safety net on the real object.
- **Rollback** is available: the previous v1 object is kept as S3 version
  `Gbq3QxTKViw51IFuKCo5BwzgPQs_zodO` (contracts/index-v2.md, "Rollback").
- **Quota accounting (R4)**, from the AI Studio daily view of `gemini-embedding-001`
  requests (RPD column, 1-day interval): **27 before the merge**; after the merge:
  *pending, AI Studio refreshes 15–30 min later*. A rise of ~1 means a batch counts as one
  request, ~41 means one per input. The last review before the merge was PR #16's own
  (execution `25ae08de`, 19:27Z, one v1 query embedding), and none ran after it, so if the
  "before" reading was taken after 19:28Z, the difference is the build alone.

