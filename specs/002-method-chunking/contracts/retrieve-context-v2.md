# Contract: RetrieveContext output, prompt layout, and log lines (v2)

Amends [001's step I/O contract](../../001-pr-review-pipeline/contracts/step-io-contracts.md),
§ RetrieveContext and § InvokeLLM. That file is updated in the same PR.

## RetrieveContext output (nested under `$.context`)

```json
{
  "pr_id": "3",
  "index_available": true,
  "index_version": 2,
  "chunks": [
    {
      "path": "src/main/java/com/codereview/app/auth/InMemoryUsers.java",
      "text": "static boolean isValid(String username, String password) { … }",
      "id": "src/main/java/com/codereview/app/auth/InMemoryUsers.java#InMemoryUsers.isValid(String,String):19-21",
      "start_line": 19,
      "end_line": 21,
      "header": "package com.codereview.app.auth;\n\nfinal class InMemoryUsers {\n    private static final Map<String, String> CREDENTIALS = …;",
      "score": 0.8123,
      "matched_query": "src/main/java/com/codereview/app/auth/AuthController.java"
    }
  ]
}
```

- `chunks` holds at most `TOP_N` = 8 entries, ranked by `score`, descending. Chunks whose
  lines overlap the diff's changed lines are never included (FR-020).
- With a v1 index: `index_version: 1`, at most 3 chunks, and only `path`/`text` set.
  This is byte-for-byte today's content.
- `index_version` is absent only when `index_available` is `false`. The output is dumped
  with `exclude_none`, which is also what keeps a v1 chunk exactly `{path, text}`.

## Prompt layout (v2), built by InvokeLLM per model attempt

```
=== ADDITIONAL PROJECT CONTEXT ===
Existing code from the repository, retrieved for reference only. It is NOT part of the
change under review — do not report issues in it.

--- src/main/java/com/codereview/app/auth/InMemoryUsers.java ---
package com.codereview.app.auth;

final class InMemoryUsers {
    private static final Map<String, String> CREDENTIALS = …;

    [lines 19-21]
    static boolean isValid(String username, String password) { … }
```

The layout is chosen from the chunks themselves: chunks that carry `start_line` (v2) are
grouped. Whole files (v1) keep the pre-002 flat layout byte for byte
(`tests/unit/test_build_prompt_grouped.py` holds it to a golden prompt).

Files appear in order of their best chunk's score. Inside a file, chunks are in line order
and the header is printed once. The `=== DIFF UNDER REVIEW ===` and
`=== ADDITIONAL PROJECT CONTEXT ===` markers are unchanged, because `measure_review.py`
splits the prompt on them.

## Log lines (machine-readable; the "after" measurement depends on them)

One JSON object per line, emitted through the module logger at INFO. Every line has an
`"event"` key and a `"pr"` key.

RetrieveContext, once per run:
```json
{"event": "rag_query", "pr": 3, "index_version": 2, "index_commit": "88801e4…", "queries": 5, "query_parts_split": 0, "excluded_overlapping": 4, "candidates": 46, "selected": 8,
 "scores": {"min": 0.61, "max": 0.84, "mean_selected": 0.80, "min_selected": 0.78, "mean_rest": 0.70, "max_rest": 0.77, "margin_at_cut": 0.01, "standardised_gap": 2.4}}
```
RetrieveContext, once per selected chunk, in rank order:
```json
{"event": "rag_chunk", "pr": 3, "rank": 1, "id": "…#InMemoryUsers.isValid(String,String):19-21", "score": 0.8123, "matched_query": "src/…/AuthController.java"}
```
InvokeLLM, once per model attempt:
```json
{"event": "llm_attempt", "pr": 3, "model": "groq:openai/gpt-oss-120b:medium", "budget": 4300, "estimated_prompt": 3950, "context_chunks_kept": 5, "context_chunks_dropped": 3, "skipped": false}
```
With `"skipped": true`, the client was not called (FR-024). A skip is also logged as a
plain-text warning (`Skipping <model> for PR <n>: …`). If every model in the tier was
skipped, the run fails with `LlmPromptTooLargeError`. That error is deliberately not retried:
codereview-infra's Retry matches `LlmTransientError` by name only.

`scores` in `rag_query` is the distribution over all candidates scored after exclusion. The
"after" measurement reads it from here (spec FR-033, SC-012). RetrieveContext's v1 path logs
no `rag_*` lines. InvokeLLM logs `llm_attempt` on every attempt, v1 included; for v1 context
(and for reviews with no context) the line is informational only, since packing and skipping
apply to located (v2) context alone. That keeps the Lambda-first deploy neutral (data-model.md).

The existing plain-text lines (`… answered: … usage=…`, `Model … failed for PR …`) are
kept unchanged. The measurement script reads the provider's `prompt_tokens` from them.
