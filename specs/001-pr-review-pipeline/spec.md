# Feature Specification: AI PR Review Pipeline

**Feature Branch**: `001-pr-review-pipeline`

**Created**: 2026-09-18

**Status**: Draft

**Input**: User description: "Especifique o harness do pipeline de revisão de PR com IA, implementado como 4 Lambdas Python separadas que correspondem aos estados de uma Step Functions já definida no repositório de infra (codereview-infra): RouteModel (decide complexidade e necessidade de RAG via Jev/DecisionEngine), RetrieveContext (busca contexto do projeto quando necessário), InvokeLLM (seleciona modelo Gemini por complexidade e gera a review via 9router), PostComment (posta a review no PR via API do GitHub). Requisitos não-funcionais: configuração só via variáveis de ambiente; cada Lambda publica seu ARN no SSM em /codereview/{environment}/lambda/{nome-do-estado}-arn; toda chamada externa é testável via mock/stub sem rede real; input/output de cada Lambda deve bater com o contrato da Step Functions do codereview-infra."

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Automated Review Comment on a Pull Request (Priority: P1)

A repository maintainer opens or updates a pull request. Without any manual action, an
automated code review comment appears on that pull request summarizing the diff and
flagging notable issues.

**Why this priority**: This is the core value of the product — a PR gets reviewed and
commented on automatically. Nothing else matters if this end-to-end path doesn't work.

**Independent Test**: Can be fully tested by triggering the pipeline with a PR event that
references a stored diff, letting it run through complexity routing (no additional context
needed) straight to review generation and comment posting, and confirming a review comment
appears on the target PR.

**Acceptance Scenarios**:

1. **Given** a pull request event with a diff already stored and referenced by key, **When**
   the pipeline runs and no additional project context is required, **Then** a review comment
   generated from the diff is posted on the pull request.
2. **Given** a pull request event, **When** the pipeline determines the PR's complexity,
   **Then** that complexity tier is used to select the review-generation resource for that PR.
3. **Given** the review text has been generated, **When** the pipeline attempts to post it,
   **Then** the comment appears attached to the correct pull request.

---

### User Story 2 - Cost-Aware Complexity Routing (Priority: P2)

Before generating a review, the pipeline classifies how complex the pull request is (low,
medium, or high) so that simple PRs are handled with lighter-weight resources and only truly
complex PRs consume the most capable (and costly) review resource.

**Why this priority**: Directly supports the project's cost-conscious operating constraint —
without routing, every PR would consume the most expensive review path regardless of size or
risk, which the project cannot sustain on a free-tier budget.

**Independent Test**: Can be tested independently by submitting PRs of varying diff size/shape
and confirming the routing decision (complexity tier, additional-context flag) is produced
correctly and consistently for each, without needing the rest of the pipeline to execute.

**Acceptance Scenarios**:

1. **Given** a small, low-risk diff, **When** the pipeline classifies it, **Then** it is
   assigned a "low" complexity tier.
2. **Given** a large or high-risk diff, **When** the pipeline classifies it, **Then** it is
   assigned a "high" complexity tier.
3. **Given** the routing decision has been made, **When** the pipeline proceeds, **Then** the
   next stage receives exactly that decision (complexity tier and context-needed flag) without
   needing to re-derive it.

---

### User Story 3 - Context-Enriched Review for Complex Changes (Priority: P3)

When a pull request's routing decision indicates the diff alone isn't enough to review it well
(e.g., it touches code whose behavior depends on other parts of the project), the pipeline
gathers relevant project context before generating the review, so the feedback reflects how the
change fits into the broader codebase rather than just the raw diff text.

**Why this priority**: Improves review quality for the subset of PRs that need it, but the
pipeline is still useful end-to-end without it (User Story 1), so it's an enhancement rather
than the critical path.

**Independent Test**: Can be tested independently by feeding a stored diff and confirming that,
when context retrieval runs, it returns relevant project context assembled from the diff and
related project material, independent of whether routing or review generation execute.

**Acceptance Scenarios**:

1. **Given** a routing decision indicating additional context is needed, **When** the pipeline
   proceeds, **Then** relevant project context is retrieved before the review is generated.
2. **Given** a routing decision indicating additional context is NOT needed, **When** the
   pipeline proceeds, **Then** context retrieval is skipped entirely and the review is generated
   from the diff alone.
3. **Given** context has been retrieved, **When** the review is generated, **Then** the
   generated review reflects both the diff and the retrieved context.

---

### Edge Cases

- What happens when the routing decision cannot be produced (the decision engine is unavailable
  or returns an invalid response)? The pipeline MUST still reach a review outcome rather than
  fail silently or hang (see Assumptions for the default fallback).
- What happens when the referenced diff cannot be found or read from storage? The pipeline MUST
  fail that PR's run in a way that is visible to whoever monitors the pipeline, rather than
  posting an empty or misleading review comment.
- What happens when context retrieval finds no relevant related material? The pipeline MUST
  still proceed to generate a review using whatever context is available (diff only).
- What happens when review generation produces no usable output (empty or malformed text)? The
  pipeline MUST NOT post an empty or malformed comment to the pull request.
- What happens when posting the comment fails (the target PR was closed/deleted, or the
  commenting service is unavailable)? The failure MUST be visible to whoever monitors the
  pipeline rather than silently discarded.
- What happens when the same PR event is delivered more than once? The pipeline SHOULD avoid
  posting duplicate review comments for the same PR revision.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The pipeline MUST determine a complexity tier (low, medium, or high) for each
  pull request before a review is generated.
- **FR-002**: The pipeline MUST determine whether a pull request needs additional project
  context beyond its diff before a review is generated.
- **FR-003**: The complexity- and context-need decision MUST be produced through a
  swappable decision capability, so the actual decision-making logic can be replaced (e.g. a
  simple local stand-in vs. a full external decision service) without changing how the rest of
  the pipeline consumes the decision.
- **FR-004**: When a pull request is flagged as needing additional context, the pipeline MUST
  retrieve relevant project context (derived from the diff and related project material) before
  review generation.
- **FR-005**: When a pull request is flagged as NOT needing additional context, the pipeline
  MUST skip context retrieval and proceed directly to review generation using the diff alone.
- **FR-006**: The pipeline MUST generate a natural-language code review for each pull request,
  using a review-generation resource appropriate to that PR's determined complexity tier.
- **FR-007**: The pipeline MUST post the generated review as a comment on the originating pull
  request.
- **FR-008**: The pipeline MUST NOT post a review comment when no usable review text was
  generated.
- **FR-009**: All configuration needed to run any stage of the pipeline (service endpoints,
  credentials, resource identifiers) MUST come exclusively from externally supplied
  configuration — no values may be hardcoded in the pipeline's logic.
- **FR-010**: After deployment, each pipeline stage MUST publish its own resolvable address to a
  shared, well-known configuration location, namespaced by deployment environment and stage
  name, so that the system orchestrating the pipeline can discover each stage dynamically.
- **FR-011**: Every dependency on an external service (decision-making, review generation,
  diff/context storage, PR commenting) MUST be replaceable with a local, network-free stand-in
  for the purpose of automated testing.
- **FR-012**: Each pipeline stage's input and output MUST conform exactly to the data contract
  shared with the system that orchestrates the pipeline, so stages can be composed without
  translation or adapter logic.
- **FR-013**: The pipeline MUST carry a reference to the full pull request diff (rather than the
  diff content itself) between stages, resolving the actual diff content only when a stage needs
  it.

### Key Entities

- **Pull Request Event**: The trigger for a pipeline run; carries lightweight PR metadata and a
  reference (key) to the full diff content stored separately.
- **Routing Decision**: The output of the complexity/context-need determination — a complexity
  tier (low/medium/high) and a flag indicating whether additional context is required.
- **Retrieved Context**: Project context assembled from the diff and related project material,
  produced only when the routing decision requires it, and consumed by review generation.
- **Generated Review**: The natural-language review text produced for a pull request, to be
  posted as a comment.
- **Review Comment**: The posted, user-visible artifact on the pull request containing the
  generated review.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: A pull request that triggers the pipeline receives an automated review comment
  without any manual step, end to end.
- **SC-002**: Low-complexity pull requests are consistently routed to a lighter-weight review
  resource than high-complexity pull requests, keeping the most expensive review resource
  reserved for the PRs that actually need it.
- **SC-003**: The full pipeline (routing, optional context retrieval, review generation,
  comment posting) can be exercised and verified in an automated test suite with zero calls to
  any real external network service or dependency on live credentials.
- **SC-004**: 100% of the data passed between pipeline stages conforms to the shared
  orchestration contract, verified by automated tests, with no manual adaptation required to
  wire stages together.
- **SC-005**: When a required upstream decision or input is unavailable, the pipeline still
  reaches a terminal outcome (either a posted review or a visibly failed/flagged run) rather than
  stalling indefinitely.

## Assumptions

- If the complexity/context-need decision cannot be produced (the decision capability is
  unavailable or returns an invalid response), the pipeline falls back to a "medium" complexity
  tier with no additional context, so the run still completes rather than failing outright.
- Context retrieval, in its initial version, uses a simple text/file-relevance based strategy
  (e.g., matching related files or keywords) rather than a semantic/embeddings-based approach;
  this can evolve later without changing the pipeline's external behavior.
- One review comment is posted per pipeline run (per PR revision); updating or replacing a
  previous comment on the same PR is out of scope for this version.
- Pull request diffs are treated as text; very large diffs may be truncated before being used in
  review generation to stay within what the review-generation resource can process, and this
  truncation does not itself count as a failure.
- The deployment environment name (used to namespace published stage addresses) is supplied to
  the pipeline at build/deploy time and matches the convention already used by the system that
  orchestrates it.
- The orchestrating system (Step Functions workflow and its surrounding infrastructure) already
  exists and defines the authoritative contract for how stages are sequenced and what data flows
  between them; this feature implements the stages that fulfill that contract, not the
  orchestration definition itself.
- `codereview-lambda` (this repo) is responsible for provisioning each Lambda's own
  least-privilege IAM execution role (Principle II), one per Lambda, through a local Terraform
  config (`infra/`) — decided over AWS SAM/Serverless Framework. `codereview-infra` only owns
  the Step Functions state machine's IAM role for *invoking* those Lambdas — a distinct resource
  that grants Step Functions permission to call each function, not the function's own
  permissions to do its work.
- The local decision-engine stub's low/medium/high complexity classification is defined by total
  changed lines (additions + deletions) in the diff: `low` ≤ 50 changed lines, `medium` 51–400,
  `high` > 400. This threshold governs the local stub and test fixtures only — the real Jev
  decision engine's internal classification algorithm is external and may use a different method
  without changing the pipeline's external behavior (Principle IV).
