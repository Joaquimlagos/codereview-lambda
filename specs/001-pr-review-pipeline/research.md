# Research: AI PR Review Pipeline

Phase 0 output for [plan.md](./plan.md). Each item resolves a Technical Context unknown or a
notable implementation choice; no open `NEEDS CLARIFICATION` markers remain.

## Language & runtime version

- **Decision**: Python 3.14, deployed as the native AWS Lambda `python3.14` managed runtime.
- **Rationale**: The constitution mandates Python-per-state Lambdas but not a version. AWS Lambda
  made `python3.14` generally available on 2025-11-18 (not a preview runtime — that distinction
  currently applies to `python3.15`, announced separately as a public-preview runtime and
  excluded from consideration here). LocalStack's Lambda runtime table
  (`localstack-core/localstack/services/lambda_/runtimes.py`, `main` branch) lists `python3.14` in
  both `RUNTIMES_AGGREGATED` (actively tested) and `IMAGE_MAPPING`, alongside `python3.13` and
  `python3.12`, with no preview/experimental marking — so 3.14 is the newest GA runtime with
  confirmed, mature LocalStack emulation, satisfying Principle V without a custom runtime/
  container packaging step.
- **Alternatives considered**: 3.13 (also GA and LocalStack-supported, but not the newest such
  option); 3.12 (older, no longer the most current GA+LocalStack-supported choice); 3.15
  (rejected — AWS-published public preview only, no SLA/support commitment, unsuitable as a
  deployment target); a container image runtime (rejected — adds packaging complexity with no
  benefit at this scale).

## Contract validation approach

- **Decision**: `pydantic` models in `src/contracts/models.py`, one per stage input/output,
  shared by all four handlers and by the `tests/contract` suite.
- **Rationale**: FR-012 requires each stage's I/O to conform exactly to the Step Functions
  contract with no adapter logic, and SC-004 requires this to be verified by automated tests.
  A single shared model set makes drift between stages impossible to introduce silently and gives
  a readable, self-documenting schema for a portfolio reviewer (Principle VI).
- **Alternatives considered**: Hand-rolled `dataclasses` + manual validation (more boilerplate,
  weaker guarantees); JSON Schema files validated at runtime (more indirection than needed at this
  scale, harder to read as Python).

## External-call abstraction shape

- **Decision**: One module per external system under `src/integrations/` (`decision_engine.py`
  for Jev, `llm_router.py` for Gemini (called directly, no separate routing service — see
  constitution.md Principle III), `storage.py` for S3, `config.py` for SSM, `github.py` for
  the GitHub API), each exposing a small interface plus a real client and a network-free stub
  implementing the same interface.
- **Rationale**: Principle IV requires every external integration to sit behind a testable
  abstraction with a local stub, and FR-011 requires every such dependency to be replaceable for
  testing. Grouping by external system (rather than by Lambda) means the four handlers share one
  definition per dependency instead of duplicating client code, resolving the "shared vs.
  per-Lambda" question left as a TODO in `CLAUDE.md`.
- **Alternatives considered**: Per-Lambda copies of each client (rejected — duplicates code across
  four folders and risks the stub and real client drifting apart); a single catch-all
  `integrations.py` (rejected — mixes unrelated concerns and makes it harder to find a given
  integration's stub for testing, hurting Principle VI clarity).

## Local AWS stand-in for tests

- **Decision**: LocalStack for S3 and SSM in `tests/integration`; direct stub objects (no network,
  no LocalStack) for Jev, Gemini, and GitHub in `tests/unit` and `tests/integration`.
- **Rationale**: Principle V requires the full pipeline to be runnable via LocalStack for the AWS
  services it actually uses (S3, SSM); Jev/Gemini/GitHub are non-AWS third parties with no
  LocalStack equivalent, so Principle IV's stub requirement covers them instead. SC-003 requires
  zero real network calls across the whole automated suite, which this split satisfies for both
  AWS and non-AWS dependencies.
- **Alternatives considered**: `moto` (in-process AWS mocking) instead of LocalStack — viable, but
  LocalStack matches the constitution's explicit wording ("LocalStack MUST be able to stand in for
  all AWS services") and lets `quickstart.md` describe one real running LocalStack instance rather
  than a test-only mocking library, which is easier for a first-time reader to reproduce manually.

## SSM ARN registry key format (FR-010)

- **Decision**: The path is `/${project_name}/lambda/{state}/arn` — e.g.
  `/codereview/lambda/route-model/arn` — with `{state}` kebab-case matching the Lambda's
  function name (`route-model`, `retrieve-context`, `invoke-llm`, `post-comment`). **No
  `{environment}` segment.** Published natively by Terraform (`infra/arn_publish.tf`, one
  `aws_ssm_parameter` per function, `value = aws_lambda_function.<name>.arn`) as part of the
  same `terraform apply` that creates the function — not by a separate post-deploy script.
- **Rationale**: Confirmed directly against `codereview-infra`'s current `lambda_arns.tf`, which
  reads from exactly `/${var.project_name}/lambda/route-model/arn` (and the sibling
  `retrieve-context`, `invoke-llm`, `post-comment` paths), with `project_name` defaulting to
  `codereview` (`variables.tf`). **This path changed upstream once already**: an earlier version
  of `codereview-infra` used `/${project_name}/${environment}/lambda/{state}-arn` (with an
  environment segment and a `-arn` suffix instead of a `/arn` segment), which this repo
  previously implemented via a manual `scripts/publish_arns.py` step — now removed, since it
  published to the stale path. `variables.tf`'s `environment` variable description still
  references the old, no-longer-accurate path pattern (a `codereview-infra`-side doc-drift issue,
  out of this repo's scope to fix). The ASL definition (`statemachine/definition.asl.json.tpl`)
  uses PascalCase (`RouteModel`, `RetrieveContext`, ...) only for Step Functions state names — a
  separate namespace from the SSM path segment, which `codereview-infra` always builds in
  kebab-case. Using anything else at this path would make `codereview-infra`'s
  `data.aws_ssm_parameter` lookups fail to resolve.
- **Alternatives considered**: PascalCase (`RouteModel/arn`) — rejected, does not match
  `lambda_arns.tf`'s literal lookup keys; keeping the old `{environment}`-segmented,
  `scripts/publish_arns.py`-driven path — rejected, no longer matches `codereview-infra` at all.

## Fallback behavior when routing is unavailable

- **Decision**: `RouteModel` catches Jev unavailability/invalid-response and returns the
  Assumptions-specified fallback decision (`complexity: medium`, `needsContext: false`) rather
  than raising, so the run still reaches `InvokeLLM`. (Field name reconciled against
  `codereview-infra`'s ASL, which branches on `$.routing.needsContext` — see
  contracts/step-io-contracts.md's authoritative-source note for the rename history.)
- **Rationale**: Directly implements the spec's documented Assumption and Edge Case ("pipeline
  MUST still reach a review outcome rather than fail silently or hang") and SC-005.
- **Alternatives considered**: Letting the Step Functions retry/catch handle it at the state
  machine level — rejected for this repo's scope, since the fallback value is a decision-content
  concern owned by `RouteModel`, not an orchestration concern owned by `codereview-infra`.

## Non-secret config resolution (DIFF_BUCKET)

- **Decision**: `DIFF_BUCKET` (and any future non-secret, environment-specific config) resolves
  via `resolve_config_value(env_var, ssm_param, backend=None)` in `src/integrations/config.py`:
  local `.env`/env var first, otherwise a plain `ssm:GetParameter` read (not Secrets Manager) at
  `/codereview/s3/pr-diffs-bucket-name` (published by `codereview-infra`'s `s3.tf`), cached in a
  module-level dict. Same real/stub-backend and env-var-first shape as `resolve_api_key` in
  `integrations/secrets.py`, kept as a separate function because this isn't a secret — no
  Secrets Manager involved, no IAM `secretsmanager:GetSecretValue` needed for it.
- **Rationale**: FR-009 requires config to come from externally supplied configuration, not
  hardcoded; the bucket name is environment-specific (differs between LocalStack/local, and any
  future real AWS environment) and is already published by `codereview-infra`, so resolving it
  the same way as the ARN registry and the three secrets avoids a fourth ad-hoc config mechanism.
- **Alternatives considered**: Reusing `resolve_api_key` directly (rejected — its name and
  Secrets-Manager-specific backend would misdescribe a plain SSM parameter read, and would imply
  an IAM `secretsmanager:GetSecretValue` grant that isn't needed for this value); a required env
  var with no SSM fallback (rejected — would force every deploy to hardcode the bucket name into
  each Lambda's Terraform `environment` block, duplicating what `codereview-infra` already
  publishes).

## Duplicate-delivery handling

- **Decision**: Out of scope for this feature's Lambda implementations; treated as an
  orchestration-level concern (Step Functions idempotency/dedup at the trigger level in
  `codereview-app`/`codereview-infra`), per the spec's Assumption that orchestration is owned
  outside this repo.
- **Rationale**: The spec marks duplicate-delivery handling as a `SHOULD`, not a `MUST`, and the
  spec's own Assumptions state the orchestrating system owns sequencing. Adding dedup logic inside
  `PostComment` would duplicate a concern better solved once, upstream, matching Principle VI
  (avoid speculative complexity here).
- **Alternatives considered**: Idempotency key check inside `PostComment` before posting —
  possible future enhancement, deliberately deferred rather than built now.
