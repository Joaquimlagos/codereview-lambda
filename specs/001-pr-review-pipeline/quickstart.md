# Quickstart: Validating the AI PR Review Pipeline

Phase 1 output for [plan.md](./plan.md). Run these to prove the feature works end-to-end, per
SC-001–SC-005. Setup commands are documented in `CLAUDE.md`'s Quick Start section; this guide
documents the validation *scenarios* and what each proves.

## Prerequisites

- LocalStack running with S3 and SSM enabled:
  `docker run -d --name codereview-localstack -p 4566:4566 -e SERVICES=s3,ssm localstack/localstack:3.0`
  (see `CLAUDE.md`). **Use the `:3.0` tag, not `:latest`** — newer LocalStack versions require a
  `LOCALSTACK_AUTH_TOKEN` even for community services like S3/SSM, and this project has no
  license; `:3.0` (community edition) needs no token.
- Python 3.14 environment with the project's dependencies installed (`boto3`, `pydantic`,
  `requests`, `pytest`) — see `CLAUDE.md`'s Quick Start section.
- Local stub implementations of Jev, Gemini, and GitHub active (default in the test/dev
  configuration; no real credentials needed) per Principle IV / FR-011. Gemini and Groq are
  called directly by `InvokeLLM` — no separate routing service to stand up. For a real (non-stub)
  run, populate `.env` from `.env.example` with `TYPESAFE_API_KEY`, `TYPESAFE_API_BASE`,
  `GEMINI_API_KEY`, `GEMINI_API_BASE`, `GROQ_API_KEY`, `GROQ_API_BASE`,
  `LLM_MODELS_LOW`/`MEDIUM`/`HIGH`, `GITHUB_APP_ID`,
  `GITHUB_APP_INSTALLATION_ID`, and `GITHUB_APP_PRIVATE_KEY_PATH` (the GitHub App's `.pem`,
  kept outside the repo — see the README's "GitHub App setup").
  The S3 bucket holding both the diff (`prs/`) and the RAG index (`index/`) is not a `.env`
  variable — it comes from the event itself (`diffBucket`, alongside `diffKey`; see
  `research.md`).

## Scenario 1 — End-to-end review on a stored diff (validates SC-001, User Story 1)

1. Seed LocalStack S3 with a fixture diff and note its bucket/key as `diff_bucket`/`diff_key`.
2. Terraform (`infra/`) publishes each Lambda's ARN to SSM natively at
   `/codereview/lambda/{state}/arn` (kebab-case state slugs: `route-model`,
   `retrieve-context`, `invoke-llm`, `post-comment` — confirmed against `codereview-infra`'s
   `lambda_arns.tf`, see `research.md`), satisfying FR-010 as part of `terraform apply` —
   no separate publish step.
3. Invoke `RouteModel` with a `PullRequestEvent` referencing that `diff_bucket`/`diff_key`, with
   the Jev stub configured to return `needsContext: false`.
4. Feed its output straight into `InvokeLLM` (skipping `RetrieveContext`, per FR-005), then feed
   `InvokeLLM`'s output into `PostComment`.
5. **Expected**: the GitHub stub records exactly one posted comment for the PR, containing
   non-empty review text derived from the fixture diff.

## Scenario 2 — Complexity-based routing (validates SC-002, User Story 2)

1. Invoke `RouteModel` with a small/low-risk fixture diff; **expected**: `complexity: "low"`.
2. Invoke `RouteModel` with a large/high-risk fixture diff; **expected**: `complexity: "high"`.
3. Feed both outputs into `InvokeLLM`; **expected**: the Gemini stub records a lighter-weight
   model selected for the `"low"` case than for the `"high"` case.

## Scenario 3 — Context-enriched review (validates User Story 3)

1. Invoke `RouteModel` with a fixture diff and a Jev stub configured to return
   `needsContext: true`.
2. Seed the same LocalStack bucket with a fixture `index/develop/index.json` (the shape
   `codereview-app`'s `scripts/build_index.py` publishes) whose `model`/`dimensions` match the
   embedding stub's, then feed RouteModel's output into `RetrieveContext` with that stub;
   **expected**: `indexAvailable: true` and up to 3 `chunks`, ordered by similarity to the
   embedded diff (`chunks` MAY be empty per the spec Edge Case).
3. Feed both outputs into `InvokeLLM`; **expected**: the LLM stub call records the retrieved
   chunks alongside the diff, and its prompt carries them under a section labelled as reference
   material rather than as part of the diff.
4. Re-run step 2 against a bucket with **no** index object; **expected**: `indexAvailable: false`
   with empty `chunks` and no error — the review still proceeds, just without project context.
5. Re-run step 2 against an index whose `model` or `dimensions` disagree with the embedding
   client's; **expected**: the run fails with `IndexCompatibilityError` rather than ranking
   across incompatible vector spaces.

## Scenario 4 — Fallback on routing failure (validates SC-005)

1. Configure the Jev stub to raise/return an invalid response.
2. Invoke `RouteModel`.
3. **Expected**: output is still the well-formed fallback `{"complexity": "medium",
   "needsContext": false}` (see `research.md`), and the run can proceed to `InvokeLLM` rather
   than halting.

## Scenario 5 — No network, full suite (validates SC-003, SC-004)

1. Run `pytest` (see `CLAUDE.md`'s Commands section) with all integrations pointed at their
   local stubs / LocalStack.
2. **Expected**: full suite passes, including `tests/contract` (every stage's I/O validated
   against the shared `pydantic` models — SC-004), with zero calls to any real external network
   endpoint (SC-003).

## Manual GitHub verification (optional, real environment only)

Only when running against a real (non-stub) GitHub API and a disposable test PR: confirm the
comment from Scenario 1 actually renders on that pull request. Not required for the automated
suite, which asserts against the GitHub stub instead.
