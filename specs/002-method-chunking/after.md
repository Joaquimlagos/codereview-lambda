# After: RAG with method-level chunks (index version 2)

**Feature**: [spec.md](spec.md) · **Baseline**: [baseline.md](baseline.md) · **Recorded**:
2026-09-28 · **Raw data**: [after-runs.json](after-runs.json) (all 9 runs, from the logs) and
[after-results.json](after-results.json) (one keyed run per PR: recomputed ranking and Gemini
token counts)

This file follows baseline.md's procedure, unchanged: 3 runs per PR, triggered one at a time
60 s apart with [trigger_runs.py](trigger_runs.py), and measured with
[measure_review.py](measure_review.py).

## Configuration under test

| Component | Value |
|---|---|
| codereview-lambda | `develop` (after #9, #10, #11), all four functions `CodeSha256 = 28k9lSkKa4jIMjPUd78ncMPjV8F2m/PlNExArv18kTo=` (deployed 2026-09-28T19:47–19:48Z) |
| Tier model lists | **identical to the baseline** (low / medium / high as in baseline.md) |
| RAG index | `version 2`, built from codereview-app `develop` @ `5a0eb9b`, `generatedAt 2026-09-28T20:03:43Z`, S3 version `8oFWxwzN9wuEFdBcxzpBXFCqTl7c8fYt`, 41 chunks (34 method, 4 type, 3 block) |
| Retrieval | one query per changed file (hunk-split above 1,800 estimated tokens), chunks overlapping changed lines excluded, top 8 by best similarity, context packed per model attempt |

| PR | Runs (execution) | Started (UTC-3) |
|---|---|---|
| #3 | `9b9d0dc0`, `10288a1c`, `0451a4ef` | 17:27, 17:32, 17:37 |
| #7 | `81c69bce`, `eacf9dd3`, `4b3fb0c8` | 17:29, 17:34, 17:39 |
| #8 | `d46f541b`, `02765bd8`, `a6dfcf4c` | 17:30, 17:36, 17:41 |

All 9 `SUCCEEDED`. **Every run was answered by the same model as that PR's baseline runs.**
Gemini `:high` returned 503 on all six high-tier runs again, so Cerebras answered PR #3 and
#8, and Groq answered PR #7. No re-trigger was needed. As in the baseline, retrieval is
deterministic: all three runs of a PR got the same chunks and a byte-identical prompt.

## 1. Prompt tokens by section

Same method as baseline.md §1. For v2 context, the prompt is rebuilt the way the answering
model received it (`pack_prompt` within its budget).

| PR | Instructions | Diff | RAG context (before → after) | Total, provider (before → after) | Context share (before → after) |
|---|---:|---:|---|---|---|
| #3 | 688 | 1,676 | 794 → **631** | 3,224 → **3,061** (Cerebras) | 25% → 21% |
| #7 | 525 | 1,451 | 1,212 → **727** | 3,258 → **2,773** (Groq) | 38% → 27% |
| #8 | 525 | 6,531 | 726 → **682** | 7,848 → **7,804** (Cerebras) | 9% → 9% |

Eight method chunks cost less than three whole files. On PR #7 the context shrank by 40% and
the whole prompt by 15%.

**Context budget (FR-023–FR-026)**: no attempt went over its budget, and none was skipped
(`llm_attempt` lines). Groq on PR #7 estimated 3,793 against 4,300, keeping all 8 chunks.
Cerebras estimated 4,415 (PR #3) and 11,133 (PR #8) against 18,000, also keeping all 8.

## 2. RAG selection

| PR | Queries (changed files) | Excluded as overlapping changed lines | Candidates | Selected |
|---|---|---:|---:|---:|
| #3 | 5 (5 files) | 6 | 35 | 8 |
| #7 | 5 (5 files) | 6 | 35 | 8 |
| #8 | **13 (11 files; 2 hunk-split)** | 0 (all 11 files are new) | 41 | 8 |

**The query covers the whole diff now.** In the baseline, PR #8's single query saw 26% of
its diff. Now every one of its 11 files contributes a query (SC-002), and the largest query
is under the 1,800-token threshold. The two largest test files were split at hunk
boundaries.

**No selected chunk repeats changed code** (SC-007). In the baseline, all 3 files retrieved
for PR #3 and PR #7 were files the PR itself modifies. Now the 6 overlapping chunks are
excluded in both, and the context is other methods of those classes plus their tests:

- **PR #3**: `JwtValidator.extractUsername`, `JwtValidator` (constructor),
  `JwtValidator.generateToken`, three `JwtValidatorTest` methods,
  `AuthControllerTest.loginWithValidCredentialsReturnsToken`, `LoginRequest`.
- **PR #7**: `TaskController.create`/`getById`, `TaskService.findById`, and five
  `TaskControllerTest`/`TaskServiceTest` methods.
- **PR #8**: `Task` and seven `TaskControllerTest`/`TaskServiceTest` methods. The
  `projects` package is new, so the closest existing code is the `tasks` module it mirrors.

**Scores come from the logs and match a recompute exactly** (SC-010). The keyed cross-check
re-ran the Lambda's own `split_queries` → batch embedding → `rank` for one run per PR. It
gave the same top-8 in the same order, with a maximum score difference of 0.0000 against
the `rag_chunk` lines.

**Score distribution** (baseline.md §2's columns, over all candidates after exclusion):

| PR | Candidates | Min | Max | Selected mean / min | Rest mean / max | Margin at the cut | Standardised gap (before → after) |
|---|---:|---:|---:|---|---|---:|---|
| #3 | 35 | 0.6246 | 0.8277 | 0.7894 / 0.7691 | 0.6640 / 0.7685 | 0.0006 | 1.78 → **2.12** |
| #7 | 35 | 0.6226 | 0.8022 | 0.7860 / 0.7728 | 0.6903 / 0.7718 | 0.0010 | 1.93 → **1.72** |
| #8 | 41 | 0.6415 | 0.8211 | 0.7875 / 0.7765 | 0.7219 / 0.7756 | 0.0009 | 1.78 → **1.37** |

**SC-012 is not met.** Method chunks do not separate relevant from irrelevant context more
sharply than whole files did. The standardised gap improved on PR #3 but fell on PR #7 and
PR #8. The margin at the cut is still about 0.001 everywhere, so choosing N stays a matter
of budget, not of a visible score cliff. The gain from chunking is in *what* is retrieved
(no duplicated diff code, the whole diff queried, a smaller context), not in cleaner score
separation.

## 3. Review output

| PR | Model (all runs) | Inline comments (before) | Inline comments (after) | Mean (before → after) | Categories after |
|---|---|---|---|---|---|
| #3 | `cerebras:gpt-oss-120b:medium` | 4, 3, 3 | 4, 3, 5 | 3.33 → **4.00** | security ×11, maintainability ×1 |
| #7 | `groq:openai/gpt-oss-120b:medium` | 2, 1, 1 | 2, 1, 1 | 1.33 → **1.33** | bug ×2, maintainability ×2 |
| #8 | `cerebras:gpt-oss-120b:medium` | 1, 1, 1 | 1, 1, 1 | 1.00 → **1.00** | bug ×3 (high ×1, medium ×2) |

No run hit `parse_fallback`. PR #3's extra fifth comment (`0451a4ef`) flags the
`LoginResponse` record gaining a field as an API-contract change (maintainability/medium).
That is a real observation, but not one of the planted defects.

## 4. PR #3 answer key

Same key and rule as baseline.md §4: 5 defects, counted per defect.

| Run | 1. Fail-open | 2. 24 h skew | 3. Password in log | 4. Enumeration | 5. Inverted test | Detected |
|---|---|---|---|---|---|---:|
| `9b9d0dc0` | ✅ `:53`, `:57` | ❌ | ✅ `:28` | ✅ `:30` | ❌ | 3 |
| `10288a1c` | ✅ `:58` | ❌ | ✅ `:29` | ✅ `:28` | ❌ | 3 |
| `0451a4ef` | ✅ `:58` | ✅ `:28` | ✅ `:29` | ✅ `:32` | ❌ | 4 |
| **Rate after** | 3/3 | 1/3 | 3/3 | **3/3** | 0/3 | **mean 3.33, range 3–4, σ 0.47** |
| Rate before | 3/3 | 1/3 | 3/3 | 1/3 | 0/3 | mean 2.67, range 2–4, σ 0.94 |

**SC-006 is met**: the mean went from 2.67 to 3.33 (≥ the baseline). The whole difference is
one defect, user enumeration, now caught in 3 of 3 runs instead of 1 of 3. The worst run
also improved (2 → 3). The other four defects have exactly the same rates, and the inverted
test is still never caught.

**How much to read into it**: +0.67 in the mean is below the "about one defect" that
baseline.md's Limitations set as the threshold for n=3. It is a consistent direction, not a
proven effect. The mechanism is plausible but unproven: the retrieved context now includes
`LoginRequest` and `AuthControllerTest.loginWithValidCredentialsReturnsToken`, code about
what the login endpoint returns, instead of the pre-change `AuthController` the diff
already showed.

## Success criteria

| Criterion | Result |
|---|---|
| SC-001 No embedding input over 2,048 tokens | ✅ index: largest chunk 361 estimated tokens, 0 split; queries: all ≤ 1,800 estimated (PR #8 hunk-split 2 files) |
| SC-002 PR #8: every changed file contributes a query | ✅ 11 of 11 files (13 queries), vs 26% of the diff before |
| SC-003 Index build no slower than before | ✅ one batch call; the whole workflow took 9 s (research R16) |
| SC-004 Transient errors don't publish a partial index | ✅ by test (stub server); no failure in the real build |
| SC-005 Every attempt's context within budget, no 413 from context | ✅ all 15 attempts within budget, no 413 |
| SC-006 PR #3 mean ≥ baseline | ✅ 3.33 vs 2.67 (below the n=3 significance threshold; see §4) |
| SC-007 0 selected chunks overlapping changed lines | ✅ 0 (6 excluded on PR #3 and #7) vs 3 of 3 files before |
| SC-008 0 reviews failed because of the index version | ✅ 0 across the rollout (T025, T035, and these 9 runs) |
| SC-009 PR #7 / #8 comments reported | ✅ 1.33 → 1.33, 1.00 → 1.00: unchanged |
| SC-010 Scores from the logs, no recompute needed | ✅ recompute agrees exactly (max difference 0.0000) |
| SC-011 No provider called over its budget | ✅ no skips needed, none over budget |
| SC-012 Standardised gap higher than baseline | ❌ 2.12 / 1.72 / 1.37 vs 1.78 / 1.93 / 1.78 |

## Limitations

- **n = 3 per side.** The only change in PR #3's defects is one defect's rate (1/3 → 3/3),
  and PRs #7 and #8 did not change at all. A larger n, or more PRs with known answers,
  would be needed to call the review-quality effect proven.
- **Retrieval and budget changed together**, by design of this feature: the context is
  both different (methods, no duplicated diff code) and smaller. This measurement cannot
  tell which of the two drove PR #3's change.
- **One answering model per PR.** Cerebras answered every PR #3 and #8 review on both
  sides, and Groq every PR #7 review. The result says nothing yet about Gemini `:high`,
  which never answered (research R14).
