"""Unit tests: RouteModel's Jev-unavailable fallback (SC-005) and its InvokeLLM model mapping."""

from contracts.models import Complexity
from integrations.decision_engine import DecisionEngineError
from integrations.llm_router import STUB_MODEL_BY_COMPLEXITY
from invoke_llm.handler import invoke_llm
from route_model.handler import FALLBACK_DECISION, route_model


def test_decision_engine_failure_falls_back_to_medium(pr_event, stub_storage, stub_decision_engine):
    stub_decision_engine.fail = True

    output = route_model(pr_event, storage=stub_storage, decision_engine=stub_decision_engine)

    assert output == FALLBACK_DECISION.model_dump()
    assert output["complexity"] == "medium"
    assert output["needsContext"] is False


def test_fallback_tier_resolves_to_a_free_tier_model_with_no_special_casing(
    pr_event, stub_storage, stub_llm_router
):
    # T030: the fallback tier (medium) must resolve through the same tier->model mapping US2
    # built, with no special-casing needed in InvokeLLM.
    assert FALLBACK_DECISION.complexity in STUB_MODEL_BY_COMPLEXITY

    event = {**pr_event, "routing": FALLBACK_DECISION.model_dump()}
    review = invoke_llm(event, storage=stub_storage, llm_router=stub_llm_router)

    assert review["model_used"] == STUB_MODEL_BY_COMPLEXITY[Complexity.MEDIUM]


def test_decision_engine_error_is_the_documented_exception_type():
    assert issubclass(DecisionEngineError, Exception)
