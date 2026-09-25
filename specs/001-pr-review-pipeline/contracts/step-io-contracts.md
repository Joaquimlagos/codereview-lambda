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

**Input** (`PullRequestEvent`, the original trigger event, unnested — the real event published
by `codereview-app`, camelCase on the wire per `src/contracts/models.py`'s `alias_generator`):
```json
{
  "prNumber": "int",
  "repository": "string",
  "sha": "string",
  "diffBucket": "string",
  "diffKey": "string",
  "filesChanged": "int",
  "linesAdded": "int",
  "linesRemoved": "int",
  "paths": ["string", "..."]
}
```
RouteModel never reads the diff body — the Jev "state" is built entirely from
`files_changed`/`lines_added`/`lines_removed`/`paths`.

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
  "prNumber": 1, "repository": "string", "sha": "string",
  "diffBucket": "string", "diffKey": "string",
  "filesChanged": 1, "linesAdded": 1, "linesRemoved": 1, "paths": ["string"],
  "routing": { "complexity": "low | medium | high", "needsContext": true }
}
```

**Also read** — the RAG index at `index/develop/index.json` in the *same* bucket the event's
`diffBucket` names (codereview-infra's `s3.tf` provisions one artifacts bucket split by prefix:
`prs/` for diffs, `index/` for the index). The index object's shape is owned by codereview-app's
`scripts/build_index.py`, which is the authoritative source for it:
```json
{
  "version": 1, "branch": "develop", "commit": "string", "generatedAt": "string",
  "model": "gemini-embedding-001", "dimensions": 768,
  "chunks": [{ "path": "string", "text": "string", "vector": [0.0] }]
}
```
Two compatibility rules apply before any scoring, because an index built against a different
vector space produces a plausible-looking but meaningless ranking:

- `model` MUST equal the model this Lambda embeds its query with (`gemini-embedding-001`).
- `dimensions` MUST equal the length of the query vector it just produced (768).

A mismatch on either is a hard failure. A *missing* index object is different in kind — it only
means develop has not been indexed yet — and degrades to `indexAvailable: false` with no chunks.

**Output** (`RetrievedContext` — returned flat; nested under `$.context`):
```json
{
  "pr_id": "string",
  "chunks": [{ "path": "string", "text": "string" }],
  "index_available": true
}
```
`chunks` MAY be `[]`, and holds at most `TOP_K` (3) entries, ranked by cosine similarity against
the embedded diff. `index_available` is `false` only in the missing-index case above.

## InvokeLLM

**Input**: the accumulated event — top-level `PullRequestEvent` fields, `routing`
(`RoutingDecision`), and `context` (`RetrievedContext`) when `RetrieveContext` ran (the key is
absent from the event entirely when it was skipped, per FR-005).

`InvokeLLM` instructs Gemini (via `build_prompt`, `src/integrations/llm_router.py`) to reply
with ONLY a JSON object shaped `{"summary": "string", "comments": [{"path", "line", "body"}]}`,
with the diff's hunk headers preserved so the model can work out each `line`'s post-change
number. `parse_review_response` parses that response defensively: if it isn't valid JSON in
that shape, the whole raw response becomes `summary` and `comments` is forced to `[]`
(`parse_fallback: true` on the result) rather than failing the run — see research.md's
"Inline review comments" decision for why this degrades instead of raising. A genuinely *empty*
response is still a hard failure (`LlmRouterError`), unchanged from before.

**Model fallback**: each complexity tier resolves to an ordered list of
`provider:model[:reasoning]` entries (`LLM_MODELS_LOW/MEDIUM/HIGH`, comma-separated; providers
`gemini` and `groq`). `InvokeLLM` tries them in order, moving to the next entry when a model
fails transiently or no longer exists; any other failure stops immediately. Before each
attempt it stops if the Lambda has less than one full attempt's time left (50 s). The entry
that answered is reported in `model_used` (e.g. `"groq:openai/gpt-oss-120b:low"`), and
`fell_back` is `true` when it wasn't the tier's first choice.

**Errors** (the Lambda `errorType` Step Functions sees):

- `LlmTransientError`: every entry failed, at least one of them transiently (HTTP
  429/500/502/503/504, or a network timeout/connection error), or the Lambda ran out of time
  for another attempt. Safe to retry. `codereview-infra`'s `Retry` on the `InvokeLLM` state
  matches this exact string, so the class name MUST NOT change; each retry re-runs the whole
  list.
- `LlmModelNotFoundError`: every entry's model is gone (HTTP 404, or Groq's
  `model_not_found`/`model_decommissioned`). A configuration problem; not retried.
- `LlmRouterError`: a model failed permanently (400, 401, 403, a blocked response, or an empty
  one that wasn't cut off by the output limit). An empty answer caused by the output limit
  (Groq `finish_reason: "length"`, Gemini `finishReason: "MAX_TOKENS"`) is not permanent: the
  router tries the next entry, and counts it as transient if the whole list ends that way.
  Not retried.

See research.md's "Gemini model lifecycle and error classification" and "Multi-provider model
fallback" decisions.

**Output** (`GeneratedReview` — returned flat; nested under `$.analysis`):
```json
{
  "pr_id": "string",
  "summary": "string",
  "comments": [{ "path": "string", "line": 1, "body": "string" }],
  "model_used": "string",
  "fell_back": false,
  "parse_fallback": false
}
```
`summary` MUST NOT be empty; a generation failure (no candidates, or a genuinely empty
response) MUST NOT produce this shape at all (FR-008) — the pipeline surfaces the failure
instead of emitting a hollow `GeneratedReview`. `comments` MAY be `[]` — a review with nothing
line-specific to flag is valid, not an error. `line` is the line number on the file's
post-change ("+"/right) side.

## PostComment

**Input**: the accumulated event; `PostComment` reads `analysis` (the `GeneratedReview` above),
the top-level `repository` field (which GitHub repo to post to), and the top-level `sha` field
(which commit to anchor the review's inline comments to) — all read directly off the event
(`src/post_comment/handler.py`).

`PostComment` posts `summary` + `comments` as one GitHub PR review via
`POST /repos/{repo}/pulls/{pr}/reviews`, body `{"commit_id": sha, "body": summary,
"event": "COMMENT", "comments": [{"path", "line", "side": "RIGHT", "body"}]}`. `event` is
always `"COMMENT"` — never `APPROVE`/`REQUEST_CHANGES` — so the AI review stays advisory/
non-blocking. GitHub rejects the whole review with **422** if any comment's `line` isn't part
of the diff; `RestGitHubClient` catches that and falls back to a single plain comment via
`POST /repos/{repo}/issues/{pr}/comments` (`summary` + each comment's `path:line — body`
concatenated as text) rather than losing the review outright.

**Output** (`ReviewComment` — returned flat; nested under `$.postComment`):
```json
{
  "pr_id": "string",
  "comment_id": "string",
  "posted": "boolean"
}
```
`comment_id` holds whichever identifier GitHub returned for whatever was actually posted — the
review's `id` on success, or the fallback comment's `id` when the 422 path was taken; the shape
doesn't distinguish which happened (only the value in GitHub itself does). A failed post MUST
raise/flag visibly rather than return `posted: false` silently swallowed (FR-007, spec Edge
Case) — `posted: false` is only ever surfaced through a monitored failure path, never as a
quiet successful-looking output.

## Cross-cutting rules

- Every field above is required unless explicitly marked optional/absent.
- No stage inlines *diff* content — the diff crosses stage boundaries only as a reference
  (`diffBucket`/`diffKey`, FR-013). Retrieved context is the deliberate exception: at most 3
  chunks ride inline in `$.context`, which keeps `InvokeLLM`'s S3 access scoped to `prs/*` and
  costs one fewer round trip (see data-model.md).
- Every shape is validated at both the producing and the consuming end in `tests/contract`
  (SC-004): producers assert their *own returned* shape matches the model; consumers assert they
  correctly read their expected keys out of a fixture accumulated event, with no adapter/
  translation step beyond reading the nested key Step Functions already put there.
- `tests/contract/test_route_model_contract.py` additionally asserts `RoutingDecision`'s raw
  dict keys exactly (not just pydantic validation), specifically to catch another silent rename
  of `needsContext` before it reaches production.
