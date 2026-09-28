# Baseline: RAG with one chunk per file (before method chunking)

**Feature**: [spec.md](spec.md) · **Recorded**: 2026-09-28 · **Raw data**: [baseline-results.json](baseline-results.json)
· **Script**: [measure_review.py](measure_review.py)

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

**Result: 4 of 5.** Only the inverted test was missed, the same gap the README records for
every post-checklist run. The previous deployment's run (`601cdd53`, Groq, 03:47Z) also
scored 4/5 (it split defect 1 across two comments, which still counts once). That is weak
evidence the score is stable across these two models.

## Limitations to keep in mind when comparing

- **One run per PR.** PR #3's history shows run-to-run variance (the inverted test was caught
  once, pre-checklist). Before/after on n=1 can only reveal large effects. Consider 2 runs
  per PR on each side if the difference turns out to be small.
- **The answering model is not controlled.** Gemini `:high` returned 503 on both high-tier
  runs, so Cerebras answered. If an "after" run is answered by a different model than its
  baseline row, re-trigger it (up to 2 extra attempts) rather than compare across models.
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
2. **Trigger the three reviews at the same time.** Use one empty commit per branch, pushed
   without checking anything out, from codereview-app's root:
   ```sh
   git fetch origin
   for b in feature/auth-resilience test/task-title-validation test/projects-module; do
     new=$(git commit-tree "origin/$b^{tree}" -p "origin/$b" \
       -m "chore: retrigger AI review (method-chunking after-measurement)")
     git push origin "$new:refs/heads/$b"
   done
   ```
3. **Wait until the three new executions are `SUCCEEDED`** (about 30 s to 2 min):
   ```sh
   aws stepfunctions list-executions --max-results 5 \
     --state-machine-arn arn:aws:states:us-east-1:424678835315:stateMachine:codereview-pr-review \
     --query 'executions[].[name,status,startDate]' --output text
   ```
4. **Run the script** with a valid Gemini key, needed for similarities and Gemini token
   counts. Everything else works without one:
   ```sh
   pip install boto3 requests tiktoken pydantic   # in any venv
   GEMINI_API_KEY=... python specs/002-method-chunking/measure_review.py \
     --out specs/002-method-chunking/after-results.json pr:3 pr:7 pr:8
   ```
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
