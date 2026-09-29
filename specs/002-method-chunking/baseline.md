# Baseline: RAG with one chunk per file (before method chunking)

**Feature**: [spec.md](spec.md) · **Recorded**: 2026-09-28 · **Raw data**: [baseline-results.json](baseline-results.json)
(first run per PR, with similarities) and [baseline-runs.json](baseline-runs.json) (all runs)
· **Scripts**: [trigger_runs.py](trigger_runs.py), [measure_review.py](measure_review.py)

Sections 1–4 describe the **first run** of each PR in detail. Section 5 adds two more runs
per PR and gives the numbers the "after" measurement is compared against: **3 runs per
PR**.

This is the "before" half of the before/after comparison for `002-method-chunking`. The
"after" measurement MUST use the same three PRs and the procedure in
[How to repeat this measurement](#how-to-repeat-this-measurement), unchanged.

## Configuration under test

| Component | Value |
|---|---|
| codereview-lambda | `develop` @ `10f109a` (merge of PR #7, Cerebras fallback + project-logger fix), deployed 2026-09-28T17:11–17:12Z, all four functions `CodeSha256 = 6Y0dTN0DTPpHN0tjpHnlA49l9buCHHvr1YsZHRVq0b8=` |
| Tier model lists (InvokeLLM env) | **low** `groq:openai/gpt-oss-120b:low, cerebras:gpt-oss-120b:low, gemini:gemini-3.5-flash:low` · **medium** `groq:openai/gpt-oss-120b:medium, cerebras:gpt-oss-120b:medium, gemini:gemini-3.5-flash:low` · **high** `gemini:gemini-3.5-flash:high, cerebras:gpt-oss-120b:medium, groq:openai/gpt-oss-120b:medium` |
| RAG index | `index/develop/index.json`, `version: 1`, built from codereview-app `develop` @ `88801e4`, `generatedAt 2026-09-28T03:11:55Z`, S3 version `Gbq3QxTKViw51IFuKCo5BwzgPQs_zodO` |
| Index contents | 16 chunks = 16 files (one chunk per whole file), `gemini-embedding-001`, 768 dims |
| Retrieval | the whole PR diff embedded once (`RETRIEVAL_QUERY`), top-3 files by cosine similarity, full file text in the prompt |

The latest PR #3 and #7 runs before this recording (2026-09-28 ~03:45Z) ran on the
*previous* Lambda deployment, and they lack the `answered … usage=` log line that the logger
fix added. Both PRs were therefore re-run on the current deployment by an empty commit on
their branches. PR #8 already had a run on the current deployment.

| PR | Branch | Trigger commit | Execution | Started (UTC-3) |
|---|---|---|---|---|
| #3 (planted auth defects) | `feature/auth-resilience` | `5ae38ce` (empty) | `209bc841…` | 2026-09-28 14:46:19 |
| #7 (small) | `test/task-title-validation` | `d528765` (empty) | `7df059d3…` | 2026-09-28 14:46:21 |
| #8 (large, 11 files) | `test/projects-module` | `cf32768` (existing head) | `ea864ec9…` | 2026-09-28 14:13:24 |

All three executions `SUCCEEDED`, all with `needsContext: true`.

The six additional runs in section 5 (2026-09-28 18:55–19:04Z) ran on the deployment that
followed PR #9 (`CodeSha256 AG6+S/49t9aU0bSlb9oDO1b5bk34rocBiMCtu259c1M=`), still against
the same v1 index object. On a v1 index that deployment is byte-for-byte the code above:
the same retrieval and the same prompt. research.md R13 checked this on PR #7, and every
extra run below reproduced the first run's top-3 and prompt size exactly.

## 1. Prompt tokens by section

Counted by rebuilding the exact prompt with `build_prompt` from the deployed commit, using
the diff from S3 and the context chunks from the execution's own state input. Tokenizer:
`o200k_base`, the base vocabulary of gpt-oss's `o200k_harmony`. **Provider total** is
`prompt_tokens` from the answering model's own log line. The consistent +67 to +71 gap is the
chat template the provider wraps around the user message, so the section counts are exact up
to that overhead.

| PR | Instructions | Diff | RAG context | Sum | Provider total | Context share |
|---|---:|---:|---:|---:|---:|---:|
| #3 | 688 ¹ | 1,676 | 794 | 3,157 | 3,224 (Cerebras) | 25% |
| #7 | 525 | 1,451 | 1,212 | 3,187 | 3,258 (Groq) | 38% |
| #8 | 525 | 6,531 | 726 | 7,781 | 7,848 (Cerebras) | 9% |

¹ Includes the 163-token security checklist, added because PR #3 touches `auth/` paths.

The same prompts counted with Gemini's own `countTokens` (`gemini-3.5-flash`), as they would
be charged if Gemini had answered:

| PR | Instructions | Diff | RAG context | Total |
|---|---:|---:|---:|---:|
| #3 | 714 | 2,128 | 1,020 | 3,861 |
| #7 | 541 | 1,867 | 1,537 | 3,944 |
| #8 | 541 | 8,052 | 914 | 9,506 |

**Calibration for any character-based estimate:** diff text measures 4.12–4.36 chars per
token on `o200k` (gpt-oss), but only **≈3.4** on Gemini's tokenizer, which counts 24–28%
more tokens for the same text. An estimator that must never under-count across all three
providers has to sit below 3.4 chars/token.

## 2. RAG selection

Cosine similarity of every indexed file against the diff. It was recomputed by re-embedding
each execution's diff with the same model, task type and dimensionality, against the index
version live at the time. Embeddings are deterministic, and the recomputed top-3 equals what
RetrieveContext actually returned in all three runs (`rag_reproduced: true`). **Bold** = returned.

| Rank | PR #3 | | PR #7 | | PR #8 | |
|---:|---|---:|---|---:|---|---:|
| 1 | **main/auth/AuthController** ² | **0.8271** | **test/tasks/TaskControllerTest** ² | **0.8004** | **main/tasks/TaskService** | **0.7889** |
| 2 | **main/auth/JwtValidator** ² | **0.8184** | **test/tasks/TaskServiceTest** ² | **0.7904** | **main/tasks/TaskController** | **0.7784** |
| 3 | **test/auth/JwtValidatorTest** ² | **0.7946** | **main/tasks/TaskService** ² | **0.7850** | **main/tasks/Task** | **0.7598** |
| 4 | test/auth/AuthControllerTest | 0.7940 | main/tasks/TaskController ² | 0.7812 | pom.xml | 0.7488 |
| 5 | main/auth/LoginResponse ² | 0.7873 | main/tasks/Task | 0.7501 | test/tasks/TaskServiceTest | 0.7473 |
| 6 | main/auth/InMemoryUsers ² | 0.7612 | test/auth/AuthControllerTest | 0.7262 | test/tasks/TaskControllerTest | 0.7440 |
| … | | | | | | |
| 16 (last) | main/CodereviewAppApplication | 0.6690 | resources/application.yml | 0.6542 | test/auth/JwtValidatorTest | 0.6732 |

² A file the PR itself modifies. The full 16-row ranking per PR is in
[baseline-results.json](baseline-results.json) (`rag_ranking`).

**Score distribution** (the "after" measurement reports the same columns over its own
candidates and top-N):

| PR | Candidates | Min | Max | Range | Top-N (N=3) mean / min | Rest mean / max | Margin at the cut (min top − max rest) | Mean gap (top − rest) | Standardised gap (mean gap ÷ σ of all) |
|---|---:|---:|---:|---:|---|---|---:|---:|---:|
| #3 | 16 | 0.6690 | 0.8271 | 0.1581 | 0.8134 / 0.7946 | 0.7177 / 0.7940 | **0.0006** | 0.0957 | 1.78 |
| #7 | 16 | 0.6542 | 0.8004 | 0.1462 | 0.7919 / 0.7850 | 0.6985 / 0.7812 | **0.0038** | 0.0935 | 1.93 |
| #8 | 16 | 0.6732 | 0.7889 | 0.1157 | 0.7757 / 0.7598 | 0.7195 / 0.7488 | **0.0110** | 0.0562 | 1.78 |

The standardised gap is the column to compare before and after. The "after" run changes
both N (3 → 8) and the candidate count (16 → ~50). Raw means and margins shift with those
alone, but the gap measured in standard deviations does not.

**The scores barely separate relevant from irrelevant.** Across all 16 files the range is
only 0.67–0.83 for every PR, and the #3/#4 gap in PR #3 is 0.0006. That is the expected
result when each vector averages a whole file: a threshold on these scores could not tell
useful context from noise.

**Silent truncation of the query.** `gemini-embedding-001` embeds at most 2,048 tokens and
silently drops the rest. Measured with the embedding model's own `countTokens`:

| PR | Diff in embedding tokens | Seen by the query | Dropped |
|---|---:|---:|---|
| #3 | 2,126 | 96% | the last ~80 tokens (end of `JwtValidatorTest`) |
| #7 | 1,848 | 100% | nothing |
| #8 | 7,918 | **26%** | 5,870 tokens: everything after `ProjectController.java` |

For PR #8, the cut falls 2 tokens before the end of the 5th of 11 files, so its retrieval
was decided by `CreateProjectRequest`, `InMemoryProjectRepository`,
`InvalidProjectException`, `Project` and `ProjectController` alone. `ProjectService` (the
change's core logic), `ProjectRepository`, `UpdateProjectRequest` and all three test files
never reached the query. The largest single-file diff is `ProjectServiceTest.java` at 1,816
tokens, under the limit but close.

**Redundancy.** For PR #3 and #7, all 3 retrieved files are files the PR itself modifies. The
context therefore repeats the pre-change version of code the diff already shows. In PR #7,
that is 1,212 tokens, 38% of the prompt.

## 3. Review output

| PR | Tier | Model that answered | Fell back? | Inline comments | Categories | Severities |
|---|---|---|---|---:|---|---|
| #3 | high | `cerebras:gpt-oss-120b:medium` | yes (Gemini `:high` → 503) | 4 | security ×4 | high ×2, medium ×2 |
| #7 | medium | `groq:openai/gpt-oss-120b:medium` | no | 2 | bug ×1, maintainability ×1 | medium ×1, low ×1 |
| #8 | high | `cerebras:gpt-oss-120b:medium` | yes (Gemini `:high` → 503) | 1 | bug ×1 | medium ×1 |

No run hit `parse_fallback`. PR #8's prompt (7,848) would not fit Groq's 8,000 TPM once
output is added. It is last in the high tier, so it was never tried.

## 4. PR #3 answer key

**Five defects, counted per defect, not per line**, as documented in the PR description and
the README. The fail-open's two `catch` blocks are one defect (validation that fails open).
The password written to the log on two lines is also one defect.

Detection rule: a defect counts as detected only if at least one inline comment names that
specific problem. Several comments on the same defect still count once. A mention only in
the summary, or a comment on the right line about something else, does not count.

| # | Defect (file) | Detected? | Comment |
|---|---|---|---|
| 1 | Validation fails open: both `catch` blocks in `isValid` return `true` (`JwtValidator`) | ✅ | `JwtValidator.java:55` security/high |
| 2 | 24 h clock-skew tolerance (`JwtValidator`) | ✅ | `JwtValidator.java:28` security/medium |
| 3 | Password logged in plaintext (`AuthController`) | ✅ | `AuthController.java:29` security/high |
| 4 | User enumeration via distinct messages (`AuthController`) | ✅ | `AuthController.java:28` security/medium |
| 5 | Inverted test assertion (`JwtValidatorTest`) | ❌ | no comment on the test file |

**Result for this first run: 4 of 5.** Only the inverted test was missed. The two
repeated runs in section 5 scored 2 of 5, so the baseline figure is the **mean over 3 runs:
2.67 of 5**, not this single run.

## 5. Repeated runs (3 per PR)

Each PR was re-run twice more, one review at a time with a 60 s gap
([trigger_runs.py](trigger_runs.py)), to keep Cerebras (5 requests/minute) out of its
limit. All nine runs used the same diff per PR and the same v1 index. Retrieval is
deterministic, so every run of a PR got the same top-3 and a byte-identical prompt:

| PR | Runs | Model (every run) | Prompt tokens (provider) | Top-3 (every run) |
|---|---|---|---:|---|
| #3 | `209bc841`, `7ad12e48`, `cf840128` | `cerebras:gpt-oss-120b:medium` (Gemini `:high` → 503 each time) | 3,224 | AuthController, JwtValidator, JwtValidatorTest |
| #7 | `7df059d3`, `10de2068`, `880195c1` | `groq:openai/gpt-oss-120b:medium` (first choice) | 3,258 | TaskControllerTest, TaskServiceTest, TaskService |
| #8 | `ea864ec9`, `6c549856`, `840e2f46` | `cerebras:gpt-oss-120b:medium` (Gemini `:high` → 503 each time) | 7,848 | TaskService, TaskController, Task |

**Only the model's answer varies.** Inline comments per run:

| PR | Run 1 | Run 2 | Run 3 | Mean | Range | σ |
|---|---:|---:|---:|---:|---|---:|
| #3 | 4 (security ×4; high ×2, medium ×2) | 3 (security ×3; high ×3) | 3 (security ×3; high ×3) | **3.33** | 3–4 | 0.47 |
| #7 | 2 (bug/medium, maintainability/low) | 1 (maintainability/low) | 1 (maintainability/low) | **1.33** | 1–2 | 0.47 |
| #8 | 1 (bug/medium) | 1 (bug/low) | 1 (bug/medium) | **1.00** | 1 | 0 |

PR #7 also has the T025 run (`f0ce8108`, same prompt, 1 comment, maintainability/low). It
is kept out of the mean so that every PR has the same n; including it gives 1.25.

**PR #3, detected defects per run** (answer key and rule from section 4):

| Run | 1. Fail-open | 2. 24 h skew | 3. Password in log | 4. Enumeration | 5. Inverted test | Detected |
|---|---|---|---|---|---|---:|
| `209bc841` | ✅ `:55` | ✅ `:28` | ✅ `:29` | ✅ `:28` | ❌ | 4 |
| `7ad12e48` | ✅ `:62` | ❌ | ✅ `:30`, `:37` (1 defect, 2 comments) | ❌ | ❌ | 2 |
| `cf840128` | ✅ `:55` | ❌ | ✅ `:29`, `:36` (1 defect, 2 comments) | ❌ | ❌ | 2 |
| **Rate** | 3/3 | 1/3 | 3/3 | 1/3 | 0/3 | **mean 2.67, range 2–4, σ 0.94** |

**Reading**: two defects are caught every time (fail-open, password in the log). Two are
caught only once in three (skew, enumeration), and the inverted test never. The single-run
4/5 in section 4 was the best of three, not the typical result. The "after" measurement is
compared against the mean and the per-defect rate, never against one run.

## Limitations to keep in mind when comparing

- **Three runs per PR, before and after.** The "after" measurement MUST use the same number
  of runs (3 per PR), triggered the same way (one at a time, 60 s apart), and compare
  means, ranges and PR #3's per-defect rate. With n=3 and PR #3's σ of 0.94 defects, only a
  difference of about one defect or more in the mean is worth reading as an effect.
- **The answering model is not controlled.** Gemini `:high` returned 503 on all six
  high-tier runs, so Cerebras answered every PR #3 and #8 review, and Groq every PR #7
  review. If an "after" run is answered by a different model than that PR's baseline runs,
  re-trigger it (up to 2 extra attempts) rather than compare across models.
- The index is rebuilt on every push to `develop`. The "after" index will be built from a
  later commit, so its `commit` must be recorded next to the results.

## How to repeat this measurement

Run from codereview-lambda's root, with the `codereview` AWS profile.

1. **Record the configuration** and paste it into the table at the top of the "after" file:
   ```sh
   export AWS_PROFILE=codereview
   for f in invoke-llm retrieve-context route-model post-comment; do
     aws lambda get-function-configuration --function-name codereview-$f \
       --query '[FunctionName,LastModified,CodeSha256]' --output text
   done
   aws lambda get-function-configuration --function-name codereview-invoke-llm \
     --query 'Environment.Variables.[LLM_MODELS_LOW,LLM_MODELS_MEDIUM,LLM_MODELS_HIGH]'
   aws s3 cp s3://codereview-artifacts/index/develop/index.json - \
     | python -c "import json,sys; i=json.load(sys.stdin); print(i['version'], i['commit'], i['generatedAt'], len(i['chunks']))"
   ```
2. **Trigger 3 runs per PR, one at a time.** `trigger_runs.py` pushes an empty commit
   (no checkout), waits for that PR's execution to finish, and waits 60 s before the next
   one, so Cerebras stays under 5 requests/minute. It takes about 15 minutes. Its lines give
   the execution prefixes for step 4:
   ```sh
   AWS_PROFILE=codereview python specs/002-method-chunking/trigger_runs.py \
     --app ../codereview-app --order 3 7 8 3 7 8 3 7 8 --label "method-chunking after"
   ```
3. **Check that every run `SUCCEEDED`** (the script prints the status), and that each PR was
   answered by the same model as its baseline runs (section 5; see Limitations).
4. **Run the script.** The Gemini key is needed only for the similarity recompute and the
   Gemini token counts. It is read from Secrets Manager and handed **only to the script's
   process**: an inline assignment on the same command line, never printed, never written
   to a file, never exported in the shell. `--env /dev/null` keeps the script from reading
   a stale key out of `.env`:
   ```sh
   pip install boto3 requests tiktoken pydantic   # in any venv
   # All 9 runs, without the key: comments, models and tokens need no Gemini call.
   AWS_PROFILE=codereview python specs/002-method-chunking/measure_review.py --env /dev/null \
     --out specs/002-method-chunking/after-runs.json <the 9 execution prefixes from step 2>
   # One run per PR with the key: the similarity recompute and Gemini token counts.
   AWS_PROFILE=codereview GEMINI_API_KEY="$(AWS_PROFILE=codereview aws secretsmanager \
       get-secret-value --secret-id codereview/gemini-api-key --query SecretString --output text)" \
     python specs/002-method-chunking/measure_review.py --env /dev/null \
       --out specs/002-method-chunking/after-results.json pr:3 pr:7 pr:8
   ```
   Keep the keyed run to one execution per PR: it makes about 7 Gemini calls per execution,
   and on the free tier 10 executions in a row hit HTTP 429. That quota is shared with the
   live pipeline.
   `pr:N` selects the most recent succeeded execution for PR N. The script needs the
   `build_prompt` from the commit being measured, so run it from a checkout of that commit.
   Once version 2 is live, the context section is whatever `build_prompt` emits for grouped
   chunks, and the section split still keys on the `=== DIFF UNDER REVIEW ===` and
   `=== ADDITIONAL PROJECT CONTEXT ===` markers. If the implementation renames them, update
   `DIFF_MARKER`/`CONTEXT_MARKER` in the script in the same PR.
5. **Score PR #3** by hand against the answer-key table above, using the same detection rule.
6. **Write `after.md`** with the same tables, including the score distribution (min, max,
   range, top-N vs rest, margin at the cut, standardised gap). The distribution is computed
   over all candidates scored *after* FR-020's exclusion. Take it from `rag_ranking` in the
   script output (the script ranks every candidate, not only the selected ones). Also
   compare the answering model per PR (see Limitations) before drawing any conclusion.
