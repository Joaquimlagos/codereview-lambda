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
- `retrieve_context/` — fetches RAG context (S3)
- `invoke_llm/` — calls the Gemini API directly, selecting the model by complexity tier itself (no separate routing service, Principle III)
- `post_comment/` — posts the review result back (GitHub)

Each Lambda folder is self-contained: its own handler, own IAM policy reference, own tests.

External integrations (Jev, Gemini, S3, SSM, GitHub) live behind testable abstractions per Principle IV, in a shared `src/integrations/` package (one module per external system, each with a real client and a local stub) — not per-Lambda duplicates.

`infra/` holds this repo's Terraform: one execution role + `aws_lambda_function` per Lambda (`iam_route_model.tf`, `iam_retrieve_context.tf`, `iam_invoke_llm.tf`, `iam_post_comment.tf`), each least-privilege-scoped (only its own secret, if any; S3 read/write only where the handler actually needs it), plus native ARN publishing to SSM (`arn_publish.tf`).

## Commands

```sh
pytest
```

```sh
ruff check src tests
```

```sh
cd infra && bash build_package.sh
```

Builds the deployment package by hand (`infra/.build/package/` + `infra/.build/lambda_src.zip`) without running Terraform — useful for inspecting/measuring the artifact. `terraform apply` runs this automatically as part of provisioning (see Quick Start above).

## Related repos

- `codereview-infra` — defines the Step Functions state machine and publishes the ARN naming contract via SSM, which this repo's Lambdas read to resolve their own resources.
- `codereview-app` (not yet created) — will trigger the initial pipeline event via GitHub Actions.
