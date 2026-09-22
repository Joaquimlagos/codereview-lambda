"""Contract test for RouteModel: output MUST validate against RoutingDecision (FR-012)."""

from contracts.models import RoutingDecision
from route_model.handler import route_model


def test_route_model_output_matches_routing_decision_contract(
    pr_event, stub_storage, stub_decision_engine
):
    stub_decision_engine.forced_result = RoutingDecision(complexity="low", needsContext=False)

    output = route_model(pr_event, storage=stub_storage, decision_engine=stub_decision_engine)

    validated = RoutingDecision.model_validate(output)
    assert validated.complexity == "low"
    assert validated.needsContext is False


def test_route_model_output_uses_the_exact_needscontext_key(
    pr_event, stub_storage, stub_decision_engine
):
    """Guards against a silent rename: codereview-infra's ASL Choice state branches on
    `$.routing.needsContext` literally (statemachine/definition.asl.json.tpl). This checks
    the raw dict, not just pydantic validation, so a renamed field (e.g. back to `needsRag`,
    or to anything else) fails here even if RoutingDecision's own definition changed too.
    """
    stub_decision_engine.forced_result = RoutingDecision(complexity="high", needsContext=True)

    output = route_model(pr_event, storage=stub_storage, decision_engine=stub_decision_engine)

    assert set(output.keys()) == {"complexity", "needsContext"}
    assert "needsRag" not in output
