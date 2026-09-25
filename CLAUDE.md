@.specify/memory/constitution.md

# codereview-lambda

Lambda implementations for the PR-review pipeline. Principles and rationale live in the constitution import above — this file is operational only.

## Quick Start

```sh
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
```

```sh
docker run -d --name codereview-localstack -p 4566:4566 -e SERVICES=s3,ssm localstack/localstack:3.0
```

**Do not use `localstack/localstack:latest`** (or other recent tags) on this project: newer
LocalStack versions refuse to start — even for community services like S3/SSM — without a
`LOCALSTACK_AUTH_TOKEN` (a paid license). Confirmed directly: `:latest` fails with "License
activation failed"; `:3.0` (community edition) starts cleanly with no token. Stick to `:3.0`
unless/until this project has a LocalStack license.

```sh
cd infra && terraform init && terraform apply
```

This deploys to **real AWS** (no LocalStack provider endpoints — see `infra/provider.tf`). It
provisions each Lambda's execution role, builds a self-contained deployment zip per function
(code + vendored `pydantic`/`requests`, via `infra/build_package.sh` — `boto3` is preinstalled
by the Lambda runtime, not vendored), creates the Lambda function itself, and publishes its ARN
to SSM at `/codereview/lambda/{state}/arn` (`infra/arn_publish.tf`) — no separate manual publish
step. If your `python3` isn't the right interpreter for building the deployment package (e.g. on
Windows), set `-var python_bin=/path/to/python`.

## Structure

One folder per Lambda, one Lambda per Step Functions state (Principle II). Expected states, per the `codereview-infra` Step Functions definition:

- `route_model/` — decides which Gemini model/route to use (via Jev, Principle III)
- `retrieve_context/` — semantic retrieval: embeds the diff and ranks codereview-app's published index (S3 + Gemini embeddings)
- `invoke_llm/` — calls the Gemini API directly, selecting the model by complexity tier itself (no separate routing service, Principle III)
- `post_comment/` — posts the review result back (GitHub)

Each Lambda folder is self-contained: its own handler, own IAM policy reference, own tests.

External integrations (Jev, Gemini, S3, SSM, GitHub) live behind testable abstractions per Principle IV, in a shared `src/integrations/` package (one module per external system, each with a real client and a local stub) — not per-Lambda duplicates.

`infra/` holds this repo's Terraform: one execution role + `aws_lambda_function` per Lambda (`iam_route_model.tf`, `iam_retrieve_context.tf`, `iam_invoke_llm.tf`, `iam_post_comment.tf`), each least-privilege-scoped (only its own secret, if any; S3 read/write only where the handler actually needs it), plus native ARN publishing to SSM (`arn_publish.tf`), and one CloudWatch log group per Lambda with a fixed retention (see Log retention below).

## Commands

```sh
pytest
```

```sh
ruff check src tests
```

```sh
cd infra && terraform fmt -check -recursive && terraform init -backend=false && terraform validate
```

The Terraform half of CI (see below): checks formatting and validity without AWS credentials, the S3 backend, or a built package.

```sh
cd infra && bash build_package.sh
```

Builds the deployment package by hand (`infra/.build/package/` + `infra/.build/lambda_src.zip`) without running Terraform — useful for inspecting/measuring the artifact. `terraform apply` runs this automatically as part of provisioning (see Quick Start above).

## CI

`.github/workflows/ci.yml` runs on pushes to `main` and on every pull request (same triggers as `codereview-infra`) with two parallel jobs — `python` (3.14: `pip install -e ".[dev]"`, `ruff check src tests`, `pytest`) and `terraform` (1.15.8: `fmt -check -recursive`, `init -backend=false`, `validate` in `infra/`). Checks only: no deploy, no AWS access, no external APIs, no secrets (`permissions: contents: read`). The tests use local stubs only, so they need no `.env`, credentials or network — keep it that way: a test that needs any of those belongs outside this suite.

## Log retention

The four `/aws/lambda/codereview-*` log groups are managed by Terraform with `retention_in_days = 7` (`log_retention_days` in `infra/locals.tf`); each `aws_cloudwatch_log_group` sits next to its function in `infra/iam_*.tf`, and function names are defined once in `locals.tf` (`local.function_names`). Step Functions has no log group of its own (logging is off in `codereview-infra`). The groups pre-existed, so they were adopted once with `import` blocks that have since been removed on purpose — don't re-add them: an `import` of a missing object is an error and would break `terraform plan` on a fresh deployment (where Terraform just creates the groups).

## Related repos

- `codereview-infra` — defines the Step Functions state machine and publishes the ARN naming contract via SSM, which this repo's Lambdas read to resolve their own resources.
- `codereview-app` — the application under review. Its GitHub Actions upload each PR's diff to the shared artifacts bucket and publish the pipeline's trigger event (`pr-checks.yml`), and build the RAG embedding index that `RetrieveContext` reads (`index-codebase.yml`).
