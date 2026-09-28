# Data Model: Method-Level Chunking

**Feature**: [spec.md](spec.md) · **Contracts**: [contracts/](contracts/)

## Index (v2), written by codereview-app, read by RetrieveContext

| Field | Type | Rule |
|---|---|---|
| `version` | int | `2` |
| `branch`, `commit`, `generatedAt`, `model`, `dimensions` | as in v1 | unchanged; `model`/`dimensions` checked as today |
| `chunks` | `IndexChunk[]` | non-empty |

### IndexChunk

| Field | Type | Rule |
|---|---|---|
| `id` | str | unique within the index, stable across builds for unchanged code (FR-011) |
| `path` | str | repo-relative POSIX path; **same meaning as v1** |
| `kind` | `"method"` \| `"type"` \| `"block"` | |
| `symbol` | `{type: str, method: str \| null}` \| null | `type` is the dotted enclosing chain (`Outer.Inner`); null for blocks |
| `startLine`, `endLine` | int | 1-based, inclusive, `startLine ≤ endLine`, lines of the *source file* |
| `part` | `{index: int, count: int}` \| null | set only on split chunks (FR-006) |
| `header` | str | may be `""` for blocks |
| `text` | str | the body; **same field name as v1**, non-empty |
| `vector` | float[768] | embedding of `header + "\n" + text` (or `text` when the header is empty); **same field as v1** |

**`id` grammar**
- method: `{path}#{symbol.type}.{method}({ParamType,…}):{startLine}-{endLine}`
- type: `{path}#{symbol.type}:{startLine}-{endLine}`
- block: `{path}#L{startLine}-{endLine}`
- split part: any of the above + `#part-{index}` (1-based)

**Validation at build time**: every `id` is unique; `estimate(header + text) ≤ 1,800`
(research R3); every vector has length `dimensions`.

## Diff query, built inside RetrieveContext (not persisted)

| Field | Type | Rule |
|---|---|---|
| `path` | str | the file's `b/` path (`a/` path for deletions) |
| `part` | `{index, count}` \| null | set when the file diff was split (research R7) |
| `text` | str | the file's diff section (or part), with its `diff --git`/`---`/`+++` lines |
| `vector` | float[768] | `RETRIEVAL_QUERY` embedding |

## Changed-line set, derived from the diff (not persisted)

`dict[old_path, set[int]]`: old-side line numbers that are removed, plus both neighbours of
pure insertions (research R8). It drives FR-020's exclusion.

## ContextChunk, the RetrieveContext → InvokeLLM contract (`src/contracts/models.py`)

| Field | Type | v1 source | v2 source |
|---|---|---|---|
| `path` | str (required) | index `path` | index `path` |
| `text` | str (required) | whole file | chunk body |
| `id` | str \| None = None | — | index `id` |
| `start_line`, `end_line` | int \| None = None | — | index lines |
| `header` | str \| None = None | — | index `header` |
| `score` | float \| None = None | — | max cosine (FR-019) |
| `matched_query` | str \| None = None | — | path of the argmax query |

Every new field is optional with a `None` default, so a v1-built `RetrievedContext` still
validates, and an InvokeLLM that ignores them still works.

`RetrievedContext` gains an optional `index_version: int | None = None` (1 or 2). It
selects the prompt layout: flat for v1, exactly as today, and grouped by file for v2.

## Provider budget (constant table in `llm_router.py`)

`(provider, reasoning) → max estimated prompt tokens`. Values are in research R9.
`CONTEXT_TOKEN_CAP = 3000`.

## State of one model attempt (router, per attempt)

```
budget = PROMPT_BUDGET[provider, reasoning]
room   = min(CONTEXT_TOKEN_CAP, budget − estimate(instructions + diff))
room < 0            → SKIPPED (logged, client not called)
else pack(chunks, room) → CALLED → answered | transient | model-gone | permanent
```
At exhaustion: all SKIPPED → `LlmPromptTooLargeError`; otherwise today's rules.
