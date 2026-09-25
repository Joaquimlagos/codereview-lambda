"""RouteModel Lambda: complexity + RAG-need decision via Jev (FR-001, FR-002, FR-003).

Never touches S3/the diff body — the "state" sent to Jev is built entirely from the
lightweight stats already on the incoming event (files_changed/lines_added/lines_removed/
paths). Reading the actual diff only happens later, in RetrieveContext, and only if Jev's
needsContext answer comes back true.
"""

from contracts.models import Complexity, PullRequestEvent, RoutingDecision
from integrations.config import require_env
from integrations.decision_engine import DecisionEngine, DecisionEngineError, JevDecisionEngine
from integrations.secrets import resolve_api_key

# Fixed fallback when Jev is unavailable/invalid, so the run still reaches a terminal outcome
# (spec.md Assumptions; SC-005) — a whole-pipeline concern, not User Story 2's. Field name is
# `needsContext`, matching codereview-infra's Choice state ($.routing.needsContext) — see
# contracts/step-io-contracts.md.
FALLBACK_DECISION = RoutingDecision(complexity=Complexity.MEDIUM, needsContext=False)

# Local .env/env var fallback, else Secrets Manager via the secret ARN Terraform sets as an env var
# (this function's execution role MUST NOT have access to invoke-llm's Gemini secret).
TYPESAFE_API_KEY_ENV = "TYPESAFE_API_KEY"
TYPESAFE_API_KEY_SECRET_ARN_ENV = "TYPESAFE_API_KEY_SECRET_ARN"

# The Jev "systemone" endpoint is always TYPESAFE_API_BASE + this fixed path — TYPESAFE_API_BASE
# is the single source of truth (FR-009); no separate JEV_ENDPOINT variable is read.
JEV_SYSTEMONE_PATH = "/v1/systemone"


def _default_decision_engine() -> DecisionEngine:
    api_key = resolve_api_key(TYPESAFE_API_KEY_ENV, TYPESAFE_API_KEY_SECRET_ARN_ENV)
    endpoint = require_env("TYPESAFE_API_BASE") + JEV_SYSTEMONE_PATH
    return JevDecisionEngine(endpoint=endpoint, api_key=api_key)


def route_model(event: dict, decision_engine: DecisionEngine | None = None) -> dict:
    pr_event = PullRequestEvent.model_validate(event)
    decision_engine = decision_engine or _default_decision_engine()

    try:
        decision = decision_engine.classify(
            files_changed=pr_event.files_changed,
            lines_added=pr_event.lines_added,
            lines_removed=pr_event.lines_removed,
            paths=pr_event.paths,
        )
    except DecisionEngineError:
        decision = FALLBACK_DECISION

    return decision.model_dump()


def handler(event: dict, context=None) -> dict:
    return route_model(event)
