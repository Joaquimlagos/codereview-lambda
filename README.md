# codereview-lambda
Harness serverless que roteia PRs entre modelos LLM (Gemini) conforme complexidade, usa RAG para contexto do projeto e comenta a análise automaticamente no PR.

## Model selection

`RouteModel` classifies each PR as `low`, `medium` or `high` complexity, and each tier maps to
an ordered fallback list of free-tier models from two providers, Groq and Gemini
(`LLM_MODELS_LOW/MEDIUM/HIGH`). Each entry is `provider:model[:reasoning]`, for example
`groq:openai/gpt-oss-120b:low`; the optional reasoning level is sent only when present.

`InvokeLLM` tries the entries in order. It moves to the next one when a model fails
transiently (HTTP 429/5xx or a timeout) or no longer exists (404); a permanent error such as a
bad request or key (400/401/403) stops immediately. Before each attempt it checks the Lambda's
remaining time and stops early with a retryable error rather than being cut off by Lambda's
timeout. If every entry fails transiently, Step Functions retries the whole step. The review
output records which model answered (`model_used`) and whether a fallback happened
(`fell_back`). Free-tier availability varies a lot between models; see
`specs/001-pr-review-pipeline/research.md`'s "Multi-provider model fallback" decision for the
measurements behind the current lists.

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
