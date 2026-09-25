"""Integration test: complexity tier routes to a lighter/heavier model (quickstart Scenario 2).

Validates SC-002.
"""

from contracts.models import Complexity
from integrations.llm_router import STUB_MODEL_BY_COMPLEXITY
from invoke_llm.handler import invoke_llm


def test_low_complexity_selects_lighter_model_than_high(pr_event, stub_storage, stub_llm_router):
    low_event = {**pr_event, "routing": {"complexity": "low", "needsContext": False}}
    high_event = {**pr_event, "routing": {"complexity": "high", "needsContext": False}}

    invoke_llm(low_event, storage=stub_storage, llm_router=stub_llm_router)
    invoke_llm(high_event, storage=stub_storage, llm_router=stub_llm_router)

    low_model = stub_llm_router.calls[0]["model"]
    high_model = stub_llm_router.calls[1]["model"]

    assert low_model == STUB_MODEL_BY_COMPLEXITY[Complexity.LOW]
    assert high_model == STUB_MODEL_BY_COMPLEXITY[Complexity.HIGH]
    assert low_model != high_model
