"""RouteModel Lambda: complexity + RAG-need decision via Jev (FR-001, FR-002, FR-003)."""

from contracts.models import Complexity, PullRequestEvent, RoutingDecision
from integrations.config import require_env, resolve_config_value
from integrations.decision_engine import DecisionEngine, DecisionEngineError, JevDecisionEngine
from integrations.secrets import resolve_api_key
from integrations.storage import S3Storage, Storage

# Fixed fallback when Jev is unavailable/invalid, so the run still reaches a terminal outcome
# (spec.md Assumptions; SC-005) — a whole-pipeline concern, not User Story 2's. Field name is
# `needsContext`, matching codereview-infra's Choice state ($.routing.needsContext) — see
# contracts/step-io-contracts.md.
FALLBACK_DECISION = RoutingDecision(complexity=Complexity.MEDIUM, needsContext=False)

# Local .env/env var fallback, else Secrets Manager via this Lambda's own SSM-looked-up ARN
# (this function's execution role MUST NOT have access to invoke-llm's Gemini secret).
TYPESAFE_API_KEY_ENV = "TYPESAFE_API_KEY"
TYPESAFE_API_KEY_SSM_ARN_PARAM = "/codereview/secrets/typesafe-api-key-arn"

# The Jev "systemone" endpoint is always TYPESAFE_API_BASE + this fixed path — TYPESAFE_API_BASE
# is the single source of truth (FR-009); no separate JEV_ENDPOINT variable is read.
JEV_SYSTEMONE_PATH = "/v1/systemone"

# Local .env/env var fallback, else the bucket name published by codereview-infra's s3.tf.
DIFF_BUCKET_ENV = "DIFF_BUCKET"
DIFF_BUCKET_SSM_PARAM = "/codereview/s3/pr-diffs-bucket-name"


def _default_storage() -> Storage:
    return S3Storage(bucket=resolve_config_value(DIFF_BUCKET_ENV, DIFF_BUCKET_SSM_PARAM))


def _default_decision_engine() -> DecisionEngine:
    api_key = resolve_api_key(TYPESAFE_API_KEY_ENV, TYPESAFE_API_KEY_SSM_ARN_PARAM)
    endpoint = require_env("TYPESAFE_API_BASE") + JEV_SYSTEMONE_PATH
    return JevDecisionEngine(endpoint=endpoint, api_key=api_key)


def route_model(
    event: dict,
    storage: Storage | None = None,
    decision_engine: DecisionEngine | None = None,
) -> dict:
    pr_event = PullRequestEvent.model_validate(event)
    storage = storage or _default_storage()
    decision_engine = decision_engine or _default_decision_engine()

    # A diff that cannot be found/read MUST fail the run visibly (spec Edge Case) — left
    # uncaught, so the Lambda invocation itself fails and shows up in Step Functions/monitoring.
    diff_text = storage.get_text(pr_event.diff_ref)

    try:
        decision = decision_engine.classify(diff_text)
    except DecisionEngineError:
        decision = FALLBACK_DECISION

    return decision.model_dump()


def handler(event: dict, context=None) -> dict:
    return route_model(event)
