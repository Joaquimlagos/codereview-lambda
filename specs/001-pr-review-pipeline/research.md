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

## Diff/context bucket resolution (superseded once already)

- **Decision**: The S3 bucket is **not resolved by Lambda-level config at all**. It arrives as
  part of the event itself: `PullRequestEvent.diff_bucket` (wire alias `diffBucket`), alongside
  `diff_key` (wire alias `diffKey`), published directly by `codereview-app`. `RetrieveContext`
  and `InvokeLLM` construct their `S3Storage` from `pr_event.diff_bucket` — see
  `src/retrieve_context/handler.py` and `src/invoke_llm/handler.py`. `RouteModel` never touches
  S3 at all (its "state" to Jev is built from `files_changed`/`lines_added`/`lines_removed`/
  `paths`, all on the event too), and `PostComment` never touches S3 either.
- **Rationale**: The event is already self-describing about where its own diff/context artifacts
  live, so there is no need for each Lambda to independently resolve a shared bucket name via env
  var or SSM — that would just be a second source of truth that could drift from what
  `codereview-app` actually used to publish the diff. The same bucket also holds the RAG index
  `RetrieveContext` reads (`index/develop/index.json`), since `codereview-infra` provisions one
  artifacts bucket split by prefix — so one event field covers both artifacts that function
  needs. `RetrieveContext` writes nothing back: it returns its chosen chunks inline, which is why
  `InvokeLLM` reads only the diff from S3.
- **Superseded prior decision**: An earlier version of this repo resolved a `DIFF_BUCKET` value
  via `resolve_config_value(env_var, ssm_param)` in `src/integrations/config.py` — local
  `.env`/env var first, otherwise a plain `ssm:GetParameter` read at
  `/codereview/s3/pr-diffs-bucket-name` (published by `codereview-infra`'s `s3.tf`). That
  mechanism assumed the bucket was fixed, Lambda-level config rather than part of the event; once
  `PullRequestEvent` was reconciled against the real `codereview-app` event and turned out to
  already carry `diffBucket`/`diffKey` directly, the SSM/env-var resolution became redundant and
  was removed — along with `resolve_config_value`, `SsmConfigBackend`, `AwsSsmConfigBackend`, and
  `StubSsmConfigBackend` (all dead code with no remaining caller once `DIFF_BUCKET` was removed
  from `.env`/`.env.example`).
- **Alternatives considered**: Keeping the SSM-resolved `DIFF_BUCKET` as a cross-check against
  `pr_event.diff_bucket` (rejected — two sources of truth for the same value, with no defined
  behavior for what happens if they disagree, adds complexity Principle VI doesn't justify at
  this stage); reusing `resolve_api_key` directly for this (rejected even before the event-based
  redesign — its name and Secrets-Manager-specific backend would have misdescribed a plain SSM
  parameter read).

## Retrieval strategy for RetrieveContext

- **Decision**: Semantic retrieval against the embedding index `codereview-app` already
  publishes. `RetrieveContext` downloads `index/develop/index.json`, embeds the PR diff as a
  retrieval *query* with `gemini-embedding-001` at 768 dimensions, ranks the index's chunks by
  cosine similarity computed in plain Python (`math.sqrt` + `sum`/`zip`, no numpy), and returns
  the top 3 as `{path, text}` pairs inline. The index's declared `model`/`dimensions` are checked
  against the query side before scoring; a missing index degrades to "no context" instead of
  failing.
- **Rationale**: The index is an existing artifact of the other repo's `index-codebase`
  workflow, so this repo neither owns chunking nor pays to re-embed the codebase — it spends one
  embedding call per review. Embeddings are not a generative call, so this stays inside
  Principle III's budget. The asymmetric task types (`RETRIEVAL_DOCUMENT` at index time,
  `RETRIEVAL_QUERY` here) are what the model expects for retrieval.
- **Superseded prior decision**: The first implementation was an explicit stub — it regex-parsed
  `diff --git a/X b/Y` headers to list touched paths and echoed the diff back as "context",
  reading no project file content at all. It satisfied the contract shape but retrieved nothing,
  so `needsContext: true` bought the review no information it did not already have.
- **Alternatives considered**: Building the index inside this repo (rejected — duplicates
  `codereview-app`'s workflow and re-embeds the whole codebase per review, blowing the free-tier
  budget); keyword/BM25 search over file contents fetched from S3 (rejected — needs the corpus
  in the Lambda anyway, and the vectors are already published); a managed vector store such as
  OpenSearch Serverless or Bedrock KB (rejected for this phase — both are paid, and a few hundred
  768-float dot products run in tens of milliseconds either way, Principle VI); numpy for the
  ranking itself (rejected — at this scale plain Python is just as fast and avoids vendoring
  numpy's ~57 MB OpenBLAS into all four Lambdas' shared deployment zip for one function's use).

## Inline review comments

- **Decision**: `InvokeLLM` prompts Gemini for structured JSON
  (`{"summary", "comments": [{"path", "line", "body"}]}`) instead of free-form review text, and
  `PostComment` posts it via GitHub's Reviews API
  (`POST /repos/{repo}/pulls/{pr}/reviews`, `event: "COMMENT"`) so each comment is anchored to
  the diff line it's about, instead of landing as one undifferentiated issue comment.
  `event: "COMMENT"` (never `APPROVE`/`REQUEST_CHANGES`) is deliberate: the AI validation job is
  advisory and non-blocking, so this Lambda MUST NOT gate the PR on the model's opinion.
- **Rationale**: Inline comments are what makes an automated review actually actionable in
  GitHub's UI — a wall of text in one issue comment forces the reader to manually map each
  observation back to a line themselves. The Reviews API is the only GitHub endpoint that
  anchors a comment to a diff line at post time.
- **Line-validity risk and fallback**: GitHub validates every comment's `line` against the
  diff server-side and rejects the *entire* review with HTTP 422 if even one is wrong — there
  is no partial-success mode. Two mitigations, at different layers: (1) `build_prompt`
  instructs the model explicitly, with the hunk headers preserved in the diff text it's shown,
  to only use post-change ("+"/right-side) line numbers that actually appear in the diff; (2)
  `RestGitHubClient.post_review` still treats a 422 as a normal, handled outcome — it falls
  back to `post_comment` (the old plain issue-comment endpoint), concatenating `summary` and
  each comment's `path:line — body` into one comment body. A generic comment beats losing the
  review outright, and this fallback is exercised in `tests/contract/test_post_comment_contract.py`
  without needing a real GitHub call (`StubGitHubClient.reject_inline_comments`).
- **Malformed-JSON risk and fallback**: independently, Gemini can reply with something that
  isn't the instructed JSON at all (prose, a markdown-fenced block, truncated output).
  `parse_review_response` (`src/integrations/llm_router.py`) is defensive about this: on any
  parse/shape failure, the whole raw response becomes `summary` with `comments` forced to
  `[]`, and `parse_fallback: true` is set on the `GeneratedReview` plus a warning logged — so a
  degraded run is observable (and, over time, a signal the prompt needs adjusting) rather than
  either crashing the pipeline or silently discarding information. A genuinely *empty*
  response (no candidates, or empty text) is left as a hard failure (`LlmRouterError`),
  unchanged from before — that's still not something to paper over.
- **Payload size**: `comments` now rides inline in `$.analysis`, the same pattern Retrieved
  Context's `chunks` already uses. Step Functions caps state I/O at 256 KB; a pathological PR
  with very many long inline comments could in principle approach that. No fixture or scenario
  here comes close, and no truncation/capping logic exists — noted as an open risk in
  data-model.md rather than mitigated, since guessing at a cap without a real large-PR case to
  measure against would be premature.
- **Alternatives considered**: Keeping one free-form issue comment and having `PostComment`
  parse line references out of it client-side (rejected — reinvents what GitHub's own Reviews
  API already validates and anchors natively, for no benefit); `event: "REQUEST_CHANGES"` or
  `"APPROVE"` (rejected outright — makes the AI review gate the PR, contradicting the
  non-blocking design); dropping any comment with an unresolvable line client-side before
  posting instead of relying on GitHub's 422 (rejected — duplicates validation logic GitHub
  already performs authoritatively, and risks silently dropping a comment for a subtly
  different reason than GitHub would have); failing the whole run on a malformed Gemini
  response instead of the summary-only fallback (rejected — a degraded-but-posted review is
  more useful than none, and FR-008 already covers the genuinely-empty case separately).

## Gemini model lifecycle and error classification

- **Decision (models)**: *Superseded by "Per-tier model fallback lists" below — each tier is
  now an ordered list rather than one model.* The three tiers first mapped to
  `gemini-3.5-flash-lite` (low), `gemini-3.5-flash` (medium) and `gemini-3.8-flash` (high), set
  as `GEMINI_MODEL_LOW/MEDIUM/HIGH` in `infra/iam_invoke_llm.tf` and `.env`/`.env.example`. Only pinned, stable model ids are
  used: never a `-latest` alias (it silently switches the underlying model, so review
  behavior changes with no deploy) and never a `-preview` model (previews are typically
  shut down within months).
- **Why it changed**: The previous `gemini-2.5-*` family passed its announced shutdown dates,
  and the first real Step Functions execution failed with a 404 from `generateContent` on
  `gemini-2.5-flash-lite`. The lesson: Gemini models have a short lifecycle, and a model
  still appearing in `ListModels` does not guarantee `generateContent` will accept it — a
  retired model can be listed and still 404. Model ids are therefore plain config (env vars),
  never code, so swapping them is a Terraform apply, not a code change.
- **Why "high" is a flash model**: The high tier uses `gemini-3.8-flash` rather than a Pro
  model because this phase must stay inside the free tier (Principle III), and Pro models'
  free-tier quota is too small to rely on. In production, the high tier would be a Pro model.
- **Decision (errors)**: `GeminiLlmRouter` splits failures in two. HTTP 429, 500, 502, 503 and
  504 — plus network timeouts/connection errors — raise `LlmTransientError`; every other
  failure (400, 401, 403, 404, an empty or blocked response) raises `LlmRouterError`.
  `LlmTransientError` subclasses `LlmRouterError`, so in-repo `except LlmRouterError` still
  catches both.
- **Rationale**: Real calls returned 503 "high demand" — a condition that clears up on its
  own, where a retry is the right response. A 404 for a retired model, a 400 for a bad
  request or a 401/403 for a bad key will fail identically on every retry, so retrying them
  only burns free-tier quota and delays the visible failure. The exception *class name* is a
  cross-repo contract: Lambda reports it as the invocation's `errorType`, and
  `codereview-infra`'s Step Functions `Retry` matches on the exact string
  `LlmTransientError` (Step Functions matches names, not inheritance) — renaming it would
  silently disable the retry.
- **Response parsing for thinking models**: Thinking models (e.g. `gemini-3.5-flash`) can
  return several parts per candidate, marking internal reasoning with `"thought": true` and
  attaching an opaque `thoughtSignature` to answer parts. The answer is the concatenated
  `text` of every part without `"thought": true`; reading only `parts[0].text` could return
  the reasoning instead of the answer. The existing malformed-JSON fallback
  (`parse_fallback`) still applies to the concatenated text.
- **Alternatives considered**: Retrying inside the Lambda with backoff (rejected — Step
  Functions already offers declarative, visible retries per error name, and in-Lambda
  retries would hold the function, and its billed duration, open while waiting); treating
  every error as retryable (rejected — retrying a 404 for a retired model cannot succeed);
  `-latest` aliases to avoid future 404s (rejected — trades a loud, explicit failure for
  silent model drift).

## Per-tier model fallback lists

*Superseded by "Multi-provider model fallback" below: the lists now mix Gemini and Groq, live
in `LLM_MODELS_*`, and Gemma is out. Kept for the measurements and reasoning that led there.*

- **Decision**: Each `GEMINI_MODEL_LOW/MEDIUM/HIGH` is a comma-separated list, in order of
  preference, instead of a single model:

  | Tier | Models (in order) |
  |---|---|
  | low | `gemma-4-26b-a4b-it`, `gemini-3.5-flash` |
  | medium | `gemini-3.5-flash`, `gemma-4-26b-a4b-it` |
  | high | `gemini-3.8-flash`, `gemini-3.5-flash`, `gemma-4-26b-a4b-it` |

  `GeminiLlmRouter` tries the models in order. A transient failure (429/500/502/503/504 or a
  network timeout) moves on to the next model; a permanent failure (`LlmRouterError`: 400,
  401, 403, 404, an empty or blocked response) stops immediately without trying the rest.
  Only when every model has failed transiently does `LlmTransientError` reach Step
  Functions, whose `Retry` then re-runs the whole list. `GeneratedReview` reports the model
  that answered (`model_used`) and whether it wasn't the first choice (`fell_back`), so the
  fallback rate is visible in the Step Functions output.
- **Measurements** (real calls against the Gemini API on the free tier, trivial prompt):
  - `gemini-3.5-flash-lite`, `gemini-3.1-flash-lite`, and `gemini-3.6/3.7/3.8-flash`: returned
    503 under load, often enough that no single one of them can be a tier's only model.
  - `gemma-4-31b-it`: answered, but took 20–33 s even for a trivial prompt, close to or past
    the 30 s per-request HTTP timeout. Excluded.
  - `gemma-4-26b-a4b-it` (~2 s) and `gemini-3.5-flash` (~2–4 s): the only two that answered
    consistently. Every list ends in one of them, so each tier has a reliable backstop.
- **Why the order differs per tier**: low leads with the fast Gemma model (a small change
  doesn't need more). Medium leads with `gemini-3.5-flash` and backs off to Gemma. High leads
  with `gemini-3.8-flash`, the strongest model available on the free tier, backs off to
  `gemini-3.5-flash`, and ends with `gemma-4-26b-a4b-it` as a last resort. On the free tier
  the high tier is a flash model; in production it would be a Pro model (see the previous
  decision).
- **Why high ends with Gemma**: high first listed only the two Gemini models. In a real run,
  `gemini-3.8-flash` and `gemini-3.5-flash` both returned 503 at the same moment and the
  execution failed. `gemma-4-26b-a4b-it` was the most stable model in every test, so it now
  closes the high list too. A review from a smaller model is better than no review, and the
  degradation stays visible: `model_used` names Gemma and `fell_back` is `true`.
- **Why a permanent error doesn't fall back**: a 400, 401/403 or 404 means the request, the
  key or the model name is wrong. Another model would fail the same way or mask a
  configuration problem that needs fixing, so failing fast and visibly is the useful outcome.
- **Gemma compatibility**: the request carries all instructions as plain user text in
  `contents` — no `systemInstruction` and no `generationConfig` (e.g. JSON mode), which Gemma
  models served by the Gemini API may not accept. Gemma responses have a single part with no
  `thought`/`thoughtSignature` fields, which `_answer_text` already handles. If Gemma wraps the
  JSON in a markdown fence despite the prompt, `_strip_code_fence` removes it before parsing.
- **Timeout budget**: each attempt has a 30 s HTTP timeout. The longest list (high) has three
  models, so if every attempt runs to its timeout the model calls alone take ~90 s, plus cold
  start, the secret fetch and the S3 diff read. The `invoke-llm` Lambda timeout is therefore
  120 s (it was 60 s for single models, then 90 s for two-model lists). A list longer than
  three models would need it raised again. With the ASL's `Retry` (3 attempts, 5 s interval,
  backoff 2), the worst case before `InvokeLLM` finally fails is about 4 × 120 s plus 35 s of
  waits, roughly 8.5 minutes.
- **Alternatives considered**: One model per tier with Step Functions retries only (rejected —
  retrying a model that is 503-ing under load mostly waits for the same overload, while a
  different model is often available immediately); retrying the same model inside the Lambda
  before moving on (rejected — spends the time budget on the model that is already failing);
  including `gemma-4-31b-it` as a last resort (rejected — its 20–33 s latency would regularly
  hit the 30 s HTTP timeout and blow the Lambda's time budget).

## Multi-provider model fallback

- **Decision**: InvokeLLM calls two providers, Gemini and Groq, and each tier's fallback list
  mixes them. The lists live in `LLM_MODELS_LOW/MEDIUM/HIGH` (renamed from `GEMINI_MODEL_*`,
  since they are no longer Gemini-only) as `provider:model[:reasoning]` entries:

  | Tier | Entries (in order) |
  |---|---|
  | low | `groq:openai/gpt-oss-120b:low`, `gemini:gemini-3.5-flash:low` |
  | medium | `groq:openai/gpt-oss-120b:medium`, `gemini:gemini-3.5-flash:low` |
  | high | `gemini:gemini-3.5-flash:high`, `groq:openai/gpt-oss-120b:medium` (changed after this decision was first written — see "High-tier reasoning: why Gemini, not Groq" below for why Gemini leads rather than falls back) |

  Groq is called through its OpenAI-compatible `chat/completions` endpoint (`GroqClient`);
  Gemini through `generateContent` (`GeminiClient`). The optional third field sends the
  provider's reasoning control (`generationConfig.thinkingConfig.thinkingLevel` on Gemini,
  `reasoning_effort` on Groq), and nothing at all when absent.
- **Why**: a latency diagnosis on the real prompt of a failed run (PR #3, 3,409 tokens:
  instructions 258, diff 2,132, RAG context 1,020) showed the prompt size was not the
  problem. With default reasoning, `gemini-3.5-flash` did not answer within 180 s, and Gemma
  returned HTTP 500 after 108 s and 32 s, and no answer within 180 s even without the RAG
  context, while answering a trivial prompt in 2.8 s. Both are reasoning models (Gemma
  reported 54 thinking tokens even for the trivial prompt), and reviewing a real diff makes
  them reason for a long time. Limiting reasoning fixed it for Gemini: `gemini-3.5-flash`
  with `thinkingLevel: low` answered in 9.6 s with valid JSON and 3 comments (354 thinking
  tokens). A second provider removes the single point of failure behind the 503 "high
  demand" errors that ended several runs.
- **Measurements on Groq** (same prompt, same methodology):

  | Model | Setting | Result |
  |---|---|---|
  | `openai/gpt-oss-120b` | `reasoning_effort: low` | 200 in 3.0 s and 2.5 s, valid JSON, 5 and 8 comments |
  | `openai/gpt-oss-120b` | default (medium) | 200 in 5.5 s, valid JSON, 3 comments, 2,106 reasoning tokens |
  | `openai/gpt-oss-120b` | `reasoning_effort: medium` | 200 in 7.4 s, valid JSON, 4 comments, 2,745 reasoning tokens (3,029 of 3,072 output tokens) |
  | `openai/gpt-oss-120b` | `medium` + `max_completion_tokens` 4,500 / 5,500 / 5,000 | 200 in 8.1 / 7.2 / 9.0 s, valid JSON, 7 / 8 / 11 comments; **3,418 / 3,111 / 3,668 output tokens**, all above the ~3,072 default cap |
  | `openai/gpt-oss-120b` | `reasoning_effort: high` | 4 of 4 runs: `finish_reason: length` with **empty content**; 3 runs stopped at 3,072 output tokens (3,070 of them reasoning), and one with `max_completion_tokens: 8192` spent 8,190 on reasoning |
  | `openai/gpt-oss-20b` | `reasoning_effort: low` | 200 in 1.1–1.3 s, valid JSON, but **0 comments** both times |
  | `qwen/qwen3.8-27b` | default | 200 in 5.0 s, output cut at 2,048 tokens, no valid JSON; then 429 (its free-tier output limit is 1,000 tokens per minute) |

  gpt-oss returns its reasoning in a separate `message.reasoning` field, so `content` holds
  only the answer. Its output (reasoning plus answer) stops at about 3,072 tokens unless
  `max_completion_tokens` is set, and the free tier allows 8,000 tokens per minute on this
  model (from Groq's `x-ratelimit-*` headers), roughly one medium-effort review per minute;
  a 429 there falls back to Gemini. Groq charges that quota by tokens actually used, not by
  the reserved `max_completion_tokens`: with a fresh window, a 5,500 cap on the 2,794-token
  prompt (8,294 reserved, above the 8,000 quota) was accepted, not rejected with a 429.
- **Choices**: `gpt-oss-120b` leads every tier: it was the only model with valid, substantive
  reviews on every run. `gemini-3.5-flash:low` backs it up from the other provider. High gets
  more reasoning (`medium`, about 2.6× the reasoning tokens of `low`) rather than a different
  model; `high` itself never produced an answer (it reasons until the output cap, whatever
  the cap is). Medium was first identical to low, then moved to `medium` effort (valid JSON
  in every run) so that low and medium differ in reasoning effort. Left out: Gemma (failed 3
  of 3 real prompts, and rejects any reasoning setting with HTTP 400), `gemini-3.8-flash`
  (0 successes in 3 attempts, one a 503 after 90 s), `gpt-oss-20b` (fast, but found nothing
  on a PR where the 120b found 5–8 issues), and Qwen (output quota too small).
- **Why medium and high are the same configuration**: a limitation of the free tier, not a
  design choice. The natural high tier would be `gpt-oss-120b` at `high` effort, but that
  never finishes within the free tier's limits: it spends the whole output budget on
  reasoning and returns no answer, at a 3,072-token cap and at 8,192 alike. A still larger
  output budget isn't viable either: the 8,000 tokens-per-minute quota already allows only
  about one medium-effort review per minute, and a bigger budget would exhaust it with a
  single review. So high uses the strongest configuration that reliably answers, which is
  the same as medium's. In production, on a paid tier, the high tier would use a stronger
  model (a Pro-class or larger reasoning model) instead.
- **Output budget at medium effort**: Groq calls at `medium` effort send an explicit
  `max_completion_tokens` of 5,500 (`GROQ_MAX_COMPLETION_TOKENS` in `llm_router.py`). Without
  it, real medium reviews (3,111–3,668 output tokens) exceed the ~3,072 default and come back
  empty. 5,500 is about 1.5× the largest measured output and was accepted without a 429 (see
  above). Low effort used at most 1,073 output tokens, well under the default, so it sends
  no cap.
- **The `high`-effort empty-content failure also happens at `medium`, non-deterministically**:
  a real `invoke-llm` run against `codereview-app` PR #13 (`gpt-oss-120b`, `reasoning_effort:
  medium`, `max_completion_tokens: 5500`, prompt 2,531 tokens — well under any TPM ceiling)
  spent 5,498 of the 5,500 completion tokens on reasoning and returned empty content
  (`finish_reason: "length"`) — the same failure this document previously described only for
  `high` effort. It does not reproduce on every `medium` run at a similar prompt size (see
  "High-tier reasoning: why Gemini, not Groq" below: Groq `:medium` succeeded on first attempt
  across PR #3, #5, #7, `fell_back: false` every time), so this is run-to-run variance in how
  long the model reasons on the same prompt/model/effort tuple, not a deterministic function of
  prompt size. The existing `LlmOutputTruncatedError` handling already covers it (the router
  moves to the next list entry, `gemini:gemini-3.5-flash:low` for the medium tier), so the
  pipeline degrades correctly when this happens; the raised cap only lowers the probability of
  the failure, it does not eliminate it the way it might appear to from the `high`-only framing
  above.
- **Token budget of the rubric and security checklist, measured against #7/#8/#13**: the
  review-quality rubric and the security checklist (both above) add to every Groq prompt, so
  they were measured against `gpt-oss-120b`'s own tokenizer (`o200k_harmony`) to check they
  don't push a typical PR over the 8,000 TPM free-tier limit:

  | Piece | Tokens |
  |---|---|
  | Base instructions (JSON shape + line-number rules), no rubric | 269 |
  | + rubric | 525 (rubric alone: **256**) |
  | + security checklist (gated on an auth/security-sensitive path) | 688 (checklist alone: **163**) |

  | PR | Raw diff tokens (`gh pr diff`, gpt-oss tokenizer) | Touches an auth/security path? |
  |---|---|---|
  | `codereview-app` #7 | 1,461 | No |
  | `codereview-app` #8 | 6,531 | No |
  | `codereview-app` #13 | 1,298 | No |

  None of these three touch `auth/`, so the checklist's 163 tokens never actually apply to any
  of them today — it only adds cost on a future PR that touches that module.
- **Conclusion: no dynamic output cap — the free tier doesn't have room, and Cerebras is the
  real fix**: a `max_completion_tokens = 8000 - prompt_tokens - margin` cap was considered as a
  response to the medium-effort failure above, and rejected. For PR #13's actual 2,531-token
  prompt it computes to roughly 5,300 — nearly identical to the current fixed 5,500 — so it
  would not have prevented that failure. For a larger prompt it computes to *less* headroom for
  reasoning, which makes the same runaway-reasoning failure more frequent, not less; all it
  changes is trading an immediate 413 for an empty response several seconds later. The
  underlying problem is that Groq's free-tier 8,000 TPM ceiling simply does not have room for
  prompt + reasoning + answer on medium-to-large PRs (#8's 6,531-token diff alone leaves under
  1,500 tokens for reasoning and answer combined, before the checklist or a rubric-sized
  addition is even counted). `LlmOutputTruncatedError`'s existing move-to-next-model fallback
  already covers this for now; the structural fix is a third provider with a larger quota
  (Cerebras, 30K TPM on the same `gpt-oss-120b` model — already proposed, not yet implemented,
  in "High-tier reasoning: why Gemini, not Groq" above), not a smaller, prompt-dependent cap on
  the provider that's already too tight.
- **Error handling**: HTTP 404, and Groq's `model_not_found`/`model_decommissioned` codes
  (which Groq can send with HTTP 400), raise `LlmModelNotFoundError`, and the router moves to
  the next entry: providers remove free-tier models without notice. If every entry is gone,
  `LlmModelNotFoundError` reaches Step Functions and is not retried (a configuration
  problem). HTTP 413 (request too large) is in the transient set, so the router moves to the
  next entry when the prompt exceeds one model's limit. Groq answers a 413 with
  `code: "rate_limit_exceeded"` (the same code as a 429) when a single request is larger than
  the model's tokens-per-minute limit ("Limit 8000, Requested 18340" was captured from the
  real API for `gpt-oss-120b`), so the status, not the code, is what tells them apart. Unlike
  the other transient statuses it won't clear up by retrying the same model; if every entry
  answers 413, Step Functions' retry re-runs the same list with the same prompt. A model that
  hits its output limit before writing any answer (Groq
  `finish_reason: "length"`, Gemini `finishReason: "MAX_TOKENS"`, with empty content) raises
  `LlmOutputTruncatedError`, and the router also moves to the next entry, since another
  model may well answer; if the whole list ends that way, a retryable `LlmTransientError`
  with each model's details reaches Step Functions. Other empty responses, and
  400/401/403, stay permanent and stop the list.
- **Timeouts and deadline**: read timeout 30 s → 45 s, with a 5 s connect timeout, which is
  about 4.5× the slowest successful answer measured (9.6 s) while still cutting off slow
  failures. Lambda timeout 120 s → 150 s (three attempts at their limit). Before each attempt
  the router checks `context.get_remaining_time_in_millis()` and, if less than one full
  attempt (50 s: connect + read) is left, raises `LlmTransientError` naming the models that
  failed and those not attempted. Otherwise Lambda would kill the function mid-request, and
  Step Functions would see `Sandbox.Timedout`, which it does not retry and which carries no
  per-model detail.
- **Secrets**: the Groq key follows the same pattern as the others: codereview-infra
  publishes its ARN at `/codereview/secrets/groq-api-key-arn`, Terraform sets it as
  `GROQ_API_KEY_SECRET_ARN`, and the InvokeLLM role may read only that secret (plus Gemini's).
  Each provider's client, and its key, is only created when one of its models is actually
  tried.
- **Constitution**: Principle III used to require "three Gemini free-tier models" for
  generation. It was amended (1.1.1 → 1.2.0, MINOR) to "three complexity tiers, each served
  by free-tier models, with fallback across providers", without naming providers or models,
  which live here instead.
- **Alternatives considered**: a third field meaning "reasoning off" instead of "not sent"
  (rejected: the providers disagree on what "off" means, and Gemma rejects the setting
  entirely); treating an empty answer as retryable on the next model (not changed: an empty
  answer is still a hard failure per FR-008, and no measured model produced one with the
  chosen settings); an OpenAI SDK instead of plain HTTP for Groq (rejected: another
  dependency in the shared zip for one POST request).

## GitHub App authentication

- **Decision**: `PostComment` authenticates as a GitHub App installation instead of with a
  personal access token (PAT). `GitHubAppAuth` (`src/integrations/github.py`) signs a JWT
  with the App's private key (RS256; `iss` = App ID, `iat` backdated 60 s for clock drift,
  `exp` 9 minutes ahead, so the whole window is GitHub's 10-minute maximum), exchanges it at
  `POST /app/installations/{installation_id}/access_tokens` for an installation access
  token, and uses that token for the Reviews API calls. The token is cached at module level
  until 5 minutes before its expiry, so a warm Lambda reuses it rather than minting one per
  invocation. The App ID and installation ID are plain env vars; the private key comes from
  Secrets Manager (`GITHUB_APP_PRIVATE_KEY_SECRET_ARN`), or from a local `.pem` path
  (`GITHUB_APP_PRIVATE_KEY_PATH`) in development.
- **Rationale**:
  - **Bot identity**: reviews appear as `<app-name>[bot]`, clearly marked as automated,
    instead of as the person who owns the PAT, whose name would otherwise be attached to
    every AI review.
  - **Short-lived credentials**: what the Lambda sends to GitHub is an installation token
    that expires after 1 hour, not a long-lived key. The private key never leaves the
    function and is only used to sign 10-minute JWTs.
  - **Scoped permission**: the App holds exactly one repository permission (Pull requests:
    Read and write), and only on the repositories it is installed on. A PAT carries its
    owner's access, often far wider than one repository.
- **Why cache to 5 minutes before expiry**: minting a token is an extra GitHub API call per
  review, and GitHub rate-limits token creation. The 5-minute margin ensures a cached token
  is never used right as it lapses mid-request.
- **Dependencies**: `PyJWT[crypto]` pulls in `cryptography` and `cffi`, both compiled
  extensions. `infra/build_package.sh`'s Linux platform flags resolve them to
  `cryptography`'s `cp311-abi3` manylinux2014 wheel (the stable ABI, valid for cp314) and
  `cffi`'s native `cp314` manylinux2014 wheel, so no build step runs on the host.
- **Alternatives considered**: Keeping the PAT with a narrower fine-grained scope (rejected —
  it still posts as a person and remains a long-lived credential); a dedicated machine
  user account with its own PAT (rejected — costs a seat on an org, still a long-lived
  credential, and the App model is what GitHub recommends for integrations); GitHub
  Actions' `GITHUB_TOKEN` (rejected — only exists inside a workflow run, while `PostComment`
  runs in Lambda after the workflow has finished).
- **Migration status**: validated in production — PR #3's review was posted as
  `pr-analysis-ia[bot]`. The old PAT has been revoked on GitHub, and `GITHUB_TOKEN`/
  `GITHUB_TOKEN_SECRET_ARN` were removed from local `.env` and from every current config/code
  path once the App path was confirmed working; `codereview-infra`'s `github-token` secret
  itself is left in place (unused) rather than deleted, since removing infrastructure this
  repo doesn't own is out of scope here.

## Review quality rubric

- **Decision**: `build_prompt` now requires every entry in `comments` to carry a `category`
  (`bug`, `security`, `performance`, `maintainability`) and a `severity` (`low`, `medium`,
  `high`), and explicitly states that praise or a description of what the code does belongs
  in `summary` only, never in `comments`. The prompt also lists what to actively check per
  category, and instructs the model not to default to an empty `comments` list just because
  nothing is obviously broken.
- **Why**: real reviews on `codereview-app` PRs showed two failure modes. PR #7 (a small,
  correct validation change) got 7 inline comments, at least 3 of them pure praise/restatement
  ("`validateTitle(task)` is called before ID generation, **which is appropriate**...",
  "...also called before updating; **good for consistency**", "...could be made static... **but
  current implementation works**") — noise that dilutes the signal of the real findings in the
  same review. PR #8 (11 files, +686 lines, clean CRUD code) and PR #9 (47/-10, a CI/docs
  change) both got a valid, non-empty `summary` and `comments: []` — inspecting both diffs by
  hand found no real defect either, but on an 11-file diff, "no findings, not even a minor
  maintainability note" is itself a sign the model defaults to silence rather than actively
  checking; the previous prompt only described the JSON *shape*, never what to look for.
- **`category`/`severity` are required fields, not optional metadata.** A comment missing
  either fails `ReviewCommentDraft` validation inside `parse_review_response` — but unlike a
  top-level shape mismatch (bad JSON, missing `summary`, `comments` not a list), this is
  scoped to that one comment: it's discarded individually (logged, with a count of how many
  of how many total were dropped), while `summary` and every other, valid comment are kept
  and `parse_fallback` stays `false`. `parse_fallback: true` is reserved for the response
  being unusable at the top level, not for one unclassified comment among otherwise-good
  ones — an early version of this decision folded both into the same fallback, which meant a
  single malformed comment could silently throw away an entire good review.
- **Where category/severity surface**: GitHub's Reviews API has no dedicated fields for
  either, so `integrations/github.py`'s `_render_comment_body` prefixes them onto the posted
  text: `**[security · high]** <body>`. Used identically by the real review post and the
  422-fallback conversational comment.
- **Alternatives considered**: A numeric confidence/quality score instead of severity
  (rejected — severity answers "how much does this matter", which is what a reviewer
  triaging comments actually needs; confidence in the model's own correctness is a different,
  harder-to-calibrate thing); enforcing "no empty comments on large diffs" as a hard rule
  (rejected — a genuinely clean large diff is possible, and forcing a comment out of nothing
  would just reintroduce the noise problem from PR #7 in a different form; the rubric asks
  the model to check actively, but doesn't forbid a genuinely empty result).

## Security checklist for auth-sensitive changes

- **Decision**: When any path on the incoming event (`PullRequestEvent.paths`, not only the
  paths that happen to appear in `diff_text`) matches
  `SECURITY_SENSITIVE_PATH_PATTERN` (`auth|security|jwt|crypto|password|session|login|token`,
  case-insensitive), `build_prompt` appends a short, generic checklist: credential/secret
  logging, user enumeration or timing differences between failure reasons, signature/expiry/
  clock-skew bypass, missing authorization (identity checked but not permission), and
  hardcoded secrets.
- **Why, with evidence**: `codereview-app` PR #3 (a deliberately vulnerable auth change, run
  twice) never flagged its planted user-enumeration bug — the login endpoint returns a
  different response body ("User not found" vs. "Incorrect password") depending on whether
  the username exists. Reading the actual diff showed this was not a visibility problem: the
  latest review commented on the *exact same two lines* about a different issue (cleartext
  password logging), so the model read that code and picked one defect over the other, twice
  in a row across two independent runs. That is exactly what a checklist fixes — nothing in
  the prompt named "differing error responses" as a thing to check for.
- **Generic wording, deliberately**: the checklist describes categories of security issue,
  not this PR's specific bugs (no mention of a 24-hour clock skew constant, `InMemoryUsers`,
  or any of PR #3's actual code) — so it helps a real, different auth change, rather than
  only ever answering one demo's fixed answer key. `tests/unit/test_llm_router.py` has a test
  asserting the checklist text never repeats those specifics.
- **Gated on `paths`, not on file content**: cheaper (paths are already on the event, no
  extra parsing), and matches the same signal `RouteModel`'s complexity criteria already uses
  for the same reason (see "Complexity criteria: an explicit size clause" below) — a
  path-based heuristic, not a guarantee, but a reasonable proxy that costs nothing extra to
  compute.
- **Alternatives considered**: Scanning `diff_text` content for keywords instead of `paths`
  (rejected — more expensive, and `paths` already carries the same signal at effectively zero
  cost); a separate, dedicated LLM call for security-only review (rejected — spends a second
  generative call, which Principle III reserves for InvokeLLM's single review call; the
  checklist achieves the same effect inside the one call already being made).

## High-tier reasoning: why Gemini, not Groq

*Superseded by "High-tier order reverted: Groq leads again" below — the order this section
argued for was flipped back once Groq `:medium`'s own reliability assumption stopped holding.
Kept for the Groq-`:high` and Gemini-`:high` measurements, which are still the basis for
keeping Gemini `:high` in the list at all.*

- **Decision**: `LLM_MODELS_HIGH` is `gemini:gemini-3.5-flash:high,groq:openai/gpt-oss-120b:medium`
  — Gemini `:high` **leads**, Groq `:medium` is the **fallback**, the reverse of low/medium's
  order (both lead with Groq). Gemini's client-level read timeout, and the router's
  pre-attempt deadline budget, are both Gemini-high-specific overrides
  (`GEMINI_HIGH_REASONING_READ_TIMEOUT_SECONDS = 90`, `GEMINI_HIGH_REASONING_ATTEMPT_BUDGET_MS`),
  not a change to the shared defaults every other entry still uses. `invoke-llm`'s Lambda
  timeout goes from 150 s to 180 s to keep real margin for the new worst case.
- **Why Gemini leads instead of falling back**: the first version of this decision put Groq
  `:medium` first and Gemini `:high` second, matching low/medium's pattern. In practice, Groq
  `:medium` succeeds on the first attempt most of the time (confirmed across PR #3, #5, #7 —
  `fell_back: false` every time), so the deeper Gemini `:high` pass would be reached only on
  the rare attempt where Groq itself failed — making the high tier behave like medium's
  configuration in practice, despite being configured differently, and defeating the whole
  point of giving high-complexity PRs deeper scrutiny. Leading with Gemini `:high` means every
  high-tier review gets the deeper pass by default; Groq `:medium` only runs when Gemini
  itself fails (rate limit, overload, or the deadline check finding too little time left).
- **Why not Groq `:high`, measured directly**: the task's own suggestion was reasoning effort
  `high` plus a bigger output cap on Groq. Tested live against a realistic high-tier prompt
  (PR #3's diff + 3 RAG context files, ~2,790 prompt tokens on Groq's tokenizer): at `high`
  effort with `max_completion_tokens: 4800`, the entire cap was spent on reasoning and the
  call returned **empty content** (`finish_reason: "length"`, 4,798 of 4,800 completion
  tokens on reasoning alone) — this is `LlmOutputTruncatedError` territory, and it happens
  reliably at this prompt size, not occasionally. Raising the cap doesn't help either: Groq's
  8,000 TPM ceiling is a *pre-flight* check on `prompt_tokens + max_completion_tokens`
  regardless of tokens actually used, so a bigger cap on a ~2,790-token prompt crosses 8,000
  and gets an instant 413 instead. Every realistic high-tier prompt (RAG context pushes most
  of them well above 1,000 tokens) is stuck between "cap too small → truncates" and "cap too
  big → instant 413". Forcing Groq to `:high` here would take today's working first-attempt
  (proven in production on PR #3, #5, #7 — `fell_back: false` every time) and make it fail
  almost every time, adding ~10 s of wasted latency and quota before Gemini does the real
  work regardless.
- **Why Gemini `:high` works, measured directly**: the same realistic prompt against
  `gemini-3.5-flash` at `thinkingLevel: "high"` returned valid JSON with **6 comments**
  (`thoughtsTokenCount: 15018`) vs. **2 comments** at `thinkingLevel: "low"`
  (`thoughtsTokenCount: 1166`) on the identical input — a real, substantive difference in
  review depth, not a marginal one. Gemini's per-request ceiling is governed by its context
  window (~1M tokens for `gemini-3.5-flash`), not a fixed small TPM number the way Groq's is,
  so it doesn't hit the same wall.
- **The cost is latency, which now has to be budgeted honestly**: the `high` call took 63.4 s
  (vs. 9.6–31.2 s at `low` across separate measurements — real run-to-run variance already
  observed). That exceeds the shared `READ_TIMEOUT_SECONDS = 45`, so without a dedicated
  override this call would time out as a false `LlmTransientError` before Gemini even
  answered. `GeminiClient.generate` now picks a 90 s read timeout specifically when
  `reasoning == "high"` (45 s everywhere else), and the router's pre-attempt deadline check
  uses a matching 95 s budget for that one entry (`_attempt_budget_ms`), so Lambda's own
  timeout doesn't cut it off mid-request either.
- **Lambda timeout raised 150 s → 180 s**: worst case for the high tier is Gemini's `:high`
  attempt (first) failing after its full 95 s budget, then Groq's `:medium` fallback (second)
  using its full 50 s — 145 s total either way the list is ordered, since the sum of the two
  attempts' budgets doesn't depend on which runs first. 150 s left ~0 s of margin for that
  path; 180 s leaves real headroom, and the deadline check before the second attempt (Groq's
  50 s budget) is comfortably covered by whatever remains after Gemini's 95 s. This only
  changes `invoke-llm`'s own Lambda timeout, not `codereview-infra`'s Step Functions `Retry`
  (still `MaxAttempts: 1`, `IntervalSeconds: 30`), so the worst-case full failure is roughly
  2 × 180 s + 30 s ≈ 6.5 minutes (up from ~5.5 minutes at 150 s) — still fine for a
  non-blocking advisory check.
- **Alternatives considered**: Groq `:medium` first, Gemini `:high` as fallback (the initial
  version of this decision — superseded: it left the deeper pass almost never reached, since
  Groq `:medium` rarely fails); a third, higher-TPM free-tier provider for the high tier
  instead of retiming Gemini (e.g. Cerebras, 30K TPM on the same `gpt-oss-120b` model —
  proposed separately as a fix for fallback-exhaustion on oversized diffs, not implemented
  here); a flat, larger read timeout for every entry regardless of reasoning level (rejected —
  needlessly extends the budget check for the low/medium tiers and every other high-tier
  entry, none of which need anywhere near 90 s in practice).

## High-tier order reverted: Groq leads again

- **Decision**: `LLM_MODELS_HIGH` is now `groq:openai/gpt-oss-120b:medium,gemini:gemini-3.5-flash:high`
  — back to Groq `:medium` **leading**, Gemini `:high` as the **fallback**, matching low/medium's
  order. The Gemini-high-specific overrides (`GEMINI_HIGH_REASONING_READ_TIMEOUT_SECONDS = 90`,
  `GEMINI_HIGH_REASONING_ATTEMPT_BUDGET_MS`) and the 180 s Lambda timeout are unchanged — they're
  keyed to the entry's own `reasoning` spec, not its position in the list, so which slot Gemini
  `:high` occupies doesn't affect them, and the worst-case total (one 50 s budget plus one 95 s
  budget, whichever order) is the same 145 s either way.
- **Why the previous reasoning stopped holding**: "High-tier reasoning: why Gemini, not Groq"
  led with Gemini specifically because Groq `:medium` "succeeds on the first attempt most of the
  time (confirmed across PR #3, #5, #7 — `fell_back: false` every time)", so leading with Groq
  would have left the deeper Gemini pass "reached only on the rare attempt where Groq itself
  failed." That assumption no longer holds: PR #13 showed Groq `:medium` can also fail with
  empty content (`finish_reason: "length"`, reasoning alone exhausting the 5,500-token cap) —
  see "Multi-provider model fallback"'s note on this — non-deterministically, on a prompt far
  smaller than the one PR #3/#5/#7 were measured against. Groq's own first-attempt reliability
  was the entire justification for spending every high-tier review on the slower, 63 s Gemini
  `:high` call regardless; with that reliability now known to be imperfect, defaulting every
  review to the slow path bought certainty the data no longer supports, at a latency cost paid
  on every single high-tier review instead of only the ones where Groq actually fails.
- **What this trades away, honestly**: Gemini `:high`'s deeper pass (6 comments vs. 2 at `low`
  on the same prompt, per the still-valid measurement above) is now reached only when Groq's
  first attempt fails for any reason — 503, the empty-content failure, or a 413 — same as
  low/medium's pattern. A high-tier review where Groq's first attempt succeeds gets Groq
  `:medium`'s depth, not Gemini `:high`'s; this is the exact trade-off the previous decision
  was written to avoid, now accepted because the alternative (always paying Gemini `:high`'s
  latency) no longer rests on a first-attempt-reliability guarantee that has since been observed
  to fail.
- **Alternatives considered**: Keeping Gemini `:high` first regardless (rejected — pays the
  63 s cost on every high-tier review to guard against a Groq failure mode that, per the
  updated measurements, is real but not the norm); dropping Gemini `:high` from the list
  entirely and accepting Groq `:medium`'s depth as the high tier's ceiling (rejected — still
  loses the measured 6-vs-2-comment depth difference on the reviews where Groq does fail, for
  no benefit); a confidence-based order that picks Groq or Gemini first per PR based on some
  predicted failure likelihood (rejected — no signal exists to predict this per-PR, and Jev's
  routing decision already happens upstream of this list).

## Complexity criteria: an explicit size clause

- **Decision**: Jev's `"high"` tier criteria text (`decision_engine.py`) gained a size clause:
  "...OR changes more than roughly 400 lines or 10 files, regardless of what area it
  touches", alongside the existing domain clause ("Touches auth, security, concurrency, or
  many interconnected files").
- **Why**: `RouteModel` sends `files_changed`/`lines_added`/`lines_removed`/`paths` to Jev,
  whose criteria text is the only thing defining "high" — there is no line-count threshold in
  code (the `LOW_MAX_LINES`/`MEDIUM_MAX_LINES`/`classify_by_line_count` constants exist only
  for the local test stub, never read by the real `JevDecisionEngine`). Real classifications
  confirmed the criteria worked exactly as written, and exposed the gap: PR #5 (4 files,
  +12/-6, a Javadoc-only change) was classified `high`, purely because one changed path was
  `auth/InMemoryUsers.java` — the domain clause working as intended, even on a tiny diff. PR
  #8 (+686 lines, 11 files, no sensitive paths) was classified `medium` — nothing in the
  criteria ever let size alone reach `high`.
- **Residual uncertainty**: Jev is an external decision model, and criteria text is a
  strong instruction, not a deterministic rule — there is no guarantee it is honored exactly
  on every input, the same way an LLM-authored review comment isn't guaranteed either. This
  is recorded honestly rather than promised as fixed.
- **Alternatives considered**: Adding the size threshold as a second, code-side check ahead
  of/instead of Jev (rejected — `RouteModel`'s own docstring and Principle III are explicit
  that structured decisions like this belong to Jev, not a re-implemented rule in this repo);
  a lower/different numeric threshold (400 lines / 10 files chosen to mirror the pre-existing
  `MEDIUM_MAX_LINES = 400` stub constant, so the two don't silently disagree).
- **Known gap, confirmed post-deploy: this clause can turn a large PR's review from empty
  into completely absent.** Reclassifying PR #8 (11 files, +686 lines, no sensitive paths)
  from `medium` to `high` moved it onto `LLM_MODELS_HIGH`, and the real run failed outright —
  Step Functions' execution ended `FAILED`, so **no review was posted to GitHub at all**,
  where the old `medium` classification at least posted an empty-but-valid review. Both
  attempts hit the same wall: Groq's `:medium` fallback returned **HTTP 413 twice**, at
  **8,249 and 8,283 requested tokens** against its 8,000 TPM ceiling. PR #8's raw diff alone
  is ~6,531 tokens (see "Token budget of the rubric and security checklist" above); this
  round's rubric adds another 256 tokens on top of the pre-existing base instructions, and
  that's enough to push a request that was *already* close to the ceiling over it — this
  failure is not purely a consequence of the size clause reclassifying the PR; the rubric's
  own token cost is part of why the fallback no longer fits either. Gemini `:high`, the lead
  entry, failed too, but on the other resource: a 90-second client-side read timeout on the
  first attempt (the prompt is large enough — ~6,500 tokens — that Gemini's reasoning pass can
  run past `GEMINI_HIGH_REASONING_READ_TIMEOUT_SECONDS`), then an HTTP 503 on the Step
  Functions retry. For comparison, PR #3's real `:high` run (a much smaller ~600-token diff)
  completed in 84.7s, comfortably inside the same 90s timeout — so large diffs are the
  specific case where Gemini `:high` risks timing out, not a general problem with the model.
  Net: for a high-tier PR whose diff is already large, **both** entries in `LLM_MODELS_HIGH`
  are now plausibly unable to complete, and the failure mode is worse than before (nothing
  posted, vs. an empty review). This is an open problem, not yet fixed — the planned next step
  is a third, higher-TPM free-tier provider (Cerebras, 30K TPM on the same `gpt-oss-120b`
  model) as a second fallback behind Gemini `:high` and Groq `:medium`, tracked as follow-up
  work rather than blocking this round's rubric/checklist/logging changes.

## Per-call observability

- **Decision**: `GeminiClient.generate` and `GroqClient.generate` now log `finish_reason`
  (`finishReason` for Gemini) and the provider's own usage object on every successful call,
  not only on failure.
- **Why**: diagnosing PR #8's and #9's empty-but-valid reviews required reconstructing the
  real prompt from S3 and replaying it against the same model/effort after the fact — and
  even then, the replay's context chunks had already drifted from what was actually used,
  because the RAG index had been rebuilt in the meantime, making the replay's numbers
  unreliable evidence about the original call. The code had no record of what the model's
  `finish_reason` or reasoning-token usage actually was at the time. Logging it on the
  success path — the same information already logged on failure via `LlmOutputTruncatedError`
  — makes this answerable directly from CloudWatch next time, without needing to replay
  anything.
- **Where it lives**: structured log lines (`logger.info`), not a new field on
  `GeneratedReview`. Usage/finish_reason is per-attempt diagnostic detail, not something
  `PostComment` or the Step Functions output needs to carry forward — adding it there would
  grow the inline payload (`comments` already rides inline, see "Inline review comments") for
  no consumer.

## RetrieveContext: guarding against blank diff text

- **Decision**: Before embedding the diff as a retrieval query, `RetrieveContext` checks that
  its text is non-blank and raises `EmptyDiffError` (uncaught, failing the run visibly) if it
  isn't, instead of calling the embeddings API with empty text.
- **Why**: Gemini's `embedContent` rejects empty input with an HTTP 400, and that failure
  path was previously unhandled here specifically — `EmbeddingError` would still have
  surfaced it, just as Gemini's own opaque error text rather than a clear, named cause. A
  docs-only PR was the originally-suspected trigger, but a docs-only diff is never actually
  blank (`diff --git a/README.md b/README.md` plus a hunk header, at minimum) — the real risk
  is a genuinely empty or truncated S3 object, e.g. an interrupted or partial write from
  `codereview-app`'s upload step. That is a data-integrity problem, not a "nothing to review"
  situation, so it degrades the same way a missing diff key already does (`StorageError` is
  left uncaught, per the pre-existing "spec Edge Case" comment) rather than being silently
  swallowed or treated like the missing-*index* case (which legitimately means "nothing to
  compare yet" and does degrade gracefully).
- **Alternatives considered**: Treating an empty diff like a missing index
  (`index_available: false`-style graceful degradation) — rejected, because unlike a missing
  index (an expected, benign state before develop's first indexing run), an empty diff object
  should never happen at all and signals a real bug elsewhere in the pipeline; masking it
  would only make that bug harder to notice.

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
