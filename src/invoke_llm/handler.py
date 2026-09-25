"""InvokeLLM Lambda: LLM review generation, model tier from RouteModel (FR-006).

Each tier's fallback list can mix providers (Gemini and Groq); see
integrations/llm_router.py.

Accepts an optional retrieved context (present only when RetrieveContext ran) so the generated
review reflects both diff and context when available (User Story 3).
"""

from contracts.models import Complexity, ContextChunk, PullRequestEvent
from integrations.config import require_env
from integrations.llm_router import GeminiClient, GroqClient, LlmRouter, MultiProviderLlmRouter
from integrations.secrets import resolve_api_key
from integrations.storage import S3Storage, Storage

# Local .env/env var fallback, else Secrets Manager via the secret ARN Terraform sets as an env var
# (this function's execution role MUST NOT have access to route-model's TypeSafe secret).
GEMINI_API_KEY_ENV = "GEMINI_API_KEY"
GEMINI_API_KEY_SECRET_ARN_ENV = "GEMINI_API_KEY_SECRET_ARN"
GROQ_API_KEY_ENV = "GROQ_API_KEY"
GROQ_API_KEY_SECRET_ARN_ENV = "GROQ_API_KEY_SECRET_ARN"

# The top-level PullRequestEvent fields present on the accumulated Step Functions event —
# camelCase, matching what codereview-app actually publishes (contracts/models.py's
# alias_generator). Extracted explicitly rather than validating the whole event because the
# accumulated event also carries "routing" and, when present, "context".
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


def _default_llm_router(remaining_time_ms=None) -> LlmRouter:
    # Built lazily per provider: a key is only fetched if one of that provider's models is
    # actually tried.
    return MultiProviderLlmRouter(
        client_factories={
            "gemini": lambda: GeminiClient(
                api_base=require_env("GEMINI_API_BASE"),
                api_key=resolve_api_key(GEMINI_API_KEY_ENV, GEMINI_API_KEY_SECRET_ARN_ENV),
            ),
            "groq": lambda: GroqClient(
                api_base=require_env("GROQ_API_BASE"),
                api_key=resolve_api_key(GROQ_API_KEY_ENV, GROQ_API_KEY_SECRET_ARN_ENV),
            ),
        },
        remaining_time_ms=remaining_time_ms,
    )


def invoke_llm(
    event: dict,
    storage: Storage | None = None,
    llm_router: LlmRouter | None = None,
    remaining_time_ms=None,
) -> dict:
    pr_event = PullRequestEvent.model_validate({k: event[k] for k in _PR_EVENT_KEYS})
    # Read from "routing"/"context" — the ResultPath keys codereview-infra's ASL actually
    # nests RouteModel's and RetrieveContext's output under (confirmed against
    # statemachine/definition.asl.json.tpl); "context" is absent entirely when
    # RetrieveContext was skipped (FR-005).
    complexity = Complexity(event["routing"]["complexity"])
    # The event is self-describing about where its own diff lives — diff_bucket + diff_key
    # together are the S3 object reference, so storage is built from the event.
    storage = storage or S3Storage(bucket=pr_event.diff_bucket)
    llm_router = llm_router or _default_llm_router(remaining_time_ms)

    # A diff that cannot be found/read MUST fail the run visibly (spec Edge Case).
    diff_text = storage.get_text(pr_event.diff_key)

    # RetrieveContext returns the retrieved chunks inline (no second S3 round-trip here).
    # Both "context absent" (branch skipped, FR-005) and "context present but empty" (no RAG
    # index published yet, index_available=false) mean the same thing to the prompt: review
    # the diff alone.
    retrieved_context = event.get("context") or {}
    context_chunks = [
        ContextChunk.model_validate(chunk) for chunk in retrieved_context.get("chunks") or []
    ]

    # An empty/malformed result raises (LlmRouterError) instead of returning a hollow
    # GeneratedReview (FR-008) — left uncaught so the run fails visibly (SC-005) rather than
    # reaching PostComment with nothing usable.
    review = llm_router.generate_review(
        pr_id=str(pr_event.pr_number),
        diff_text=diff_text,
        complexity=complexity,
        context_chunks=context_chunks,
    )
    return review.model_dump()


def handler(event: dict, context=None) -> dict:
    # The router checks the Lambda's remaining time before each model attempt, so a slow
    # fallback chain ends in LlmTransientError (retried by Step Functions) rather than a
    # Lambda timeout.
    remaining_time_ms = context.get_remaining_time_in_millis if context is not None else None
    return invoke_llm(event, remaining_time_ms=remaining_time_ms)
