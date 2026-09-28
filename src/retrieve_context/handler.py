"""RetrieveContext Lambda: semantic retrieval over codereview-app's RAG index (FR-004).

Only invoked when RouteModel set needsContext. Reads the embedding index published by
codereview-app (`index/develop/index.json` in the same artifacts bucket the diff lives in),
embeds the PR diff as retrieval *queries*, and returns the most cosine-similar chunks.

Two index versions are read (index contract owned by codereview-app's
scripts/build_index.py; see specs/002-method-chunking/contracts/index-v2.md):

- **version 1** — one chunk per whole file. Handled exactly as before 002: the whole diff is
  one query, and the TOP_K files come back as `{path, text}`. Kept byte-for-byte so that
  deploying this Lambda before codereview-app publishes version 2 changes nothing
  (tests/unit/test_retrieve_context_versions.py holds it to a golden output).
- **version 2** — one chunk per method, type or block, each with its location and header.
  Each changed file's diff is its own query (diff_queries.py), chunks overlapping the diff's
  changed lines are left out, and the top chunks by best similarity are returned and logged
  (ranking.py).

Any other version fails the run, before any embedding call.

`model` and `dimensions` are verified against this Lambda's own embedding client before any
scoring: vectors produced by a different model, or truncated to a different dimensionality,
are not comparable, so a mismatch is a hard failure rather than a silently meaningless
ranking. A missing index is different in kind — it just means develop has not been indexed
yet — so that degrades gracefully to "no context" instead of failing the run.
"""

import json

from contracts.models import ContextChunk, PullRequestEvent, RetrievedContext
from integrations.config import require_env
from integrations.embeddings import EmbeddingClient, GeminiEmbeddingClient
from integrations.logging_config import configure_project_logging
from integrations.secrets import resolve_api_key
from integrations.storage import S3Storage, Storage, StorageError
from retrieve_context.diff_queries import changed_lines, split_queries
from retrieve_context.ranking import cosine_similarity as _cosine_similarity
from retrieve_context.ranking import log_ranking, rank

# Raises this project's own loggers to INFO (root logger and third-party loggers
# untouched) — see integrations/logging_config.py.
configure_project_logging()

# Published by codereview-app's index-codebase.yml workflow on every push to develop.
INDEX_KEY = "index/develop/index.json"
# Version 1 only: how many whole files to return. Version 2 uses ranking.TOP_N chunks.
TOP_K = 3
SUPPORTED_INDEX_VERSIONS = (1, 2)

# Same secret and resolution pattern InvokeLLM uses — this function needs the Gemini key to
# embed the diff (see infra/iam_retrieve_context.tf for the matching IAM grant).
GEMINI_API_KEY_ENV = "GEMINI_API_KEY"
GEMINI_API_KEY_SECRET_ARN_ENV = "GEMINI_API_KEY_SECRET_ARN"

# The top-level PullRequestEvent fields present on the accumulated Step Functions event —
# camelCase, matching what codereview-app actually publishes.
_PR_EVENT_KEYS = (
    "prNumber",
    "repository",
    "sha",
    "diffBucket",
    "diffKey",
    "filesChanged",
    "linesAdded",
    "linesRemoved",
    "paths",
)


class IndexCompatibilityError(Exception):
    """Raised when the index was built with a different embedding model/dimensionality."""


class EmptyDiffError(Exception):
    """Raised when the diff object was read successfully but its content is blank.

    Embedding blank text is not a "nothing to compare" situation the same way a missing
    index is: Gemini's embedContent rejects empty input with an opaque HTTP 400, and a blank
    diff is itself a real data problem — a docs-only PR's diff still has `diff --git`/hunk
    headers and is never actually empty, so this can only mean the S3 object is genuinely
    empty or was truncated on write. Fails the run visibly (spec Edge Case), same as a
    missing diff key, rather than degrading silently or surfacing Gemini's own error text.
    """


def _default_embedding_client() -> EmbeddingClient:
    api_key = resolve_api_key(GEMINI_API_KEY_ENV, GEMINI_API_KEY_SECRET_ARN_ENV)
    return GeminiEmbeddingClient(api_base=require_env("GEMINI_API_BASE"), api_key=api_key)


def _verify_index_compatibility(
    index: dict, client: EmbeddingClient, query_vector: list[float]
) -> None:
    index_model = index.get("model")
    if index_model != client.model:
        raise IndexCompatibilityError(
            f"Index was built with embedding model {index_model!r} but this function queries "
            f"with {client.model!r}; vectors from different models are not comparable. "
            "Rebuild the index (codereview-app's index-codebase workflow) or align the models."
        )

    index_dimensions = index.get("dimensions")
    if index_dimensions != len(query_vector):
        raise IndexCompatibilityError(
            f"Index declares {index_dimensions} dimensions but the query vector has "
            f"{len(query_vector)}; vectors of different lengths are not comparable. "
            "Rebuild the index or align outputDimensionality on both sides."
        )


def _index_version(index: dict) -> int:
    # Indexes published before `version` existed are version 1 by definition.
    version = index.get("version", 1)
    if type(version) is not int or version not in SUPPORTED_INDEX_VERSIONS:
        raise IndexCompatibilityError(
            f"Index declares version {version!r}; this function reads versions "
            f"{SUPPORTED_INDEX_VERSIONS}. Deploy a RetrieveContext that understands it, or "
            "restore a supported index (specs/002-method-chunking/contracts/index-v2.md)."
        )
    return version


def _top_chunks(chunks: list[dict], query_vector: list[float], top_k: int) -> list[ContextChunk]:
    """Version 1: the `top_k` chunks with the highest cosine similarity to `query_vector`."""
    scored = [(_cosine_similarity(chunk["vector"], query_vector), chunk) for chunk in chunks]
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [ContextChunk(path=chunk["path"], text=chunk["text"]) for _, chunk in scored[:top_k]]


def retrieve_context(
    event: dict,
    storage: Storage | None = None,
    embedding_client: EmbeddingClient | None = None,
) -> dict:
    pr_event = PullRequestEvent.model_validate({k: event[k] for k in _PR_EVENT_KEYS})
    # The event is self-describing about where its artifacts live: the diff and the index
    # share the one artifacts bucket (codereview-infra's s3.tf splits them by prefix).
    storage = storage or S3Storage(bucket=pr_event.diff_bucket)
    embedding_client = embedding_client or _default_embedding_client()
    pr_id = str(pr_event.pr_number)

    try:
        index_raw = storage.get_text(INDEX_KEY)
    except StorageError:
        # No index yet (nothing merged to develop): review the diff without project context
        # rather than failing the whole run.
        return RetrievedContext(pr_id=pr_id, chunks=[], index_available=False).model_dump(
            exclude_none=True
        )

    index = json.loads(index_raw)
    # Checked before the diff is even read: an unreadable index must not cost an API call.
    version = _index_version(index)

    # A diff that cannot be found/read MUST fail the run visibly (spec Edge Case).
    diff_text = storage.get_text(pr_event.diff_key)
    if not diff_text or not diff_text.strip():
        # A real diff is never blank (at minimum it has `diff --git`/hunk headers); an empty
        # or whitespace-only object at this key means something else went wrong writing it
        # (e.g. a truncated upload) — fail loudly rather than send Gemini empty text (400) or
        # silently return "no context" for what is actually a data problem.
        raise EmptyDiffError(
            f"Diff at {pr_event.diff_key!r} in bucket {pr_event.diff_bucket!r} is empty"
        )

    if version == 1:
        chunks = _retrieve_v1(index, diff_text, embedding_client)
    else:
        chunks = _retrieve_v2(index, diff_text, embedding_client, pr_event.pr_number)
    context = RetrievedContext(
        pr_id=pr_id, chunks=chunks, index_available=True, index_version=version
    )
    # exclude_none: a v1 chunk stays exactly {path, text}, as before version 2 existed.
    return context.model_dump(exclude_none=True)


def _retrieve_v1(
    index: dict, diff_text: str, embedding_client: EmbeddingClient
) -> list[ContextChunk]:
    """Unchanged pre-002 behaviour: the whole diff as one query, top-K whole files."""
    query_vector = embedding_client.embed_query(diff_text)
    _verify_index_compatibility(index, embedding_client, query_vector)
    return _top_chunks(index.get("chunks") or [], query_vector, TOP_K)


def _retrieve_v2(
    index: dict, diff_text: str, embedding_client: EmbeddingClient, pr_number: int
) -> list[ContextChunk]:
    queries = split_queries(diff_text)
    # One batch call for every changed file (more only past 100 queries).
    query_vectors = embedding_client.embed_queries([query.text for query in queries])
    _verify_index_compatibility(index, embedding_client, query_vectors[0])

    ranking = rank(index.get("chunks") or [], queries, query_vectors, changed_lines(diff_text))
    log_ranking(pr_number, index, queries, ranking)
    return [
        ContextChunk(
            path=ranked.chunk["path"],
            text=ranked.chunk["text"],
            id=ranked.chunk.get("id"),
            start_line=ranked.chunk.get("startLine"),
            end_line=ranked.chunk.get("endLine"),
            header=ranked.chunk.get("header"),
            score=round(ranked.score, 4),
            matched_query=ranked.matched_query,
        )
        for ranked in ranking.selected
    ]


def handler(event: dict, context=None) -> dict:
    return retrieve_context(event)
