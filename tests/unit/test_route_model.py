"""Unit tests: the local decision-engine stub classifies by changed-line thresholds
(spec.md Assumptions: low <= 50, medium 51-400, high > 400), computed from
linesAdded + linesRemoved on the event — RouteModel never reads the diff itself.
"""

from route_model.handler import route_model


def test_small_change_classified_low(pr_event, stub_decision_engine):
    event = {**pr_event, "linesAdded": 8, "linesRemoved": 2}  # 10 total

    output = route_model(event, decision_engine=stub_decision_engine)

    assert output["complexity"] == "low"


def test_medium_change_classified_medium(pr_event, stub_decision_engine):
    event = {**pr_event, "linesAdded": 150, "linesRemoved": 50}  # 200 total

    output = route_model(event, decision_engine=stub_decision_engine)

    assert output["complexity"] == "medium"


def test_large_change_classified_high(pr_event, stub_decision_engine):
    event = {**pr_event, "linesAdded": 400, "linesRemoved": 50}  # 450 total

    output = route_model(event, decision_engine=stub_decision_engine)

    assert output["complexity"] == "high"
