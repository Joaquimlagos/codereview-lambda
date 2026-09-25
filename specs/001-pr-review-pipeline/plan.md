# Implementation Plan: AI PR Review Pipeline

**Branch**: `001-pr-review-pipeline` | **Date**: 2026-09-18 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `/specs/001-pr-review-pipeline/spec.md`

## Summary

Implement the four Lambdas that fulfill the `codereview-infra` Step Functions contract for
automated PR review: `RouteModel` (complexity + RAG-need decision via Jev), `RetrieveContext`
(diff-relevant project context, only when routed for it), `InvokeLLM` (calls the Gemini API
directly, choosing the model by complexity tier itself — no separate routing service), and
`PostComment` (posts the review to the GitHub PR). Each Lambda is an independent deployable unit
(Principle II) that talks to every external system (Jev, Gemini, S3, SSM, GitHub) only through a
testable abstraction (Principle IV), configured exclusively via environment variables (FR-009),
and runnable end-to-end against LocalStack with no live AWS/network dependency (Principle V,
SC-003).

## Technical Context

**Language/Version**: Python 3.14 (newest GA AWS Lambda managed runtime, available since
2025-11-18, with confirmed, mature emulation support in LocalStack — see `research.md`)

**Primary Dependencies**: `boto3` (S3, SSM); `pydantic` (runtime validation of the Step Functions
I/O contract per FR-012); `requests` (HTTP clients for Jev, Gemini, and the GitHub API — kept
thin and wrapped, never called directly from handlers, per Principle IV)

**Storage**: S3 (diff and retrieved-context artifacts, referenced by key per FR-013) and SSM
(configuration + the per-Lambda ARN registry at `/codereview/lambda/{state}/arn`, where
`{state}` is the kebab-case function name — `route-model`, `retrieve-context`, `invoke-llm`,
`post-comment` — confirmed against `codereview-infra`'s current `lambda_arns.tf`, published
natively by Terraform (`infra/arn_publish.tf`), per FR-010, see `research.md`) — both external
to this repo, stood in locally by LocalStack (Principle V)

**Testing**: `pytest`, with every external dependency (Jev, Gemini, S3, SSM, GitHub)
swapped for a local stub/fake at the abstraction boundary (Principle IV, FR-011) so the suite
makes zero real network calls (SC-003); a dedicated `contract` test layer asserts each Lambda's
input/output against the shared schema (FR-012, SC-004)

**Target Platform**: AWS Lambda (Python 3.14 runtime), orchestrated by the Step Functions state
machine defined in `codereview-infra`; local development/test target is LocalStack standing in for
S3, SSM, and Step Functions

**Project Type**: Backend pipeline — four independently deployable Lambda handlers plus a shared,
testable integrations package (no frontend/mobile component)

**Performance Goals**: No explicit latency/throughput SLA; the binding constraint is Gemini
free-tier request budget (Principle III), not speed — correctness and cost-tier discipline take
priority over raw performance

**Constraints**: No paid-tier model calls (Principle III); all configuration via environment
variables only, no hardcoded endpoints/credentials/resource identifiers (FR-009); every stage
input/output must conform exactly to the shared Step Functions contract with no adapter logic
(FR-012); diffs/context are carried by reference (S3 key), never inlined between stages (FR-013)

**Scale/Scope**: 4 Lambdas, 1 Step Functions workflow, invocation volume tied to this portfolio
project's own PR traffic — small scale by design, not built for high throughput

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| Principle | Gate | Status |
|-----------|------|--------|
| I. English-Only Codebase | All planned code, docs, and contracts are English-only | PASS |
| II. One Lambda Per Step Functions State | 4 states → 4 independent Lambda folders, no shared dispatcher handler; each Lambda's own least-privilege IAM execution role is provisioned by this repo's deploy tooling (spec.md Assumptions) — `codereview-infra` only owns the separate Step-Functions-invocation role | PASS |
| III. Cost-Conscious Model Usage | RouteModel decision via Jev (non-generative); InvokeLLM restricted to the three free-tier Gemini models, called directly with no separate routing service | PASS |
| IV. External Integrations Behind Testable Abstractions | Jev, Gemini, S3, SSM, and GitHub each get a dedicated interface with a network-free stub (see `research.md` / Project Structure) | PASS |
| V. Local Testability via LocalStack | S3 and SSM usage documented with a LocalStack-compatible path (see `quickstart.md`) | PASS |
| VI. Portfolio-Grade Clarity Over Premature Optimization | Structure is a flat, one-folder-per-Lambda layout plus a single shared integrations package — no speculative abstraction layers | PASS |

No violations identified; **Complexity Tracking** is not needed.

## Project Structure

### Documentation (this feature)

```text
specs/001-pr-review-pipeline/
├── plan.md              # This file (/speckit-plan command output)
├── research.md          # Phase 0 output (/speckit-plan command)
├── data-model.md        # Phase 1 output (/speckit-plan command)
├── quickstart.md        # Phase 1 output (/speckit-plan command)
├── contracts/           # Phase 1 output (/speckit-plan command)
│   └── step-io-contracts.md
└── tasks.md             # Phase 2 output (/speckit-tasks command - NOT created by /speckit-plan)
```

### Source Code (repository root)

```text
src/
├── route_model/            # RouteModel Lambda (Step Functions state: RouteModel)
│   └── handler.py
├── retrieve_context/       # RetrieveContext Lambda (Step Functions state: RetrieveContext)
│   └── handler.py
├── invoke_llm/              # InvokeLLM Lambda (Step Functions state: InvokeLLM)
│   └── handler.py
├── post_comment/            # PostComment Lambda (Step Functions state: PostComment)
│   └── handler.py
├── contracts/                # Shared pydantic models for the Step Functions I/O contract (FR-012)
│   └── models.py
└── integrations/              # Testable abstractions for every external call (Principle IV)
    ├── decision_engine.py     # Jev client + interface (used by RouteModel)
    ├── llm_router.py          # Gemini client + interface (used by InvokeLLM, called directly)
    ├── storage.py              # S3 client + interface (diff/context by reference, FR-013)
    ├── config.py                # env/SSM config resolution (non-secret values, FR-009)
    ├── secrets.py                # env/.env fallback + Secrets Manager resolution (API keys/tokens)
    └── github.py                 # GitHub PR-comment client + interface (used by PostComment)

tests/
├── contract/                 # Per-Lambda input/output schema conformance (FR-012, SC-004)
├── integration/               # LocalStack-backed end-to-end pipeline runs
└── unit/                      # Per-Lambda and per-integration tests against stubs (FR-011, SC-003)

infra/
├── iam_route_model.tf, iam_retrieve_context.tf, iam_invoke_llm.tf, iam_post_comment.tf
│                             # Per-Lambda execution role + least-privilege policy + aws_lambda_function
├── arn_publish.tf              # aws_ssm_parameter per function, publishing its ARN (FR-010)
└── lambda_package.tf            # Shared archive_file packaging src/ for all four functions
```

**Structure Decision**: One top-level folder per Lambda under `src/`, matching Principle II
one-to-one with the four Step Functions states. All external-service access is centralized in
`src/integrations/`, so no handler imports an SDK or HTTP client directly (Principle IV), and each
integration module exposes both its real client and a local stub used by `tests/unit` and
`tests/integration`. `src/contracts/` holds the shared pydantic models so all four Lambdas and the
contract tests validate against one definition instead of four ad-hoc copies (FR-012). This
resolves the "shared vs. per-Lambda" TODO left open in `CLAUDE.md`. ARN publishing is now a native
Terraform resource (`infra/arn_publish.tf`), not a separate post-deploy script — the earlier
`scripts/publish_arns.py` has been removed.

## Complexity Tracking

Not applicable — no Constitution Check violations were identified.
