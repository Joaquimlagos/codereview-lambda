"""Jev (TypeSafe AI) decision-engine abstraction: complexity + RAG-need routing (Principle III).

Structured, non-generative decisions only — no Gemini/LLM call belongs here. Request/response
shape below was validated live against the real TypeSafe API Playground.
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
    def classify(
        self, files_changed: int, lines_added: int, lines_removed: int, paths: list[str]
    ) -> RoutingDecision:
        """Classify a PR's complexity/RAG need from its lightweight diff stats — never the
        diff body itself. Raises DecisionEngineError on failure."""


class JevDecisionEngine(DecisionEngine):
    def __init__(self, endpoint: str, api_key: str, session=None):
        import requests

        self._endpoint = endpoint
        self._api_key = api_key
        self._session = session or requests.Session()

    def classify(
        self, files_changed: int, lines_added: int, lines_removed: int, paths: list[str]
    ) -> RoutingDecision:
        payload = {
            "model": "jev-latest",
            "state": {
                "files_changed": files_changed,
                "lines_added": lines_added,
                "lines_removed": lines_removed,
                "paths": paths,
            },
            "questions": {
                "tier": {
                    "type": "choice",
                    "instructions": (
                        "What is the complexity level of this code change for review purposes?"
                    ),
                    "criteria": {
                        "low": "Trivial change: docs, config, renames, no business logic",
                        "medium": "Common business logic, limited scope, few files",
                        "high": (
                            "Touches auth, security, concurrency, or many interconnected files"
                        ),
                    },
                },
                "needsContext": {
                    "type": "noul",
                    "instructions": (
                        "Does this change need additional context from the rest of the "
                        "project (via RAG) to be safely reviewed?"
                    ),
                },
            },
        }
        try:
            response = self._session.post(
                self._endpoint,
                json=payload,
                headers={"Authorization": f"Bearer {self._api_key}"},
                timeout=10,
            )
            response.raise_for_status()
            data = response.json()
            # A response with no "answers" (or a malformed one) MUST be treated as a Jev
            # failure, same as a network/HTTP error — the KeyError/TypeError below is caught
            # by the except clause and re-raised as DecisionEngineError, which is exactly
            # what triggers RouteModel's documented fallback.
            answers = data["answers"]
            complexity = answers["tier"]["choice"]
            needs_context = answers["needsContext"]["noul"] >= 0.5
            return RoutingDecision(complexity=complexity, needsContext=needs_context)
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

    Classifies by total changed lines (lines_added + lines_removed) using the
    spec.md-defined thresholds, unless a fixed response or a failure has been configured —
    needed for RouteModel's fallback-path tests.
    """

    def __init__(self):
        self.fail: bool = False
        self.forced_result: RoutingDecision | None = None

    def classify(
        self, files_changed: int, lines_added: int, lines_removed: int, paths: list[str]
    ) -> RoutingDecision:
        if self.fail:
            raise DecisionEngineError("stub configured to simulate Jev failure")
        if self.forced_result is not None:
            return self.forced_result
        lines_changed = lines_added + lines_removed
        complexity = classify_by_line_count(lines_changed)
        return RoutingDecision(complexity=complexity, needsContext=False)
