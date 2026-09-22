"""InvokeLLM Lambda: Gemini review generation, model tier from RouteModel (FR-006).

Accepts an optional retrieved context (present only when RetrieveContext ran) so the generated
review reflects both diff and context when available (User Story 3).
"""

from contracts.models import Complexity, PullRequestEvent
from integrations.config import require_env, resolve_config_value
from integrations.llm_router import GeminiLlmRouter, LlmRouter
from integrations.secrets import resolve_api_key
from integrations.storage import S3Storage, Storage

# Local .env/env var fallback, else Secrets Manager via this Lambda's own SSM-looked-up ARN
# (this function's execution role MUST NOT have access to route-model's TypeSafe secret).
GEMINI_API_KEY_ENV = "GEMINI_API_KEY"
GEMINI_API_KEY_SSM_ARN_PARAM = "/codereview/secrets/gemini-api-key-arn"

# Local .env/env var fallback, else the bucket name published by codereview-infra's s3.tf.
DIFF_BUCKET_ENV = "DIFF_BUCKET"
DIFF_BUCKET_SSM_PARAM = "/codereview/s3/pr-diffs-bucket-name"


def _default_storage() -> Storage:
    return S3Storage(bucket=resolve_config_value(DIFF_BUCKET_ENV, DIFF_BUCKET_SSM_PARAM))


def _default_llm_router() -> LlmRouter:
    api_key = resolve_api_key(GEMINI_API_KEY_ENV, GEMINI_API_KEY_SSM_ARN_PARAM)
    return GeminiLlmRouter(api_base=require_env("GEMINI_API_BASE"), api_key=api_key)


def invoke_llm(
    event: dict,
    storage: Storage | None = None,
    llm_router: LlmRouter | None = None,
) -> dict:
    pr_event = PullRequestEvent.model_validate(
        {k: event[k] for k in ("pr_id", "repository", "revision", "diff_ref")}
    )
    # Read from "routing"/"context" — the ResultPath keys codereview-infra's ASL actually
    # nests RouteModel's and RetrieveContext's output under (confirmed against
    # statemachine/definition.asl.json.tpl); "context" is absent entirely when
    # RetrieveContext was skipped (FR-005).
    complexity = Complexity(event["routing"]["complexity"])
    storage = storage or _default_storage()
    llm_router = llm_router or _default_llm_router()

    # A diff that cannot be found/read MUST fail the run visibly (spec Edge Case).
    diff_text = storage.get_text(pr_event.diff_ref)

    context_text = None
    retrieved_context = event.get("context")
    if retrieved_context is not None:
        context_text = storage.get_text(retrieved_context["context_ref"])

    # An empty/malformed result raises (LlmRouterError) instead of returning a hollow
    # GeneratedReview (FR-008) — left uncaught so the run fails visibly (SC-005) rather than
    # reaching PostComment with nothing usable.
    review = llm_router.generate_review(
        pr_id=pr_event.pr_id,
        diff_text=diff_text,
        complexity=complexity,
        context_text=context_text,
    )
    return review.model_dump()


def handler(event: dict, context=None) -> dict:
    return invoke_llm(event)
