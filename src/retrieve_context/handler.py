"""RetrieveContext Lambda: semantic retrieval over codereview-app's RAG index (FR-004).

Only invoked when RouteModel set needsContext. Reads the embedding index published by
codereview-app (`index/develop/index.json` in the same artifacts bucket the diff lives in),
embeds the PR diff as a retrieval *query*, and returns the TOP_K most cosine-similar chunks.

Index contract (owned by codereview-app's scripts/build_index.py, mirrored in
specs/001-pr-review-pipeline/contracts/step-io-contracts.md):

    {"version": 1, "branch": "develop", "commit": "<sha>", "generatedAt": "<iso8601>",
     "model": "gemini-embedding-001", "dimensions": 768,
     "chunks": [{"path": "src/...", "text": "<file contents>", "vector": [768 floats]}]}

`model` and `dimensions` are verified against this Lambda's own embedding client before any
scoring: vectors produced by a different model, or truncated to a different dimensionality,
are not comparable, so a mismatch is a hard failure rather than a silently meaningless
ranking. A missing index is different in kind — it just means develop has not been indexed
yet — so that degrades gracefully to "no context" instead of failing the run.
"""

import json
import math

from contracts.models import ContextChunk, PullRequestEvent, RetrievedContext
from integrations.config import require_env
from integrations.embeddings import EmbeddingClient, GeminiEmbeddingClient
from integrations.secrets import resolve_api_key
from integrations.storage import S3Storage, Storage, StorageError

# Published by codereview-app's index-codebase.yml workflow on every push to develop.
INDEX_KEY = "index/develop/index.json"
TOP_K = 3

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


def _cosine_similarity(vector_a: list[float], vector_b: list[float]) -> float:
    """Plain-Python cosine similarity — no numpy: at a few hundred 768-float vectors per run
    this is tens of milliseconds, and skipping numpy keeps the other three Lambdas from
    carrying its ~57 MB of vendored OpenBLAS in the shared deployment zip for no benefit.

    A zero-norm vector scores 0 rather than raising a ZeroDivisionError — such a vector is
    degenerate and should simply never rank, not blow up the whole retrieval.
    """
    dot_product = sum(a * b for a, b in zip(vector_a, vector_b, strict=True))
    norm_a = math.sqrt(sum(a * a for a in vector_a))
    norm_b = math.sqrt(sum(b * b for b in vector_b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot_product / (norm_a * norm_b)


def _top_chunks(chunks: list[dict], query_vector: list[float], top_k: int) -> list[ContextChunk]:
    """The `top_k` chunks with the highest cosine similarity to `query_vector`."""
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
        return RetrievedContext(pr_id=pr_id, chunks=[], index_available=False).model_dump()

    index = json.loads(index_raw)

    # A diff that cannot be found/read MUST fail the run visibly (spec Edge Case).
    diff_text = storage.get_text(pr_event.diff_key)
    query_vector = embedding_client.embed_query(diff_text)

    _verify_index_compatibility(index, embedding_client, query_vector)

    chunks = _top_chunks(index.get("chunks") or [], query_vector, TOP_K)
    context = RetrievedContext(pr_id=pr_id, chunks=chunks, index_available=True)
    return context.model_dump()


def handler(event: dict, context=None) -> dict:
    return retrieve_context(event)
