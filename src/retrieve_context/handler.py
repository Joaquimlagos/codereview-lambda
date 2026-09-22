"""RetrieveContext Lambda: assembles project context, only invoked when RouteModel set
needsContext (FR-004).

KNOWN STUB, not real RAG: this handler does NOT search a vector index or any external
knowledge source. It only regex-parses the `diff --git a/X b/Y` header lines already
present in the diff text itself to list touched file paths ("sources"), then wraps that
file list around the same diff text as the "context" it stores. No project file content
beyond the diff is ever read. This is intentional for the project's current stage — see
research.md's "simple text/file-relevance based strategy" decision and the matching note in
README.md — but it means retrieved "context" today is really just the diff, annotated.
"""

import re

from contracts.models import PullRequestEvent, RetrievedContext
from integrations.config import resolve_config_value
from integrations.storage import S3Storage, Storage

_DIFF_FILE_PATTERN = re.compile(r"^diff --git a/(\S+) b/(\S+)", re.MULTILINE)

# Local .env/env var fallback, else the bucket name published by codereview-infra's s3.tf.
DIFF_BUCKET_ENV = "DIFF_BUCKET"
DIFF_BUCKET_SSM_PARAM = "/codereview/s3/pr-diffs-bucket-name"


def _default_storage() -> Storage:
    return S3Storage(bucket=resolve_config_value(DIFF_BUCKET_ENV, DIFF_BUCKET_SSM_PARAM))


def _extract_sources(diff_text: str) -> list[str]:
    """File paths touched by the diff; MAY be empty when nothing is identifiable (Edge Case)."""
    return sorted({match.group(2) for match in _DIFF_FILE_PATTERN.finditer(diff_text)})


def retrieve_context(event: dict, storage: Storage | None = None) -> dict:
    pr_event = PullRequestEvent.model_validate(
        {k: event[k] for k in ("pr_id", "repository", "revision", "diff_ref")}
    )
    storage = storage or _default_storage()

    # A diff that cannot be found/read MUST fail the run visibly (spec Edge Case).
    diff_text = storage.get_text(pr_event.diff_ref)
    sources = _extract_sources(diff_text)
    files_note = ", ".join(sources) if sources else "(no related files identified)"
    context_text = f"Files touched: {files_note}\n\n{diff_text}"
    context_ref = storage.put_text(
        f"context/{pr_event.pr_id}-{pr_event.revision}.txt", context_text
    )

    context = RetrievedContext(pr_id=pr_event.pr_id, context_ref=context_ref, sources=sources)
    return context.model_dump()


def handler(event: dict, context=None) -> dict:
    return retrieve_context(event)
