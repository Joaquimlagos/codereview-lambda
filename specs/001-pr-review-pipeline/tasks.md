---

description: "Task list for the AI PR Review Pipeline feature"
---

# Tasks: AI PR Review Pipeline

**Input**: Design documents from `/specs/001-pr-review-pipeline/`

**Prerequisites**: [plan.md](./plan.md), [spec.md](./spec.md), [research.md](./research.md),
[data-model.md](./data-model.md), [contracts/step-io-contracts.md](./contracts/step-io-contracts.md),
[quickstart.md](./quickstart.md)

**Tests**: Included. The spec's own Success Criteria (SC-003, SC-004) and Functional Requirements
(FR-011, FR-012) explicitly require automated, network-free contract and integration test
coverage, so test tasks are not optional here.

**Organization**: Tasks are grouped by user story (spec.md priorities P1/P2/P3) so each story is
independently implementable and testable.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies on incomplete tasks)
- **[Story]**: US1 = Automated Review Comment (P1), US2 = Cost-Aware Complexity Routing (P2),
  US3 = Context-Enriched Review (P3)
- File paths follow the `src/` / `tests/` layout defined in [plan.md](./plan.md)'s Project Structure

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Project initialization per plan.md's Project Structure.

- [X] T001 Create the source/test skeleton exactly as laid out in [plan.md](./plan.md)'s Project
  Structure: `src/route_model/`, `src/retrieve_context/`, `src/invoke_llm/`, `src/post_comment/`,
  `src/contracts/`, `src/integrations/` (each with `__init__.py`), `tests/contract/`,
  `tests/integration/`, `tests/unit/`, and `scripts/`.
- [X] T002 Initialize `pyproject.toml` at the repo root declaring the Python 3.14 target and the
  dependencies from plan.md's Technical Context: `boto3`, `pydantic`, `requests`, `pytest`.
- [X] T003 [P] Add `ruff` configuration (`pyproject.toml` `[tool.ruff]` or `ruff.toml`) as the
  project's linter/formatter, resolving the "linter Python a definir" TODO in `CLAUDE.md`.
- [X] T004 [P] Update `CLAUDE.md`'s Quick Start section with the concrete `python -m venv`/install
  command (from T002's `pyproject.toml`) and the LocalStack bring-up command (services: `s3,ssm`),
  replacing the two setup TODOs there — content driven by [quickstart.md](./quickstart.md)'s
  Prerequisites section.

**Checkpoint**: Repo skeleton, dependency manifest, and lint config exist; ready for shared code.

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: The shared contract models and external-integration abstractions every Lambda and
every user story depends on (Principle IV, FR-011, FR-012).

**⚠️ CRITICAL**: No user story task may start until this phase is complete.

- [X] T005 Reconcile [contracts/step-io-contracts.md](./contracts/step-io-contracts.md) against
  `codereview-infra`'s actual Step Functions definition
  (`statemachine/definition.asl.json.tpl`) and ARN contract (`lambda_arns.tf`) before any model
  in this phase is written. Two concrete discrepancies to resolve, found by reading those files
  directly: (1) the ASL's `Choice` state branches on `$.routeModel.needsRag` (boolean) — the real
  field name is `needsRag`, not this repo's assumed `context_needed`; (2) each state's
  `ResultPath` (`$.routeModel`, `$.retrieveContext`, `$.invokeLlm`, `$.postComment`) nests that
  state's raw Lambda output under a namespaced key inside the growing accumulated event — a
  downstream Lambda's input is the *whole* accumulated object, not just the immediately prior
  stage's flat output as currently documented. Update `contracts/step-io-contracts.md` and
  `data-model.md` field names/shapes to match before T006–T010 encode them as pydantic models.
  If the real field name turns out to be `needsRag` (or anything else other than
  `context_needed`), also update every other place that currently asserts `context_needed` as
  settled fact rather than treating it as reconciled by this task: `research.md`'s "Fallback
  behavior when routing is unavailable" section, and `quickstart.md`'s Scenarios 1, 3, and 4
  (currently lines 25, 41, and 51–52).
- [X] T006 [P] Define the `PullRequestEvent` model in `src/contracts/models.py` (per T005's
  reconciled shape): fields `pr_id` (string), `repository` (string), `revision` (string),
  `diff_ref` (string). Per [data-model.md](./data-model.md): all fields required and non-empty;
  `diff_ref` MUST be a resolvable storage key, not inline diff text.
- [X] T007 [P] Define the `RoutingDecision` model in `src/contracts/models.py`, using the field
  names/shape as reconciled by T005 (this repo's current placeholder is `complexity` (enum
  `low`/`medium`/`high`) + `context_needed` (boolean); update to whatever T005 confirms against
  the real ASL/contract before finalizing). Per [data-model.md](./data-model.md): `complexity`
  MUST be one of the three tiers — no free-text values.
- [X] T008 [P] Define the `RetrievedContext` model in `src/contracts/models.py`: fields `pr_id`
  (string), `context_ref` (string), `sources` (list of string). Per
  [data-model.md](./data-model.md): `sources` MAY be an empty list.
- [X] T009 [P] Define the `GeneratedReview` model in `src/contracts/models.py`: fields `pr_id`
  (string), `review_text` (string), `model_used` (string). Per [data-model.md](./data-model.md):
  `review_text` MUST be non-empty and non-malformed.
- [X] T010 [P] Define the `ReviewComment` model in `src/contracts/models.py`: fields `pr_id`
  (string), `comment_id` (string), `posted` (boolean). Per [data-model.md](./data-model.md):
  `posted` MUST be `true` only on a confirmed successful post.
- [X] T011 [P] Implement the SSM abstraction (interface + real client + local stub) in
  `src/integrations/config.py`: read all configuration exclusively from environment variables
  (FR-009), and publish a resolvable ARN to `/codereview/{environment}/lambda/{state}-arn`, where
  `{state}` is the kebab-case function name — `route-model`, `retrieve-context`, `invoke-llm`,
  `post-comment` — exactly matching the keys `codereview-infra`'s `lambda_arns.tf` looks up
  (FR-010; see `research.md`, "SSM ARN registry key format").
- [X] T012 [P] Implement the S3 abstraction (interface + real client + local stub) in
  `src/integrations/storage.py`: resolve a `diff_ref`/`context_ref` key to its content; never
  passes diff/context content directly between callers (FR-013).
- [X] T013 [P] Implement the Jev decision-engine abstraction (interface + real client + local
  stub) in `src/integrations/decision_engine.py`, including a configurable
  failure/invalid-response mode for the stub, needed by `RouteModel`'s fallback path
  (research.md, "Fallback behavior when routing is unavailable").
- [X] T014 [P] Implement the 9router/Gemini abstraction (interface + real client + local stub) in
  `src/integrations/llm_router.py`, restricted to the three free-tier Gemini models (flash-lite,
  flash, pro) per Principle III.
- [X] T015 [P] Implement the GitHub PR-comment abstraction (interface + real client + local stub)
  in `src/integrations/github.py`.
- [X] T016 Add `tests/conftest.py` fixtures that wire every Lambda handler to the local stubs from
  T011–T015 (and LocalStack for S3/SSM where applicable) by default, so `tests/unit` and
  `tests/integration` make zero real network calls (SC-003, FR-011).

**Checkpoint**: Foundation ready — user story implementation can begin.

---

## Phase 3: User Story 1 - Automated Review Comment on a Pull Request (Priority: P1) 🎯 MVP

**Goal**: A PR event with a stored diff and no additional-context need flows straight through
`RouteModel` → `InvokeLLM` → `PostComment` and lands a review comment on the PR.

**Independent Test**: Trigger the pipeline with a PR event referencing a stored diff, no context
retrieval needed, and confirm a review comment is posted (quickstart.md Scenario 1).

### Tests for User Story 1

> **NOTE**: Write these tests first; confirm they fail before implementing the handlers below.

- [X] T017 [P] [US1] Contract test for `RouteModel` in `tests/contract/test_route_model_contract.py`:
  assert output validates against `RoutingDecision` (T007) per
  [contracts/step-io-contracts.md](./contracts/step-io-contracts.md).
- [X] T018 [P] [US1] Contract test for `InvokeLLM` in `tests/contract/test_invoke_llm_contract.py`:
  assert output validates against `GeneratedReview` (T009).
- [X] T019 [P] [US1] Contract test for `PostComment` in
  `tests/contract/test_post_comment_contract.py`: assert output validates against `ReviewComment`
  (T010).
- [X] T020 [P] [US1] Integration test in `tests/integration/test_end_to_end_review.py`
  implementing quickstart.md Scenario 1: seed a fixture diff, run
  `RouteModel` (context-not-needed case) → `InvokeLLM` → `PostComment`, assert exactly one posted
  comment with non-empty review text (SC-001).

### Implementation for User Story 1

- [X] T021 [US1] Implement the `RouteModel` handler in `src/route_model/handler.py`: validate
  input against `PullRequestEvent` (T006), call the decision-engine abstraction (T013), return a
  `RoutingDecision` (T007) (FR-001, FR-002, FR-003). On decision-engine failure or an invalid
  response, return the fixed fallback complexity `"medium"` with context-not-needed (exact field
  name per T005) instead of raising (research.md, "Fallback behavior when routing is
  unavailable"; SC-005) — this is a
  whole-pipeline success criterion, not a User Story 2 concern, so it MUST be part of the MVP,
  not deferred to Phase 4.
- [X] T022 [US1] Implement the `InvokeLLM` handler in `src/invoke_llm/handler.py`: accept the
  accumulated event carrying `PullRequestEvent` + `RoutingDecision` under `$.routeModel` (per
  T005's reconciled shape), call the llm-router abstraction (T014), return a `GeneratedReview`
  (T009) (FR-006). Depends on T021's output shape.
- [X] T023 [US1] Implement the `PostComment` handler in `src/post_comment/handler.py`: accept
  `GeneratedReview`, MUST NOT construct/post a comment when `review_text` is empty or malformed
  (FR-008), call the GitHub abstraction (T015), return `ReviewComment` (T010), and raise/flag
  visibly on a posting failure rather than swallowing it (FR-007).
- [X] T024 [US1] Add the diff-not-found and empty-generation edge-case handling across
  `src/route_model/handler.py`, `src/invoke_llm/handler.py`, and `src/post_comment/handler.py`:
  a diff that cannot be found/read via storage (T012) MUST fail the run visibly (spec Edge Case),
  matching the checks already required by T021–T023.

**Checkpoint**: User Story 1 is fully functional and independently testable end-to-end.
**Caveat**: FR-010 (publishing each Lambda's ARN to SSM) is not yet satisfied at this checkpoint
— that lands in Phase 6 (T037). This does not block local testing (LocalStack/stub-based tests
invoke handlers directly), but the pipeline is not yet fully deployable/discoverable by
`codereview-infra` until Phase 6 completes.

---

## Phase 4: User Story 2 - Cost-Aware Complexity Routing (Priority: P2)

**Goal**: `RouteModel` consistently classifies PRs into low/medium/high complexity, and
`InvokeLLM` picks a correspondingly lighter or heavier free-tier Gemini model.

**Independent Test**: Submit diffs of varying size/shape and confirm the routing decision
(complexity tier, context-needed flag) is produced correctly without running the rest of the
pipeline (quickstart.md Scenario 2).

### Tests for User Story 2

- [X] T025 [P] [US2] Unit test in `tests/unit/test_route_model.py`: a fixture diff with ≤ 50
  changed lines (additions + deletions) is classified `complexity: "low"` (spec.md Assumptions —
  local stub threshold; Acceptance Scenario, User Story 2 #1).
- [X] T026 [P] [US2] Unit test in `tests/unit/test_route_model.py`: a fixture diff with > 400
  changed lines is classified `complexity: "high"` (spec.md Assumptions — local stub threshold;
  Acceptance Scenario, User Story 2 #2). Include a mid-range fixture (51–400 changed lines)
  asserting `complexity: "medium"` for completeness.
- [X] T027 [P] [US2] Integration test in `tests/integration/test_complexity_routing.py`
  implementing quickstart.md Scenario 2: feed both a "low" and a "high" `RoutingDecision` into
  `InvokeLLM` and assert the llm-router stub records a lighter-weight model for "low" than for
  "high" (SC-002).

### Implementation for User Story 2

- [X] T028 [US2] Extend the decision-engine stub in `src/integrations/decision_engine.py` (T013)
  to classify by total changed lines per spec.md's Assumptions: `low` ≤ 50, `medium` 51–400,
  `high` > 400.
- [X] T029 [US2] Extend the llm-router abstraction in `src/integrations/llm_router.py` (T014) with
  an explicit complexity-tier → free-tier Gemini model mapping (flash-lite/flash/pro), so lower
  tiers never select a heavier model (Principle III, SC-002).
- [X] T030 [US2] Verify the complexity-tier → model mapping from T029 against `RouteModel`'s
  fallback tier: since T021 already falls back to `complexity: "medium"` on decision-engine
  failure (an MVP/whole-pipeline concern, not a User Story 2 one), confirm `InvokeLLM` resolves
  that fallback tier to the correct "medium" free-tier Gemini model with no special-casing needed.

**Checkpoint**: User Stories 1 and 2 both work independently.

---

## Phase 5: User Story 3 - Context-Enriched Review for Complex Changes (Priority: P3)

**Goal**: When `RoutingDecision`'s context-needed flag (field name pending T005) is true,
`RetrieveContext` assembles relevant project context before `InvokeLLM` generates the review.

**Independent Test**: Feed a stored diff into `RetrieveContext` directly and confirm it returns
relevant project context assembled from the diff and related material, independent of routing or
review generation (quickstart.md Scenario 3).

### Tests for User Story 3

- [X] T031 [P] [US3] Contract test for `RetrieveContext` in
  `tests/contract/test_retrieve_context_contract.py`: assert output validates against
  `RetrievedContext` (T008) per
  [contracts/step-io-contracts.md](./contracts/step-io-contracts.md); include a case where
  `sources` is `[]` (data-model.md Edge Case).
- [X] T032 [P] [US3] Integration test in `tests/integration/test_context_enriched_review.py`
  implementing quickstart.md Scenario 3: run `RouteModel` (context-needed case) →
  `RetrieveContext` → `InvokeLLM`, assert the llm-router stub call recorded the retrieved context
  alongside the diff, not the diff alone.
- [X] T033 [P] [US3] Integration test in `tests/integration/test_context_skip.py`: with the
  context-not-needed case, assert `RetrieveContext` is never invoked and `InvokeLLM` runs from the
  diff alone (FR-005, spec Acceptance Scenario, User Story 3 #2).

### Implementation for User Story 3

- [X] T034 [US3] Implement the `RetrieveContext` handler in `src/retrieve_context/handler.py`:
  accept the accumulated event carrying `PullRequestEvent` + `RoutingDecision` (per T005's
  reconciled shape), assemble context from the diff and related project material via the storage
  abstraction (T012), return `RetrievedContext` (T008); an empty `sources` list MUST NOT block
  the run (FR-004, data-model.md Edge Case).
- [X] T035 [US3] Extend the `InvokeLLM` handler in `src/invoke_llm/handler.py` (T022) to accept an
  optional `RetrievedContext` (nested under `$.retrieveContext` per T005's reconciled shape) and
  incorporate it into the review-generation call when present, so the generated review reflects
  both diff and context (spec Acceptance Scenario, User Story 3 #3).

**Checkpoint**: All three user stories are independently functional.

---

## Phase 6: Polish & Cross-Cutting Concerns

**Purpose**: Deployment tooling and end-to-end validation that spans all three user stories.

- [X] T036 [P] Implement ARN-publishing tooling in `scripts/publish_arns.py` built on the SSM
  abstraction (T011) so each of the four Lambdas' ARNs is published to
  `/codereview/{environment}/lambda/{state}-arn` (kebab-case state slugs, per T011) after deploy
  (FR-010). Satisfies the Phase 3 checkpoint caveat above.
- [X] T037 [P] Run the full `pytest` suite and confirm: zero calls to any real external network
  endpoint (SC-003), and 100% of inter-stage payloads validated against the shared contract models
  from Phase 2 (SC-004). Result: 15/15 passed, `ruff check` clean; every test uses the stub/
  in-memory integrations from Phase 2 — no `requests`/`boto3` real client is ever instantiated.
- [X] T038 Execute quickstart.md Scenarios 1–5 against a running LocalStack instance and record the
  results, closing out the manual validation pass for this feature. Result: all 5 PASSED against
  a live `localstack/localstack:3.0` container (S3 + SSM, community edition) — real S3
  put/get for the diff and retrieved-context text, real SSM `put_parameter` calls at the
  4 kebab-case ARN paths (T011/T036), low/high complexity routing to `gemini-flash-lite`/
  `gemini-pro` respectively, context-enriched `InvokeLLM` call recorded a non-null context, the
  Jev-failure fallback produced exactly `{"complexity": "medium", "needsRag": false}`, and the
  full `pytest` suite (15/15) stayed green throughout.
- [X] T039 [P] Replace the remaining `CLAUDE.md` TODOs (`pytest` command, linter command) with the
  concrete commands now that T002/T003/T016 exist.

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: No dependencies — start immediately.
- **Foundational (Phase 2)**: Depends on Setup (T001–T004) — BLOCKS all user stories. Within
  Phase 2, T005 (contract reconciliation against `codereview-infra`) BLOCKS T006–T010 (the models
  it corrects).
- **User Story 1 (Phase 3)**: Depends on Foundational (Phase 2) only.
- **User Story 2 (Phase 4)**: Depends on Foundational (Phase 2) and on `RouteModel`/`InvokeLLM`
  existing from User Story 1 (T021, T022), since it extends rather than duplicates them.
- **User Story 3 (Phase 5)**: Depends on Foundational (Phase 2) and on `InvokeLLM` existing from
  User Story 1 (T022), since it extends rather than duplicates it.
- **Polish (Phase 6)**: Depends on all three user stories being complete.

### User Story Dependencies

- **User Story 1 (P1)**: Independently testable once Phase 2 is done — this is the MVP. Note:
  FR-010 (ARN publishing) is *not* part of this story's checkpoint — see the Phase 3 caveat.
- **User Story 2 (P2)**: Independently testable on its own inputs/outputs (routing decisions in
  isolation, per quickstart.md Scenario 2), but its implementation tasks modify files US1 already
  created (T021's handler, T014's router) rather than standing fully apart. Note: the
  decision-unavailable fallback (SC-005) lives in T021/US1, not here — US2 only adds the
  fine-grained tier→model mapping and confirms the fallback tier resolves cleanly through it.
- **User Story 3 (P3)**: Same relationship as US2 — independently testable via
  `RetrieveContext` in isolation, but T035 extends the `InvokeLLM` handler from US1.

### Within Each User Story

- Tests are written first and MUST fail before the corresponding implementation task.
- Contract/unit tests before integration tests before handler implementation.
- Story checkpoint reached only once all of that story's implementation tasks pass their tests.

### Parallel Opportunities

- T003–T004 (Setup) in parallel once T001–T002 exist.
- T006–T015 (Foundational models + integrations) in parallel once T005's reconciliation is
  done — all distinct files.
- T017–T020 (US1 tests) in parallel; T021–T023 are sequential (each depends on the prior stage's
  output shape), T024 follows all three.
- T025–T027 (US2 tests) in parallel; T028–T029 in parallel, T030 depends on both T021 (fallback
  already implemented there) and T029 (mapping to verify against) existing.
- T031–T033 (US3 tests) in parallel; T034 then T035 (T035 depends on T034's output existing).
- T036, T037, T039 in parallel; T038 depends on T036/T037 having run first.

---

## Parallel Example: Foundational Phase

```bash
# T005 (contract reconciliation) runs first and alone — it corrects the shapes below.

# Then launch all contract-model tasks together (same file, but independent definitions —
# coordinate merge order or split into separate commits):
Task: "Define PullRequestEvent model in src/contracts/models.py"
Task: "Define RoutingDecision model in src/contracts/models.py"
Task: "Define RetrievedContext model in src/contracts/models.py"
Task: "Define GeneratedReview model in src/contracts/models.py"
Task: "Define ReviewComment model in src/contracts/models.py"

# Launch all integration-abstraction tasks together (all distinct files):
Task: "Implement SSM abstraction + stub in src/integrations/config.py"
Task: "Implement S3 abstraction + stub in src/integrations/storage.py"
Task: "Implement Jev abstraction + stub in src/integrations/decision_engine.py"
Task: "Implement 9router/Gemini abstraction + stub in src/integrations/llm_router.py"
Task: "Implement GitHub abstraction + stub in src/integrations/github.py"
```

---

## Implementation Strategy

### MVP First (User Story 1 Only)

1. Complete Phase 1: Setup.
2. Complete Phase 2: Foundational (blocks everything else).
3. Complete Phase 3: User Story 1.
4. **STOP and VALIDATE**: run quickstart.md Scenario 1 and the T017–T020 tests independently.
5. This is the MVP — a PR gets an automated review comment end to end (SC-001). FR-010 (ARN
   publishing) is intentionally still open at this point (see Phase 3 caveat) and closes in
   Phase 6.

### Incremental Delivery

1. Setup + Foundational → foundation ready.
2. User Story 1 → validate independently → MVP.
3. User Story 2 → validate independently (cost-aware routing, SC-002) → still fully backed by
   User Story 1's working end-to-end path.
4. User Story 3 → validate independently (context enrichment) → richest reviews for complex PRs.
5. Polish → deployment tooling (closes FR-010), full-suite validation, remaining `CLAUDE.md`
   TODOs closed.

### Suggested MVP Scope

**User Story 1 only** (T001–T024): the pipeline already satisfies SC-001, SC-003/SC-004, and
SC-005 for the no-context path at that point — the decision-unavailable fallback is built into
T021, so a Jev outage during MVP validation still reaches a terminal outcome instead of
stalling/crashing — without needing routing sophistication (US2) or context retrieval (US3). FR-010
is the one MUST requirement not yet satisfied at this scope (see Phase 3 caveat); it is closed in
Phase 6.

---

## Phase 7: Convergence

**Purpose**: Close gaps found by `/speckit-converge` after `/speckit-implement` completed all of
Phases 1–6. Every behavior below is already correctly implemented in code; these tasks close a
documentation/contract inaccuracy and three missing automated-test verifications of edge-case
behavior that the code relies on but nothing currently proves.

- [ ] T040 Update the PostComment "Input" section in
  [contracts/step-io-contracts.md](./contracts/step-io-contracts.md) to document that
  `repository` (from the accumulated event's top-level `PullRequestEvent` fields) is required
  alongside `invokeLlm` — `src/post_comment/handler.py` reads `event["repository"]` to know which
  GitHub repo to post to, which the current doc text ("reads only `invokeLlm`") omits, per FR-012
  (contradicts)
- [ ] T041 Add a unit test (e.g. `tests/unit/test_storage_error_propagation.py`) asserting that
  `route_model`, `retrieve_context`, and `invoke_llm` each let `StorageError` propagate uncaught
  when their `diff_ref`/`context_ref` key is missing from storage, per the spec Edge Case
  "What happens when the referenced diff cannot be found or read from storage?" (missing)
- [ ] T042 Add a unit test (e.g. `tests/unit/test_invoke_llm_empty_review_failure.py`) asserting
  that `invoke_llm` lets `LlmRouterError` propagate uncaught — so `PostComment` is never reached —
  when the llm-router stub is configured with `empty_output=True`, per FR-008 (missing)
- [ ] T043 Add a unit test (e.g. `tests/unit/test_post_comment_failure_visibility.py`) asserting
  that `post_comment` lets `GitHubClientError` propagate uncaught when the GitHub client stub is
  configured with `fail=True`, per FR-007 (missing)
