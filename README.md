# codereview-lambda

[![CI](https://github.com/Joaquimlagos/codereview-lambda/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/Joaquimlagos/codereview-lambda/actions/workflows/ci.yml)

Serverless harness that routes pull requests to LLM models (Groq, Gemini) by complexity, uses RAG for project context, and automatically comments its review on the PR.

## Architecture

```text
[codereview-app]  GitHub Actions, on every pull request
   │
   ├─ uploads the diff ───────────────────────▶ S3  prs/{pr}/{sha}.diff
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
                       │      │            RAG: embeds the diff, ranks the project's index
                       │      │            (S3 index/), returns the 3 closest files
                       │      └─ no  ─▶ (skipped)
                       │
                       ├─ 3. InvokeLLM
                       │      tries the complexity tier's model list, in order:
                       │
                       │      Groq ──▶ Gemini      ◀─ FALLBACK 1: next provider when a model
                       │                              fails transiently (429 / 5xx / timeout),
                       │                              rejects the prompt as too large (413),
                       │                              or no longer exists (404)
                       │      every model down ──▶ LlmTransientError ──▶ Step Functions retries
                       │
                       └─ 4. PostComment
                              signs in as a GitHub App and posts one review, each comment
                              anchored to a line of the diff
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

| Repository | Role |
|---|---|
| [`codereview-app`](https://github.com/Joaquimlagos/codereview-app) | **Triggers.** A deliberately simple Java / Spring Boot "Task Manager" that exists to generate pull requests of different complexity. Its GitHub Actions authenticate to AWS with OIDC, upload each PR's diff to S3 and publish the `PRReviewRequested` event, and build the RAG index of the codebase. |
| [`codereview-infra`](https://github.com/Joaquimlagos/codereview-infra) | **Orchestrates.** Terraform for the glue between the services: the EventBridge bus and rule that receive the event, the Step Functions state machine that calls the four Lambdas in order, the shared S3 artifacts bucket, the (initially empty) Secrets Manager secrets, and the IAM role the app's workflows assume. |
| **`codereview-lambda`** (this repo) | **Executes.** The four Lambda functions that do the work (routing, retrieval, LLM review, posting the comment), with their least-privilege IAM roles, CloudWatch log groups, and the SSM parameters that publish their ARNs to the state machine. |

**At runtime** the chain is linear: the app **triggers**, infra **orchestrates**, lambda
**executes**.

**At deploy time** it isn't, because infra and lambda hand each other values through SSM
Parameter Store in both directions: infra publishes the secret ARNs and the bucket name the
Lambdas need, and the Lambdas publish the ARNs the state machine needs. The order is:

1. `codereview-infra`: the secrets and the artifacts bucket only (a targeted apply), then fill
   in the secret values.
2. `codereview-lambda`: apply. It reads those values from SSM, deploys the four Lambdas, and
   publishes their ARNs to `/codereview/lambda/<state>/arn`.
3. `codereview-infra`: full apply. It reads the Lambda ARNs and creates EventBridge, Step
   Functions and the OIDC role.
4. `codereview-app`: can now trigger the pipeline (its OIDC role only exists after step 3).

The exact commands are in [`codereview-infra`'s README](https://github.com/Joaquimlagos/codereview-infra#bootstrap-order)
("Bootstrap order").

## Why this exists

Reviewing every pull request by hand is slow, and much of a first pass is repetitive: the same
kinds of nitpicks and missed checks, PR after PR. This pipeline gives each PR an automated first
review. An LLM reads the diff (plus related project files when the change needs them) and leaves
comments on the exact lines it is talking about, so people can spend their review time on design
and intent. The review is advisory: it comments, and never approves or blocks a PR. It is a
portfolio project, built to run entirely on free-tier services.

**Built with spec-driven development**, as part of an AI-agent-assisted workflow: the feature
was specified, planned and broken into tasks with [GitHub Spec Kit](https://github.com/github/spec-kit)
(tooling in [`.specify/`](.specify), specification and design artifacts in
[`specs/001-pr-review-pipeline/`](specs/001-pr-review-pipeline)), governed by a
[constitution](.specify/memory/constitution.md), and implemented together with an AI coding
agent (Claude Code).

## Model selection

`RouteModel` classifies each PR as `low`, `medium` or `high` complexity, and each tier maps to
an ordered fallback list of free-tier models from two providers, Groq and Gemini
(`LLM_MODELS_LOW/MEDIUM/HIGH`). Each entry is `provider:model[:reasoning]`, for example
`groq:openai/gpt-oss-120b:low`; the optional reasoning level is sent only when present.

`InvokeLLM` tries the entries in order. It moves to the next one when a model fails
transiently (HTTP 429/5xx or a timeout), when the prompt is too large for that model (HTTP
413), or when the model no longer exists (404); a permanent error such as a bad request or key
(400/401/403) stops immediately. Before each attempt it checks the Lambda's remaining time and
stops early with a retryable error rather than being cut off by Lambda's timeout. If every
entry fails transiently, Step Functions retries the whole step. The review output records which
model answered (`model_used`) and whether a fallback happened (`fell_back`).

HTTP 413 is grouped with the transient failures so a review isn't aborted when the prompt (the
diff plus the retrieved RAG files) exceeds one model's limit. On Groq's free tier the limit is
per request: `gpt-oss-120b` allows 8,000 tokens per minute, and a single request larger than
that is refused with a 413 regardless of when it is sent. That is a limit of *that model*, so
the next entry in the list, from a provider with a larger limit, may accept the same prompt.
Retrying the same model would not help. (If every entry rejects the prompt as too large, Step
Functions' retry re-runs the same list with the same prompt and fails the same way; the way to
avoid that is a smaller prompt, not another attempt.)

Free-tier availability varies a lot between models; see
`specs/001-pr-review-pipeline/research.md`'s "Multi-provider model fallback" decision for the
measurements behind the current lists.

**The high tier gets a genuinely different configuration, not just the same one twice.**
`LLM_MODELS_HIGH` leads with Groq at `medium` (as low and medium do), then falls back to
Gemini at `thinkingLevel: "high"`. Gemini `high` measured 6 inline comments vs. 2 at `low` on
the same real prompt, a real difference in review depth; `high` effort on Groq itself reliably
exhausts its output budget on reasoning alone and returns nothing (tested directly), and even
Groq `medium` — the free tier's only viable Groq setting — has since been observed to fail
the same way non-deterministically (see research.md's "Multi-provider model fallback"), so
Gemini `high` is kept as the fallback that still delivers a deep review whenever Groq's first
attempt doesn't answer, without spending Gemini's slower, costlier call on every review. The
`high` Gemini call is also much slower (measured up to 63 s vs. 9–31 s at `low`), so it gets
its own longer timeout and the Lambda's overall timeout is 180 s rather than 150 s. See
research.md's "High-tier reasoning: why Gemini, not Groq" and its superseding note.

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

## Known limitations (current stage)

- **The RAG index only covers `develop`.** `RetrieveContext` reads a single index object,
  `index/develop/index.json`, published by codereview-app's `index-codebase` workflow. A PR
  targeting another branch is still reviewed against develop's snapshot of the codebase, and
  a PR opened before develop was ever indexed is reviewed with no project context at all
  (`indexAvailable: false` — a deliberate graceful degradation, not a failure).
- **Retrieval is whole-file, top-3, single-pass.** Each index entry is one whole file, ranked
  by cosine similarity against the embedded diff; there is no sub-file chunking, no reranking
  step, and no token budgeting beyond the fixed `TOP_K = 3`. A large retrieved file consumes
  prompt budget in full.
