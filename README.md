# codereview-lambda

[![CI](https://github.com/Joaquimlagos/codereview-lambda/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/Joaquimlagos/codereview-lambda/actions/workflows/ci.yml)

Serverless harness that routes pull requests to LLM models (Groq, Cerebras, Gemini) by complexity, uses RAG for project context, and automatically comments its review on the PR.

## Architecture

```text
[codereview-app]  GitHub Actions, on every pull request
   │
   ├─ uploads the diff, minus review exclusions ─▶ S3  prs/{pr}/{sha}.diff
   └─ publishes PRReviewRequested (PR metadata + diff key only)
        │
[codereview-infra]
        ▼
   EventBridge ──▶ Step Functions state machine
                       │
[codereview-lambda]    │  (this repo: one Lambda per state)
                       │
                       ├─ 1. RouteModel
                       │      classifies the PR: complexity (low / medium / high) and
                       │      whether it needs project context
                       │
                       ├─ needsContext?
                       │      ├─ yes ─▶ 2. RetrieveContext
                       │      │            RAG: embeds each changed file's diff, ranks the
                       │      │            project's method-level index (S3 index/), and
                       │      │            returns the 8 closest methods, leaving out code
                       │      │            the diff itself changes
                       │      └─ no  ─▶ (skipped)
                       │
                       ├─ 3. InvokeLLM
                       │      tries the complexity tier's model list, in order:
                       │
                       │      low / medium:  Groq ──▶ Cerebras ──▶ Gemini
                       │      high:          Cerebras ──▶ Groq ──▶ Gemini
                       │
                       │      Groq      fast, free-tier baseline; leads low/medium
                       │      Cerebras  same gpt-oss-120b model, 30K TPM vs. Groq's 8K —
                       │                covers diffs too large for Groq; leads high
                       │      Gemini    last resort in every tier (20 requests/day)
                       │
                       │      context is packed per attempt into that provider's
                       │      prompt budget; a provider the diff alone overflows is
                       │      skipped without being called
                       │
                       │      FALLBACK 1: next provider when a model fails transiently
                       │      (429 / 5xx / timeout), rejects the prompt as too large
                       │      (413 on Groq, 429 on Cerebras), or no longer exists (404)
                       │      every model down ──▶ LlmTransientError ──▶ Step Functions retries
                       │
                       └─ 4. PostComment
                              signs in as a GitHub App and posts one review, each comment
                              anchored to a line of the diff (checked against the diff first:
                              moved to where its code_snippet is, or put in the review body)
                              │
                              └─ GitHub answers 422 (a comment's line isn't in the diff)
                                    ──▶ FALLBACK 2: one plain PR comment (summary + comments)
```

- **Fallback 1** keeps a review from failing because one model or provider is overloaded or
  retired; see [Model selection](#model-selection).
- **Fallback 2** keeps a review from being lost because one comment points at a line GitHub
  won't anchor to; see [Review comments](#review-comments).
- The state machine (including the `needsContext` branch and the retry on `LlmTransientError`)
  is defined in `codereview-infra`; this repository implements the four Lambdas it calls.

## Part of a 3-repo pipeline

This is one of three independent repositories that make up the pipeline:

| Repository | Role | Owns |
| --- | --- | --- |
| [`codereview-app`](https://github.com/Joaquimlagos/codereview-app) | **Triggers.** Sample Spring Boot app. Its GitHub Actions compute each PR's diff, upload it to S3, publish the `PRReviewRequested` event, and build the method-level RAG index. | The workflows (`pr-checks.yml`, `index-codebase.yml`, `index-script-tests.yml`) and the index builder (`scripts/`) |
| [`codereview-infra`](https://github.com/Joaquimlagos/codereview-infra) | **Orchestrates.** The AWS glue between the other two. | EventBridge bus and rule, Step Functions state machine, artifacts bucket, the five Secrets Manager secrets, the GitHub OIDC role, Terraform remote state |
| **[`codereview-lambda`](https://github.com/Joaquimlagos/codereview-lambda)** (this repo) | **Executes.** Classifies each PR, retrieves method-level RAG context, generates the review with Groq/Cerebras/Gemini fallback, and posts it as inline PR comments. | The four Lambdas, their IAM roles and CloudWatch log groups, and the SSM parameters that publish their ARNs |

**At runtime** the chain is linear: the app **triggers**, infra **orchestrates**, lambda
**executes**.

**At deploy time** it isn't: infra publishes the secret ARNs and the bucket name the Lambdas
need, and the Lambdas publish the ARNs the state machine needs, both through SSM Parameter
Store. The first deployment therefore runs in phases across the two repos; the phases and
commands are in [`codereview-infra`'s "Bootstrap order"](https://github.com/Joaquimlagos/codereview-infra#bootstrap-order).

## Why this exists

Reviewing every pull request by hand is slow, and much of a first pass is repetitive: the same
kinds of nitpicks and missed checks, PR after PR. This pipeline gives each PR an automated first
review. An LLM reads the diff (plus the most related methods of the project when the change needs them) and leaves
comments on the exact lines it is talking about, so people can spend their review time on design
and intent. The review is advisory: it comments, and never approves or blocks a PR. It is a
portfolio project, built to run entirely on free-tier services.

**Built with spec-driven development**, as part of an AI-agent-assisted workflow: the feature
was specified, planned and broken into tasks with [GitHub Spec Kit](https://github.com/github/spec-kit)
(tooling in [`.specify/`](.specify), specification and design artifacts in
[`specs/001-pr-review-pipeline/`](specs/001-pr-review-pipeline)), governed by a
[constitution](.specify/memory/constitution.md), and implemented together with an AI coding
agent (Claude Code). The second feature, method-level RAG
([`specs/002-method-chunking/`](specs/002-method-chunking)), was built the same way, spanning
this repo and `codereview-app`, and measured before and after with the same procedure.

## Live demo

[`codereview-app` PR #3](https://github.com/Joaquimlagos/codereview-app/pull/3) is a standing
demonstration: five security defects planted on purpose, presented as plausible-sounding work
("make authentication tolerant of clock skew between servers and improve login
diagnostics"). Nothing in the diff, the commit message, or the branch name hints that any of
it is intentional — the reviewer gets the same signal a real PR would give.

**Measured over 3 runs, before and after method-level RAG** (the
[baseline](specs/002-method-chunking/baseline.md) and
[after-measurement](specs/002-method-chunking/after.md) of `specs/002-method-chunking`,
2026-09-28). Within each side, every run got the same diff, the same retrieved context and
a byte-identical prompt, so only the model's answer varies. All six runs were answered by
the same model, `cerebras:gpt-oss-120b:medium`.

| Defect | Detected before (whole-file RAG) | Detected after (method-level RAG) |
|---|---|---|
| `JwtValidator.isValid` fail-open — both `catch` blocks return `true`, accepting an expired, malformed, or forged-signature token | 3 of 3 | 3 of 3 |
| Submitted password written to the log in plaintext | 3 of 3 | 3 of 3 |
| Clock-skew tolerance set to 24 hours, keeping expired tokens usable for a day | 1 of 3 | 1 of 3 |
| Different responses for "user not found" vs. "incorrect password" — user enumeration | 1 of 3 | **3 of 3** |
| `JwtValidatorTest`'s assertion inverted, so a rejected-token test now expects acceptance | 0 of 3 | 0 of 3 |
| **Mean per run** | **2.67 of 5** (range 2–4) | **3.33 of 5** (range 3–4) |

**Two defects are reliable**: the fail-open validation and the plaintext password are
flagged every time. **The inverted test assertion**, the subtlest of the five, was never
caught. **The one change is user enumeration**, from 1 of 3 runs to 3 of 3. That is a positive
signal, **not a conclusive one**: with 3 runs per side, a difference this size is below what
the measurement can confirm, and the other four defects kept exactly the same rates.

**Why three runs, not one.** Earlier single runs made the picture look better than it is:
after the security checklist (research.md's "Security checklist for auth-sensitive changes")
was added to the prompt, two separate runs each caught the enumeration, and one baseline run
found 4 of 5. Repeating the identical prompt showed that was the good end of the range, not
the typical result. Any change to the pipeline is measured against the mean over 3 runs,
never against a single review.

## Results

### Method-level RAG (`specs/002-method-chunking`)

The RAG index went from one entry per whole file to one entry per Java method, each with
its package, class declaration and fields as a header, built with tree-sitter in
`codereview-app`. Retrieval now embeds each changed file's diff separately, leaves out
methods the diff itself changes, and packs the top 8 into each provider's prompt budget.
Measured on `codereview-app` PRs #3, #7 and #8, 3 runs each, before and after, answered by
the same models:

**It improved what reaches the model:**

| | Before (whole files) | After (methods) |
|---|---|---|
| Share of PR #8's diff that shaped retrieval | 26% (the embedding model silently drops everything past 2,048 tokens) | **all 11 changed files**, one query each |
| Retrieved context repeating code the diff already shows (PRs #3, #7) | 3 of 3 files | **0** |
| RAG context on PR #7 | 1,212 tokens | **727 (−40%)**, whole prompt −15% |
| Attempts over their provider's budget | — | 0 of 15 |

**And kept review quality where it was:**

| PR | Inline comments per run, mean (before → after) | Planted defects found (PR #3) |
|---|---|---|
| #3 (planted auth defects) | 3.33 → 4.00 | 2.67 → 3.33 of 5 (enumeration 1/3 → 3/3; see [Live demo](#live-demo)) |
| #7 (small, 5 files) | 1.33 → 1.33 | — |
| #8 (large, 11 files) | 1.00 → 1.00 | — |

PR #3's gain is a positive signal that 3 runs per side cannot confirm, not a proven
improvement. One success criterion was **not met**: method-level scores do not separate
relevant from irrelevant code more sharply than whole-file scores did (standardised gap
1.78 / 1.93 / 1.78 → 2.12 / 1.72 / 1.37; the margin at the cut is still ~0.001). The gain
is in *what* is retrieved, not in cleaner scores. Details, per-run data and the
success-criteria table:
[`specs/002-method-chunking/after.md`](specs/002-method-chunking/after.md).

### Model fallback and review-quality rubric

Two earlier measured cases, against real `codereview-app` PRs:

- **[PR #8](https://github.com/Joaquimlagos/codereview-app/pull/8)** (11 files, +686 lines):
  previously failed with no review posted at all — Groq rejected the prompt as too large
  (413) and Gemini was overloaded (503), exhausting both entries in the then-two-provider
  high tier. With Cerebras added, the same PR is reviewed in ~7 s by
  `cerebras:gpt-oss-120b:medium`, flagging two real bugs: a `NullPointerException` risk in a
  sort comparator, and a non-atomic name-uniqueness check that lets concurrent requests race
  past it.
- **[PR #7](https://github.com/Joaquimlagos/codereview-app/pull/7)**: went from 7 inline
  comments — several purely complimentary ("which is appropriate", "good for consistency") —
  to 2, both real problems (a validation-ordering bug, a maintainability note), once the
  review-quality rubric required every comment to name a category and forbade praise as a
  comment entry.

## Model selection

`RouteModel` classifies each PR as `low`, `medium` or `high` complexity, and each tier maps to
an ordered fallback list of free-tier models from three providers, Groq, Cerebras and Gemini
(`LLM_MODELS_LOW/MEDIUM/HIGH`). Each entry is `provider:model[:reasoning]`, for example
`groq:openai/gpt-oss-120b:low`; the optional reasoning level is sent only when present. Groq
and Cerebras both serve the same `gpt-oss-120b` model — Cerebras was added as a second
`gpt-oss-120b` fallback with a much larger free-tier token ceiling (30,000 tokens/minute vs.
Groq's 8,000), for PRs whose diff alone is too large for Groq to accept at all.

`InvokeLLM` tries the entries in order. It moves to the next one when a model fails
transiently (HTTP 429/5xx or a timeout), when the prompt is too large for that model (HTTP
413 on Groq, HTTP 429 with `code: "token_quota_exceeded"` on Cerebras — a different status/
shape per provider, but both already fall into the same transient classification), or when
the model no longer exists (404); a permanent error such as a bad request or key (400/401/403)
stops immediately. Before each attempt it checks the Lambda's remaining time and stops early
with a retryable error rather than being cut off by Lambda's timeout. If every entry fails
transiently, Step Functions retries the whole step. The review output records which model
answered (`model_used`) and whether a fallback happened (`fell_back`).

HTTP 413/429-as-too-large is grouped with the transient failures so a review isn't aborted
when the prompt (the diff plus the retrieved methods) exceeds one model's limit. On Groq's
free tier the limit is checked per request, *before* the call runs: `gpt-oss-120b` allows
8,000 tokens per minute, and a single request larger than that is refused with a 413
regardless of when it is sent. Cerebras' equivalent limit is checked against *actual* usage
instead (confirmed with a real test call: a tiny prompt with a 29,000-token output cap still
succeeded, using only the tokens it actually generated) — either way, that is a limit of *that
model*, so the next entry in the list, from a provider with a larger limit, may accept the
same prompt. Retrying the same model would not help. (If every entry rejects the prompt as too
large, Step Functions' retry re-runs the same list with the same prompt and fails the same
way; the way to avoid that is a smaller prompt, not another attempt.)

Free-tier availability varies a lot between models; see
`specs/001-pr-review-pipeline/research.md`'s "Multi-provider model fallback" and "Cerebras as
a third fallback provider" decisions for the measurements behind the current lists.

**The high tier leads with Cerebras, and keeps Gemini `high` only as the last resort.**
`LLM_MODELS_HIGH` is Cerebras at `medium`, then Groq at `medium`, then Gemini at
`thinkingLevel: "high"`. Gemini `high` used to lead this tier, because it was the deeper
reasoning pass (6 inline comments vs. 2 at `low` on the same prompt). But it returned HTTP
503 on 12 of 12 measured high-tier reviews, each attempt costing a median 5.5 s and one of
its 20 free requests per day, so every review was really answered by the Cerebras fallback
anyway. Cerebras at `high` effort was tested on the real prompts: it spent its whole
12,000-token output budget on reasoning without answering, 4 of 4 times.

**The cost is explicit: in practice the high tier now runs the same model (`gpt-oss-120b`)
and reasoning effort (`medium`) as the medium tier**, only with Cerebras first. The
complexity classification still sets the fallback order and the time budget, but no longer
changes who answers.

Gemini `high` keeps its own longer timeout (90 s read, 95 s attempt budget). The worst case,
all three attempts failing, is 50 + 50 + 95 = 195 s, inside the Lambda's 230 s. With Step
Functions' one retry (`MaxAttempts: 1`, `IntervalSeconds: 30` in `codereview-infra`), the
worst case for a full review is about `2 × 230 s + 30 s ≈ 490 s` (8.2 minutes) before the
failure surfaces, which is fine for a non-blocking advisory check. See research.md's "High
tier: Cerebras first, Gemini last".

## Review quality

Every inline comment must name a `category` (`bug`, `security`, `performance`,
`maintainability`) and a `severity` (`low`, `medium`, `high`) — the prompt explicitly forbids
praise or a description of the code as a `comments` entry; that belongs in the summary only.
GitHub's Reviews API has no dedicated fields for either, so they're prefixed onto the posted
text: `**[security · high]** <the observation>`.

When any changed path looks auth/security-adjacent (matching
`auth|security|jwt|crypto|password|session|login|token`), the prompt adds a short, generic
security checklist — credential logging, user enumeration/timing differences, signature or
clock-skew bypass, missing authorization, hardcoded secrets — described as categories, not as
any specific PR's planted bugs. See research.md's "Review quality rubric" and "Security
checklist for auth-sensitive changes" decisions for the real reviews that motivated both.

## Review comments

`PostComment` posts the generated review as inline, diff-anchored comments — one GitHub
"review" (`event: "COMMENT"`, advisory only, never approves/blocks the PR) whose per-comment
`path`/`line` tie each observation to the exact line it's about, plus a top-level summary. If
any comment's line falls outside the diff, GitHub rejects the whole review (422); in that case
`PostComment` falls back to a single plain conversational comment (summary + observations
concatenated as text) rather than losing the review. See
`specs/001-pr-review-pipeline/research.md`'s "Inline review comments" decision.

**Where a comment's line comes from.** The model doesn't count lines. `InvokeLLM` sends the
diff with each line's post-change number printed in front of it (`integrations/diff_lines.py`),
and the model answers with that number plus the line's text:

```
  27| +    public List<Task> findOverdue(LocalDate today) {
  28| +        return tasks.values().stream()
  29| +                .filter(task -> !task.completed() && !task.dueDate().isAfter(today))
    | -        Task created = new Task(id, task.title(), task.description(), task.completed());
```

```json
{"path": "src/.../TaskService.java", "line": 29,
 "code_snippet": ".filter(task -> !task.completed() && !task.dueDate().isAfter(today))",
 "body": "...", "category": "bug", "severity": "high"}
```

Before posting, `PostComment` reads the same diff from S3 and checks each comment
(`post_comment/anchoring.py`). If `code_snippet` is on `line`, the comment is posted there.
If it is on another line of that file's diff, the comment moves to the matching line closest
to `line` and a `line_adjusted` line is logged; a snippet that only matches a removed line is
posted on that line's LEFT side. A comment without `code_snippet` is posted only if `line` is in
the diff. Anything else goes into the review's body under the summary instead of onto a line
it may not be about, and is logged as `comment_unanchored`. Before this, the model worked out
numbers from the `@@` header and on [PR #21](https://github.com/Joaquimlagos/codereview-app/pull/21)
put both comments 3 and 9 lines above the bug.

## GitHub App setup

`PostComment` authenticates as a GitHub App, so reviews appear as the App's bot account
(`<app-name>[bot]`) rather than as a person. It signs a short-lived JWT with the App's private
key, exchanges it for a 1-hour installation token, and caches that token until about 5 minutes
before it expires. See `specs/001-pr-review-pipeline/research.md`'s "GitHub App
authentication" decision for why this replaced a personal access token.

To create and connect the App:

1. **Create the App.** GitHub → Settings → Developer settings → GitHub Apps → New GitHub App
   (use the organization's settings instead if the repository belongs to an org). The App
   name becomes the bot's display name. Any homepage URL works. Untick **Webhook → Active**:
   the pipeline is triggered by `codereview-app`'s workflow, so the App needs no webhook.
2. **Grant one permission.** Under Repository permissions, set **Pull requests** to
   **Read and write** (Metadata: Read-only is added automatically). Nothing else is needed.
3. **Limit where it can be installed.** Choose "Only on this account", then create the App.
   Note the **App ID** shown on its settings page.
4. **Generate a private key.** On the same page, "Generate a private key" downloads a `.pem`
   file. Keep it outside the repository (`*.pem` is gitignored as a safety net).
5. **Install the App** on the repository being reviewed (`codereview-app`). The
   **installation ID** is the number at the end of the installation's settings URL
   (`github.com/settings/installations/<id>`).
6. **Store the key in Secrets Manager.** `codereview-infra` owns the secret
   `codereview/github-app-private-key` and publishes its ARN at
   `/codereview/secrets/github-app-private-key-arn`. Put the key in it with
   `aws secretsmanager put-secret-value --secret-id codereview/github-app-private-key
   --secret-string file://path/to/key.pem`.
7. **Configure this repo.** Set `github_app_id` and `github_app_installation_id` in
   `infra/terraform.tfvars` (see `terraform.tfvars.example`) and run `terraform apply`. For
   local runs, set `GITHUB_APP_ID`, `GITHUB_APP_INSTALLATION_ID` and
   `GITHUB_APP_PRIVATE_KEY_PATH` in `.env` (see `.env.example`).

The `codereview/github-token` secret still exists in `codereview-infra`, but `PostComment` no
longer uses it and its role can no longer read it.

## Continuous integration

`.github/workflows/ci.yml` runs on pushes to `main` and on every pull request (the same triggers
as `codereview-infra`, so a branch with an open PR is checked once, not twice), with two jobs in
parallel. It only checks: it never deploys, has no AWS access, calls no external API, and uses
no secrets (`permissions: contents: read`).

- **`python`**: Python 3.14 (the Lambda runtime version), `pip install -e ".[dev]"`,
  `ruff check src tests`, and `pytest`. The suite runs entirely against local stubs, so it needs
  no `.env`, credentials or network.
- **`terraform`**: Terraform 1.15.8 (`versions.tf` requires `>= 1.10`), then in `infra/`:
  `terraform fmt -check -recursive`, `terraform init -backend=false` (the S3 backend needs AWS
  credentials) and `terraform validate`. The build `null_resource` and the `archive_file` data
  source don't get in the way: `validate` neither runs provisioners nor reads data sources, so
  the deployment package doesn't need to be built first.

To run the same checks locally: `pytest`, `ruff check src tests`, and in `infra/`
`terraform fmt -check -recursive && terraform init -backend=false && terraform validate`.

## Log retention

Each Lambda's CloudWatch log group (`/aws/lambda/codereview-<function>`) is managed by Terraform
with `retention_in_days = 7`. AWS creates these groups automatically on a function's first
invocation, with no expiry, so without this the logs would be kept, and billed, forever. Every
function's Terraform file (`infra/iam_*.tf`) declares its group and the function depends on it;
the function names are defined once in `infra/locals.tf`, so a function and its log group can't
drift apart. To change the retention, edit `log_retention_days` in `infra/locals.tf`.

Step Functions has no log group of its own to manage: the state machine, owned by
`codereview-infra`, has logging turned off.

**History:** the groups already existed when Terraform took them over, so they were adopted into
the state once with `import` blocks (in a temporary `infra/imports.tf`, since deleted). The
blocks are not kept on purpose: an `import` of an object that doesn't exist is an error, so they
would make `terraform plan` fail on a fresh deployment, where Terraform simply creates the
groups.

## Structured logs

Besides plain-text lines, the Lambdas log one JSON object per line for the pipeline's
CloudWatch dashboard (in `codereview-infra`) and the measurement scripts in `specs/`. Lambda
prefixes each line with `[INFO]`, the timestamp and the request id, so Logs Insights queries
extract the fields with `parse`.

| `event` | Logged by | Fields |
| --- | --- | --- |
| `route_decision` | RouteModel, once per run | `pr`, `tier`, `needs_context`, `source` (`jev` or `fallback`), `jev_ms`, `error` (exception class name on a fallback) |
| `rag_query` | RetrieveContext, once per v2 run | `pr`, `queries`, `candidates`, `selected`, `excluded_overlapping`, `scores` (`min`, `max`, `mean_selected`, `min_selected`, `margin_at_cut`, …) |
| `rag_chunk` | RetrieveContext, once per selected chunk | `pr`, `rank`, `id`, `score`, `matched_query` |
| `llm_attempt` | InvokeLLM, before each attempt | `pr`, `model`, `budget`, `estimated_prompt`, `context_chunks_kept`/`dropped`, `skipped` |
| `llm_call` | InvokeLLM, after each call that reached a provider | `pr`, `tier`, `model`, `attempt`, `fell_back`, `outcome` (`ok`, `transient`, `truncated`, `model_not_found`, `permanent`), `http_status`, `llm_ms`, `finish_reason`, `prompt_tokens`, `output_tokens` (reasoning included), `reasoning_tokens` |
| `review_posted` | PostComment, once per posted review, only when the state machine passes `timing.startTime` | `pr`, `comments`, `fallback_422` (posted as one plain comment after a 422), `elapsed_ms` (from the start of the execution) |
| `line_adjusted` | PostComment, once per comment moved to another line | `pr`, `path`, `original_line` (the model's), `line`, `side` (`RIGHT`, or `LEFT` for a removed line), `has_snippet` |
| `comment_unanchored` | PostComment, once per comment put in the review body instead of on a line | `pr`, `path`, `original_line`, `reason` (`path_not_in_diff`, `line_not_in_diff`, `snippet_not_found`), `has_snippet` |

These lines carry identifiers, counts and timings only: never the prompt, the diff, the
review's text, a provider's error body or a key (`tests/unit/test_structured_logs.py` checks this). The
plain-text `… answered: … usage=…` and `Model … failed for PR …` lines are kept unchanged,
because `specs/002-method-chunking/measure_review.py` parses them.

## Known limitations (current stage)

- **The RAG index only covers `develop`.** `RetrieveContext` reads a single index object,
  `index/develop/index.json`, published by codereview-app's `index-codebase` workflow. A PR
  targeting another branch is still reviewed against develop's snapshot of the codebase, and
  a PR opened before develop was ever indexed is reviewed with no project context at all
  (`indexAvailable: false` — a deliberate graceful degradation, not a failure).
- **Retrieval scores barely discriminate.** Method-level retrieval fixed coverage and
  redundancy, but every candidate still scores within ~0.2 of the others, and the gap
  between the 8th and 9th chunk is ~0.001. The top 8 is set by the prompt budget, not by a
  visible relevance cliff. There is no reranking step.
- **Each index rebuild spends one embedding request per chunk.** Batching does not reduce
  quota: a full rebuild of today's 41 chunks costs 41 of `gemini-embedding-001`'s 1,000 free
  requests per day, and it runs on every push to `develop`. Incremental indexing is in the
  backlog (`specs/002-method-chunking/tasks.md`).
- **The high tier answers with the same model and effort as medium** (`gpt-oss-120b` at
  `medium`); see [Model selection](#model-selection).
- **Review quality is measured on 3 runs per side and 3 PRs.** That is enough to see that
  quality held, not to prove a small improvement.
