<!--
Sync Impact Report
==================
Version change: 1.2.0 → 2.0.0
Rationale: Principle V required the full pipeline to be runnable and testable locally via
LocalStack. In practice the pipeline runs directly on AWS (infra/provider.tf has no
LocalStack endpoints), and the test suite, locally and in CI, runs entirely against the
local stubs required by Principle IV — no LocalStack container is involved. Recent
LocalStack images also refuse to start without a paid license token, even for S3/SSM. The
principle is rewritten around what it protects: validating a change without real cloud
resources, credentials or network. Treated as MAJOR: the principle is redefined and the
LocalStack obligation is removed.

Modified principles:
- V. Local Testability via LocalStack → V. Local Testability via Stubs.

Added sections: none
Removed sections: none

Other changes:
- Principle IV's rationale: "LocalStack-based testing" → "stub-based testing".
- Technology Constraints' local/dev bullet reworded to match.
- Technology Constraints' storage bullet: secrets live in Secrets Manager, not SSM; SSM
  Parameter Store holds configuration only (resource ARNs and names).

Follow-up TODOs: none.
-->

# codereview-lambda Constitution

## Core Principles

### I. English-Only Codebase
All code, identifiers, comments, commit messages, and documentation MUST be
written in English, even when planning or design conversations happen in
another language. No exceptions for "temporary" code.

Rationale: The project is built as a portfolio piece intended for an
English-reading audience (reviewers, recruiters, collaborators); mixed-language
code fragments an unfamiliar reader's understanding and signal inconsistent craft.

### II. One Lambda Per Step Functions State
Every state in the Step Functions workflow (e.g., RouteModel, RetrieveContext,
InvokeLLM, PostComment) MUST be implemented as its own independent Lambda
function. A single Lambda MUST NOT act as a monolithic handler that internally
dispatches to multiple states or responsibilities. Each Lambda MUST have its
own least-privilege IAM role/policy, and MUST be able to scale and set
concurrency limits independently of the others.

Rationale: Per-state isolation keeps blast radius, IAM permissions, and
scaling characteristics scoped to exactly what each step needs, and keeps the
Step Functions definition legible as the source of truth for orchestration
rather than in-code branching.

### III. Cost-Conscious Model Usage
The project MUST run within free-tier limits during this phase. Generative LLM
calls MUST be organized into three complexity tiers, each served by free-tier
models with fallback across providers, called directly from the InvokeLLM
Lambda — model selection by complexity tier, and falling back to the next model
or provider, is performed in that Lambda itself, with no separate routing
service in front of the providers' APIs. Structured decisions that do not
require open-ended generation — model routing and RAG-necessity checks — MUST
use Jev (TypeSafe
AI), an external decision model, rather than spending a generative LLM call on
them.

Rationale: Generative calls are the scarce, costly resource; offloading
deterministic/structured decisions to a purpose-built decision model preserves
free-tier budget for the analysis work that actually needs generation.

### IV. External Integrations Behind Testable Abstractions
Every external integration (Jev, Gemini, S3, SSM, and any future
external service) MUST be accessed through a dedicated, testable
interface/abstraction. Handlers MUST NOT call external SDKs or HTTP clients
directly. Each abstraction MUST support a local stub/fake implementation that
requires no network access and no real credentials.

Rationale: This is what makes local development and stub-based testing
possible without depending on live AWS infrastructure or third-party quotas,
and keeps handler code focused on orchestration logic rather than integration
plumbing.

### V. Local Testability via Stubs
Every Lambda handler and every external integration MUST be testable locally
without network access, credentials, or real AWS infrastructure, through the
local stubs required by Principle IV. The test suite, locally and in CI, MUST
run entirely against those stubs. The deployed pipeline runs directly on AWS;
no LocalStack setup is required or maintained.

Rationale: Fast local iteration and CI runs must not depend on provisioning
real cloud resources or incurring AWS costs just to validate a change. The
pipeline was first planned around LocalStack, but it is deployed directly to
AWS and its tests already run on stubs, which give the same isolation without
a container or a license.

### VI. Portfolio-Grade Clarity Over Premature Optimization
Code and documentation MUST be written to clearly communicate intent to a
reader encountering the project for the first time. Clarity takes priority
over premature optimization or clever abstraction; optimize only when a real
constraint (cost, latency, free-tier limit) demands it.

Rationale: This repository serves as a portfolio artifact — a reviewer's
ability to understand *why* code is structured a certain way matters more than
marginal performance gains that add complexity.

## Technology Constraints

- LLM generation: three complexity tiers, each served by free-tier models with fallback across providers, called directly from the InvokeLLM Lambda, which performs the complexity-tier → model selection and the fallback itself — no paid-tier model calls, no separate routing service.
- Structured decisioning (routing, RAG-necessity): Jev (TypeSafe AI), not a generative LLM call.
- Orchestration: AWS Step Functions, with one Lambda per state as required by Principle II.
- Storage/context: S3 for RAG context artifacts; SSM Parameter Store for configuration (resource ARNs and names); Secrets Manager for API keys and the GitHub App private key — all accessed only through the abstractions required by Principle IV.
- Local/dev environment: local stubs (Principle IV) stand in for every AWS service and external API in tests; deployment targets real AWS.

## Development Workflow

- Every PR MUST be reviewed against these principles before merge; a change that adds a new Step Functions state MUST add a corresponding new, independent Lambda (Principle II), not extend an existing handler.
- A new external integration MUST ship with its testable abstraction and a local stub from the same PR that introduces its first caller (Principle IV).
- Reviewers MUST confirm generative LLM calls are not used where a structured/deterministic decision (routing, RAG-necessity) would suffice (Principle III).
- Code, comments, and docs MUST be reviewed for English-only compliance regardless of the language used in the PR description or discussion (Principle I).

## Governance

This constitution supersedes other informal practices for this repository.
Amendments are made by editing `.specify/memory/constitution.md` directly,
updating the Sync Impact Report, and bumping the version per semantic
versioning:

- MAJOR: backward-incompatible principle removal or redefinition.
- MINOR: a new principle or materially expanded section added.
- PATCH: wording clarifications with no semantic change.

All feature plans and PRs are expected to verify compliance with this
constitution. Any deviation must be justified in the relevant plan/PR
description; unjustified complexity or violations of Principles I–VI should
be flagged in review.

**Version**: 2.0.0 | **Ratified**: 2026-09-18 | **Last Amended**: 2026-09-28
