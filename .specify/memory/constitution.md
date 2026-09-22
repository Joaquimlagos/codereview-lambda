<!--
Sync Impact Report
==================
Version change: 1.0.0 → 1.1.0
Rationale: InvokeLLM was implemented to call the Gemini `generateContent` API directly
(GEMINI_API_BASE + a model resolved per complexity tier, in-Lambda), not through a
separately deployed "9router" proxy service as Principles III/IV and the Technology
Constraints originally described. This amendment removes the 9router requirement and
documents the actual architecture. Treated as MINOR: the principle's enforceable intent
(free-tier Gemini models only, no paid-tier calls, Jev for structured decisions) is
unchanged — only an implementation-level routing detail is removed, not the principle
itself.

Modified principles:
- III. Cost-Conscious Model Usage — removed "routed through the local 9router"; now
  states calls go directly to the Gemini API, with model-tier selection performed in the
  InvokeLLM Lambda itself.
- IV. External Integrations Behind Testable Abstractions — "Gemini/9router" → "Gemini" in
  the list of integrations requiring a testable abstraction.

Added sections: none
Removed sections: none

Other changes:
- Technology Constraints' LLM-generation bullet updated to match (no 9router).

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
calls MUST use only the three Gemini free-tier models (flash-lite, flash, pro),
called directly from the InvokeLLM Lambda — model selection by complexity tier
is performed in that Lambda itself, with no separate routing service in front
of the Gemini API. Structured decisions that do not require open-ended
generation — model routing and RAG-necessity checks — MUST use Jev (TypeSafe
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

Rationale: This is what makes local development and LocalStack-based testing
possible without depending on live AWS infrastructure or third-party quotas,
and keeps handler code focused on orchestration logic rather than integration
plumbing.

### V. Local Testability via LocalStack
The full pipeline MUST be runnable and testable locally via LocalStack,
without requiring real AWS infrastructure. Any AWS service used by a Lambda
(S3, SSM, Step Functions, etc.) MUST have a documented LocalStack-compatible
setup path.

Rationale: Fast local iteration and CI runs must not depend on provisioning
real cloud resources or incurring AWS costs just to validate a change.

### VI. Portfolio-Grade Clarity Over Premature Optimization
Code and documentation MUST be written to clearly communicate intent to a
reader encountering the project for the first time. Clarity takes priority
over premature optimization or clever abstraction; optimize only when a real
constraint (cost, latency, free-tier limit) demands it.

Rationale: This repository serves as a portfolio artifact — a reviewer's
ability to understand *why* code is structured a certain way matters more than
marginal performance gains that add complexity.

## Technology Constraints

- LLM generation: Gemini free-tier models only (flash-lite, flash, pro), called directly from the InvokeLLM Lambda, which performs the complexity-tier → model selection itself — no paid-tier model calls, no separate routing service.
- Structured decisioning (routing, RAG-necessity): Jev (TypeSafe AI), not a generative LLM call.
- Orchestration: AWS Step Functions, with one Lambda per state as required by Principle II.
- Storage/context: S3 for RAG context artifacts; SSM for configuration and secrets — both accessed only through the abstractions required by Principle IV.
- Local/dev environment: LocalStack MUST be able to stand in for all AWS services the pipeline touches.

## Development Workflow

- Every PR MUST be reviewed against these principles before merge; a change that adds a new Step Functions state MUST add a corresponding new, independent Lambda (Principle II), not extend an existing handler.
- A new external integration MUST ship with its testable abstraction and a local stub from the same PR that introduces its first caller (Principle IV).
- Reviewers MUST confirm generative Gemini calls are not used where a structured/deterministic decision (routing, RAG-necessity) would suffice (Principle III).
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

**Version**: 1.1.0 | **Ratified**: 2026-09-18 | **Last Amended**: 2026-09-21
