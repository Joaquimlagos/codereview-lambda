"""Integration test: end-to-end review with no context needed (quickstart Scenario 1, SC-001)."""

from contracts.models import RoutingDecision
from invoke_llm.handler import invoke_llm
from post_comment.handler import post_comment
from route_model.handler import route_model


def test_end_to_end_review_without_context(
    pr_event, stub_storage, stub_decision_engine, stub_llm_router, stub_github_client
):
    stub_decision_engine.forced_result = RoutingDecision(complexity="low", needsContext=False)

    routing = route_model(pr_event, storage=stub_storage, decision_engine=stub_decision_engine)
    event_after_route = {**pr_event, "routing": routing}

    review = invoke_llm(event_after_route, storage=stub_storage, llm_router=stub_llm_router)
    assert review["review_text"]

    event_after_invoke = {**event_after_route, "analysis": review}
    comment = post_comment(event_after_invoke, github_client=stub_github_client)

    assert comment["posted"] is True
    assert len(stub_github_client.posted_comments) == 1
