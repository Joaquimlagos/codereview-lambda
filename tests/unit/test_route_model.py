"""Unit tests: the local decision-engine stub classifies by changed-line thresholds
(spec.md Assumptions: low <= 50, medium 51-400, high > 400).
"""

from integrations.storage import StubStorage
from route_model.handler import route_model


def test_small_diff_classified_low(pr_event, stub_storage, stub_decision_engine):
    output = route_model(pr_event, storage=stub_storage, decision_engine=stub_decision_engine)
    assert output["complexity"] == "low"


def test_medium_diff_classified_medium(pr_event, stub_decision_engine):
    medium_diff = "diff --git a/f b/f\n" + "\n".join(f"+line {i}" for i in range(200))
    storage = StubStorage(initial={pr_event["diff_ref"]: medium_diff})

    output = route_model(pr_event, storage=storage, decision_engine=stub_decision_engine)
    assert output["complexity"] == "medium"


def test_large_diff_classified_high(pr_event, stub_decision_engine, large_diff):
    storage = StubStorage(initial={pr_event["diff_ref"]: large_diff})

    output = route_model(pr_event, storage=storage, decision_engine=stub_decision_engine)
    assert output["complexity"] == "high"
