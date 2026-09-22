# Step Functions I/O Contracts

Phase 1 output for [plan.md](./plan.md), reconciled against `codereview-infra`'s actual
`statemachine/definition.asl.json.tpl` and `lambda_arns.tf`. Defines the input/output shape
each Lambda MUST accept and produce (FR-012), built from the entities in
[data-model.md](../data-model.md).

> **Authoritative source note**: `codereview-infra` owns the Step Functions definition and is the
> authoritative contract (see spec Assumptions). The schemas below are this repo's implementation
> of that contract, reconciled directly against the real ASL template; if `codereview-infra`'s
> definition changes, the `codereview-infra` definition wins and this file MUST be updated again.
> This happened once already — the ResultPath keys and the `needsContext` field name below were
> both silently renamed upstream (from `$.routeModel`/`$.retrieveContext`/`$.invokeLlm` and
> `needsRag`) between two reconciliation passes on this repo. Re-verify against the live ASL
> before trusting this file blindly on a future change.

All payloads are JSON, validated at each Lambda boundary via the shared `pydantic` models in
`src/contracts/models.py` (one model per shape below), and exercised by `tests/contract`.

**Result shape (reconciled)**: each state's `ResultPath` in the ASL (`$.routing`, `$.context`,
`$.analysis`, `$.postComment`) nests that Lambda's *raw, unwrapped* return value under a
namespaced key inside the growing accumulated event — Step Functions does this nesting
automatically. Handlers therefore only ever *return* the flat shape documented below for their
own stage; they *receive* the whole accumulated event built up by every prior stage, and read
only the keys they need from it (see "Input" per stage).

## RouteModel

**Input** (`PullRequestEvent`, the original trigger event, unnested):
```json
{
  "pr_id": "string",
  "repository": "string",
  "revision": "string",
  "diff_ref": "string"
}
```

**Output** (`RoutingDecision` — returned flat; Step Functions nests it under `$.routing`):
```json
{
  "complexity": "low | medium | high",
  "needsContext": "boolean"
}
```
Field name confirmed against the ASL's `CheckNeedsContext` `Choice` state, which branches on
`$.routing.needsContext` — not `needsRag` (an earlier, since-renamed reconciliation) and not
`context_needed` (the original, never-verified assumption).

On decision-engine failure, output MUST still be well-formed: `{"complexity": "medium", "needsContext": false}`.

## RetrieveContext

Only invoked when the ASL's `CheckNeedsContext` state finds `$.routing.needsContext` is `true`;
skipped entirely otherwise (FR-005) — the Step Functions definition in `codereview-infra` owns
that branch, not this Lambda.

**Input**: the accumulated event — original `PullRequestEvent` fields at the top level, plus
`routing` (the `RoutingDecision` above) nested by Step Functions:
```json
{
  "pr_id": "string", "repository": "string", "revision": "string", "diff_ref": "string",
  "routing": { "complexity": "low | medium | high", "needsContext": true }
}
```

**Output** (`RetrievedContext` — returned flat; nested under `$.context`):
```json
{
  "pr_id": "string",
  "context_ref": "string",
  "sources": ["string", "..."]
}
```
`sources` MAY be `[]`.

## InvokeLLM

**Input**: the accumulated event — top-level `PullRequestEvent` fields, `routing`
(`RoutingDecision`), and `context` (`RetrievedContext`) when `RetrieveContext` ran (the key is
absent from the event entirely when it was skipped, per FR-005).

**Output** (`GeneratedReview` — returned flat; nested under `$.analysis`):
```json
{
  "pr_id": "string",
  "review_text": "string",
  "model_used": "string"
}
```
`review_text` MUST NOT be empty; a generation failure MUST NOT produce this shape at all (FR-008)
— the pipeline surfaces the failure instead of emitting a hollow `GeneratedReview`.

## PostComment

**Input**: the accumulated event; `PostComment` reads `analysis` (the `GeneratedReview` above)
and the top-level `repository` field from it — it needs `repository` to know which GitHub repo
to post the comment to (`src/post_comment/handler.py` reads `event["repository"]` directly).

**Output** (`ReviewComment` — returned flat; nested under `$.postComment`):
```json
{
  "pr_id": "string",
  "comment_id": "string",
  "posted": "boolean"
}
```
A failed post MUST raise/flag visibly rather than return `posted: false` silently swallowed
(FR-007, spec Edge Case) — `posted: false` is only ever surfaced through a monitored failure path,
never as a quiet successful-looking output.

## Cross-cutting rules

- Every field above is required unless explicitly marked optional/absent.
- No stage inlines diff or context content — only reference keys (`diff_ref`, `context_ref`)
  cross stage boundaries (FR-013).
- Every shape is validated at both the producing and the consuming end in `tests/contract`
  (SC-004): producers assert their *own returned* shape matches the model; consumers assert they
  correctly read their expected keys out of a fixture accumulated event, with no adapter/
  translation step beyond reading the nested key Step Functions already put there.
- `tests/contract/test_route_model_contract.py` additionally asserts `RoutingDecision`'s raw
  dict keys exactly (not just pydantic validation), specifically to catch another silent rename
  of `needsContext` before it reaches production.
