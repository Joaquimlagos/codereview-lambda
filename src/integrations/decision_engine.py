"""Jev (TypeSafe AI) decision-engine abstraction: complexity + RAG-need routing (Principle III).

Structured, non-generative decisions only — no Gemini/LLM call belongs here.
"""

from abc import ABC, abstractmethod

from contracts.models import Complexity, RoutingDecision

# Local stub thresholds only (spec.md Assumptions) — the real Jev engine's algorithm is external
# and may differ without changing the pipeline's behavior.
LOW_MAX_LINES = 50
MEDIUM_MAX_LINES = 400


class DecisionEngineError(Exception):
    """Raised when Jev is unavailable or returns an invalid response."""


class DecisionEngine(ABC):
    @abstractmethod
    def classify(self, diff_text: str) -> RoutingDecision:
        """Classify a diff's complexity/RAG need. Raises DecisionEngineError on failure."""


class JevDecisionEngine(DecisionEngine):
    def __init__(self, endpoint: str, api_key: str, session=None):
        import requests

        self._endpoint = endpoint
        self._api_key = api_key
        self._session = session or requests.Session()

    def classify(self, diff_text: str) -> RoutingDecision:
        try:
            response = self._session.post(
                self._endpoint,
                json={"diff": diff_text},
                headers={"Authorization": f"Bearer {self._api_key}"},
                timeout=10,
            )
            response.raise_for_status()
            data = response.json()
            return RoutingDecision(complexity=data["complexity"], needsContext=data["needsContext"])
        except Exception as exc:
            raise DecisionEngineError(str(exc)) from exc


def classify_by_line_count(lines_changed: int) -> Complexity:
    if lines_changed <= LOW_MAX_LINES:
        return Complexity.LOW
    if lines_changed <= MEDIUM_MAX_LINES:
        return Complexity.MEDIUM
    return Complexity.HIGH


class StubDecisionEngine(DecisionEngine):
    """Network-free stand-in for tests (Principle IV, FR-011).

    Classifies by total changed lines using the spec.md-defined thresholds, unless a fixed
    response or a failure has been configured — needed for RouteModel's fallback-path tests.
    """

    def __init__(self):
        self.fail: bool = False
        self.forced_result: RoutingDecision | None = None

    def classify(self, diff_text: str) -> RoutingDecision:
        if self.fail:
            raise DecisionEngineError("stub configured to simulate Jev failure")
        if self.forced_result is not None:
            return self.forced_result
        lines_changed = len(diff_text.splitlines())
        complexity = classify_by_line_count(lines_changed)
        return RoutingDecision(complexity=complexity, needsContext=False)
