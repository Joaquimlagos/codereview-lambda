"""Gemini review generation: model tier resolved from GEMINI_MODEL_LOW/MEDIUM/HIGH
(Principle III) — no model name is ever hardcoded in the real client. The real client calls
Gemini's `generateContent` endpoint directly, with the URL built from GEMINI_API_BASE; there
is no separate routing service in front of it — tier -> model selection happens in this
Lambda (see constitution.md Principle III).
"""

from abc import ABC, abstractmethod

from contracts.models import Complexity, GeneratedReview
from integrations.config import require_env

# Complexity tier -> the .env variable suffix holding that tier's real Gemini model name.
# The real client resolves the model name only from these env vars (Principle III); nothing
# here is a model name itself.
_TIER_ENV_SUFFIX: dict[Complexity, str] = {
    Complexity.LOW: "LOW",
    Complexity.MEDIUM: "MEDIUM",
    Complexity.HIGH: "HIGH",
}


def model_for_complexity(complexity: Complexity) -> str:
    """Resolve the real Gemini model name for a tier from its GEMINI_MODEL_{tier} env var."""
    suffix = _TIER_ENV_SUFFIX[complexity]
    return require_env(f"GEMINI_MODEL_{suffix}")


# Fixed model names for the stub only, so tests stay deterministic and network/env-var-free
# (Principle IV, FR-011) — never read by the real client.
STUB_MODEL_BY_COMPLEXITY: dict[Complexity, str] = {
    Complexity.LOW: "gemini-2.5-flash-lite",
    Complexity.MEDIUM: "gemini-2.5-flash",
    Complexity.HIGH: "gemini-2.5-pro",
}


class LlmRouterError(Exception):
    """Raised when Gemini is unavailable or returns an invalid response."""


class LlmRouter(ABC):
    @abstractmethod
    def generate_review(
        self,
        pr_id: str,
        diff_text: str,
        complexity: Complexity,
        context_text: str | None = None,
    ) -> GeneratedReview:
        """Generate a review for `diff_text` (plus optional `context_text`) at `complexity` tier."""


class GeminiLlmRouter(LlmRouter):
    def __init__(self, api_base: str, api_key: str, session=None):
        import requests

        self._api_base = api_base
        self._api_key = api_key
        self._session = session or requests.Session()

    def generate_review(
        self,
        pr_id: str,
        diff_text: str,
        complexity: Complexity,
        context_text: str | None = None,
    ) -> GeneratedReview:
        model = model_for_complexity(complexity)
        url = f"{self._api_base}/models/{model}:generateContent"
        prompt = f"Diff:\n{diff_text}"
        if context_text:
            prompt += f"\n\nContext:\n{context_text}"
        payload = {"contents": [{"parts": [{"text": prompt}]}]}
        # Gemini's real API authenticates with this header, not an OAuth-style Bearer token.
        headers = {"x-goog-api-key": self._api_key}

        try:
            response = self._session.post(url, json=payload, headers=headers, timeout=30)
            response.raise_for_status()
            data = response.json()
        except Exception as exc:
            raise LlmRouterError(str(exc)) from exc

        # Gemini returns an empty/missing `candidates` list (HTTP 200, no exception) when the
        # response is blocked by a safety filter. Decision: treat that the same as any other
        # generation failure — raise LlmRouterError so PostComment never receives a review
        # (FR-008), letting the run fail visibly instead of silently posting nothing (SC-005).
        candidates = data.get("candidates") or []
        if not candidates:
            raise LlmRouterError("Gemini returned no candidates (response likely safety-filtered)")

        try:
            review_text = candidates[0]["content"]["parts"][0]["text"]
            return GeneratedReview(pr_id=pr_id, review_text=review_text, model_used=model)
        except Exception as exc:
            raise LlmRouterError(str(exc)) from exc


class StubLlmRouter(LlmRouter):
    """Network-free stand-in for tests (Principle IV, FR-011)."""

    def __init__(self):
        self.calls: list[dict] = []
        self.fail: bool = False
        self.empty_output: bool = False

    def generate_review(
        self,
        pr_id: str,
        diff_text: str,
        complexity: Complexity,
        context_text: str | None = None,
    ) -> GeneratedReview:
        model = STUB_MODEL_BY_COMPLEXITY[complexity]
        self.calls.append(
            {
                "pr_id": pr_id,
                "diff_text": diff_text,
                "complexity": complexity,
                "context_text": context_text,
                "model": model,
            }
        )
        if self.fail:
            raise LlmRouterError("stub configured to simulate Gemini failure")
        review_text = "" if self.empty_output else f"Automated review ({model}): {diff_text[:80]}"
        try:
            return GeneratedReview(pr_id=pr_id, review_text=review_text, model_used=model)
        except Exception as exc:
            # Mirrors the real client: an empty/malformed review is a generation failure (FR-008),
            # not a hollow GeneratedReview.
            raise LlmRouterError(str(exc)) from exc
