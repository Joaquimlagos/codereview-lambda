"""The JSON log lines the CloudWatch dashboard reads (`llm_call`, `route_decision`):

- their fields, including every `llm_call` outcome and the normalised token counts;
- that they never carry the prompt, the diff, a provider's error body, or a key;
- that the plain-text lines the 002 measurement script parses are unchanged.
"""

import json

import pytest
import requests

from contracts.models import Complexity
from integrations.decision_engine import DecisionEngine, DecisionEngineError, JevDecisionEngine
from integrations.llm_router import (
    CerebrasClient,
    GeminiClient,
    GroqClient,
    LlmRouterError,
    LlmTransientError,
    MultiProviderLlmRouter,
)
from route_model.handler import route_model

REVIEW_JSON = json.dumps({"summary": "Adds logging.", "comments": []})
SECRET_KEY = "sk-SENTINEL-KEY-0123456789"
DIFF_MARKER = "SENTINEL_DIFF_LINE_do_not_log"


class FakeResponse:
    def __init__(self, status_code: int, body: dict | None = None):
        self.status_code = status_code
        self._body = body or {}
        self.text = json.dumps(self._body)

    def json(self):
        return self._body


class FakeSession:
    """Answers each POST with the next queued response (or raises a queued exception)."""

    def __init__(self, *outcomes):
        self._outcomes = list(outcomes)
        self.requests: list[dict] = []

    def post(self, url, json, headers, timeout):
        self.requests.append({"url": url, "payload": json, "headers": headers})
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _openai_ok(content=REVIEW_JSON, finish_reason="stop") -> FakeResponse:
    return FakeResponse(
        200,
        {
            "choices": [{"message": {"content": content}, "finish_reason": finish_reason}],
            "usage": {
                "prompt_tokens": 3061,
                "completion_tokens": 3071,
                "completion_tokens_details": {"reasoning_tokens": 2666},
            },
        },
    )


def _gemini_ok() -> FakeResponse:
    return FakeResponse(
        200,
        {
            "candidates": [{"content": {"parts": [{"text": REVIEW_JSON}]}, "finishReason": "STOP"}],
            "usageMetadata": {
                "promptTokenCount": 2800,
                "candidatesTokenCount": 150,
                "thoughtsTokenCount": 1166,
            },
        },
    )


def _router(monkeypatch, tier_list: str, **sessions) -> MultiProviderLlmRouter:
    monkeypatch.setenv("LLM_MODELS_MEDIUM", tier_list)
    builders = {
        "groq": lambda s: GroqClient("https://groq.test/v1", SECRET_KEY, session=s),
        "cerebras": lambda s: CerebrasClient("https://cerebras.test/v1", SECRET_KEY, session=s),
        "gemini": lambda s: GeminiClient("https://gemini.test/v1beta", SECRET_KEY, session=s),
    }
    factories = {
        provider: (lambda b=builders[provider], s=session: b(s))
        for provider, session in sessions.items()
    }
    return MultiProviderLlmRouter(client_factories=factories)


def _review(router: MultiProviderLlmRouter):
    return router.generate_review(
        pr_id="42", diff_text=f"+{DIFF_MARKER}\n", complexity=Complexity.MEDIUM
    )


def _events(caplog, name: str) -> list[dict]:
    prefix = f'{{"event": "{name}"'
    return [json.loads(r.getMessage()) for r in caplog.records if r.getMessage().startswith(prefix)]


# --- llm_call ----------------------------------------------------------------------------


def test_llm_call_on_first_model_success_has_every_field(monkeypatch, caplog):
    router = _router(
        monkeypatch, "groq:openai/gpt-oss-120b:medium", groq=FakeSession(_openai_ok())
    )

    with caplog.at_level("INFO"):
        _review(router)

    [call] = _events(caplog, "llm_call")
    assert call == {
        "event": "llm_call",
        "pr": 42,
        "tier": "medium",
        "model": "groq:openai/gpt-oss-120b:medium",
        "attempt": 0,
        "fell_back": False,
        "outcome": "ok",
        "http_status": None,
        "llm_ms": call["llm_ms"],
        "finish_reason": "stop",
        "prompt_tokens": 3061,
        "output_tokens": 3071,
        "reasoning_tokens": 2666,
    }
    assert isinstance(call["llm_ms"], int) and call["llm_ms"] >= 0


def test_llm_call_logs_every_attempt_with_outcome_and_status(monkeypatch, caplog):
    router = _router(
        monkeypatch,
        "groq:openai/gpt-oss-120b:medium,cerebras:gpt-oss-120b:medium,gemini:gemini-3.5-flash:low",
        groq=FakeSession(FakeResponse(413, {"error": {"message": "too large"}})),
        cerebras=FakeSession(_openai_ok(content="", finish_reason="length")),
        gemini=FakeSession(_gemini_ok()),
    )

    with caplog.at_level("INFO"):
        review = _review(router)

    calls = _events(caplog, "llm_call")
    assert [(c["model"], c["attempt"], c["fell_back"], c["outcome"], c["http_status"])
            for c in calls] == [
        ("groq:openai/gpt-oss-120b:medium", 0, False, "transient", 413),
        ("cerebras:gpt-oss-120b:medium", 1, True, "truncated", None),
        ("gemini:gemini-3.5-flash:low", 2, True, "ok", None),
    ]
    # Gemini reports the answer and its thoughts separately; output_tokens counts both.
    assert (calls[2]["prompt_tokens"], calls[2]["output_tokens"], calls[2]["reasoning_tokens"]) \
        == (2800, 150 + 1166, 1166)
    assert calls[0]["prompt_tokens"] is None and calls[0]["finish_reason"] is None
    assert review.model_used == "gemini:gemini-3.5-flash:low" and review.fell_back


def test_empty_answer_is_logged_as_truncated_but_still_stops_the_list(monkeypatch, caplog):
    router = _router(
        monkeypatch,
        "groq:openai/gpt-oss-120b:medium,gemini:gemini-3.5-flash:low",
        groq=FakeSession(_openai_ok(content="", finish_reason="stop")),
        gemini=FakeSession(_gemini_ok()),
    )

    with caplog.at_level("INFO"), pytest.raises(LlmRouterError) as exc_info:
        _review(router)

    assert type(exc_info.value) is LlmRouterError
    assert [c["outcome"] for c in _events(caplog, "llm_call")] == ["truncated"]


@pytest.mark.parametrize("status", [400, 401, 403])
def test_permanent_error_is_logged_with_its_status_and_reraised(monkeypatch, caplog, status):
    router = _router(
        monkeypatch,
        "groq:openai/gpt-oss-120b:medium,gemini:gemini-3.5-flash:low",
        groq=FakeSession(FakeResponse(status, {"error": {"message": "bad"}})),
        gemini=FakeSession(_gemini_ok()),
    )

    with caplog.at_level("INFO"), pytest.raises(LlmRouterError) as exc_info:
        _review(router)

    assert type(exc_info.value) is LlmRouterError
    [call] = _events(caplog, "llm_call")
    assert (call["outcome"], call["http_status"]) == ("permanent", status)


def test_model_gone_and_timeout_outcomes(monkeypatch, caplog):
    router = _router(
        monkeypatch,
        "groq:openai/gpt-oss-120b:medium,gemini:gemini-3.5-flash:low",
        groq=FakeSession(FakeResponse(404, {"error": {"code": "model_not_found"}})),
        gemini=FakeSession(requests.Timeout("read timed out")),
    )

    with caplog.at_level("INFO"), pytest.raises(LlmTransientError):
        _review(router)

    assert [(c["outcome"], c["http_status"]) for c in _events(caplog, "llm_call")] == [
        ("model_not_found", 404),
        ("transient", None),
    ]


# --- route_decision ----------------------------------------------------------------------


class _FixedEngine(DecisionEngine):
    def __init__(self, outcome):
        self._outcome = outcome

    def classify(self, files_changed, lines_added, lines_removed, paths):
        if isinstance(self._outcome, Exception):
            raise self._outcome
        return self._outcome


def test_route_decision_from_jev(pr_event, stub_decision_engine, caplog):
    event = {**pr_event, "linesAdded": 150, "linesRemoved": 50}

    with caplog.at_level("INFO"):
        route_model(event, decision_engine=stub_decision_engine)

    [line] = _events(caplog, "route_decision")
    assert line == {
        "event": "route_decision",
        "pr": pr_event["prNumber"],
        "tier": "medium",
        "needs_context": line["needs_context"],
        "source": "jev",
        "jev_ms": line["jev_ms"],
        "error": None,
    }
    assert isinstance(line["needs_context"], bool) and isinstance(line["jev_ms"], int)


def test_route_decision_on_fallback_names_the_error_class_only(pr_event, caplog):
    session = FakeSession(requests.Timeout(f"timed out; key {SECRET_KEY}"))
    engine = JevDecisionEngine(endpoint="https://jev.test/v1/systemone", api_key=SECRET_KEY,
                               session=session)

    with caplog.at_level("INFO"):
        output = route_model(pr_event, decision_engine=engine)

    [line] = _events(caplog, "route_decision")
    assert output == {"complexity": "medium", "needsContext": False}
    assert (line["tier"], line["needs_context"], line["source"], line["error"]) == (
        "medium", False, "fallback", "Timeout",
    )


# --- nothing sensitive in the new lines ----------------------------------------------------


def test_new_lines_never_contain_prompt_diff_error_body_or_key(monkeypatch, pr_event, caplog):
    echoed = FakeResponse(400, {"error": {"message": f"bad request near {DIFF_MARKER}"}})
    router = _router(
        monkeypatch,
        "groq:openai/gpt-oss-120b:medium,cerebras:gpt-oss-120b:medium",
        groq=FakeSession(FakeResponse(503, {"error": {"message": f"echo {DIFF_MARKER}"}})),
        cerebras=FakeSession(echoed),
    )
    sensitive_path = f"src/{DIFF_MARKER}/Auth.java"
    engine = _FixedEngine(DecisionEngineError(f"failed for {sensitive_path} with {SECRET_KEY}"))

    with caplog.at_level("INFO"):
        with pytest.raises(LlmRouterError):
            _review(router)
        route_model({**pr_event, "paths": [sensitive_path]}, decision_engine=engine)

    lines = [
        r.getMessage() for r in caplog.records
        if r.getMessage().startswith(('{"event": "llm_call"', '{"event": "route_decision"'))
    ]
    assert len(lines) == 3
    for line in lines:
        for forbidden in (DIFF_MARKER, SECRET_KEY, "Bearer", "x-goog-api-key", "You are reviewing"):
            assert forbidden not in line


# --- the plain-text lines stay byte-identical ----------------------------------------------


def test_answered_text_lines_are_unchanged(monkeypatch, caplog):
    router = _router(
        monkeypatch,
        "groq:openai/gpt-oss-120b:medium,gemini:gemini-3.5-flash:low",
        groq=FakeSession(FakeResponse(503, {"error": {"message": "busy"}})),
        gemini=FakeSession(_gemini_ok()),
    )

    with caplog.at_level("INFO"):
        _review(router)

    text_lines = [r.getMessage() for r in caplog.records if not r.getMessage().startswith("{")]
    assert text_lines == [
        "Model groq:openai/gpt-oss-120b:medium failed for PR 42 (groq:openai/gpt-oss-120b "
        'returned HTTP 503: {"error": {"message": "busy"}}); trying next model',
        "gemini:gemini-3.5-flash answered: finishReason=STOP usage={'promptTokenCount': 2800, "
        "'candidatesTokenCount': 150, 'thoughtsTokenCount': 1166}",
    ]


def test_openai_compatible_answered_line_is_unchanged(monkeypatch, caplog):
    router = _router(
        monkeypatch, "cerebras:gpt-oss-120b:medium", cerebras=FakeSession(_openai_ok())
    )

    with caplog.at_level("INFO"):
        _review(router)

    assert [r.getMessage() for r in caplog.records if " answered: " in r.getMessage()] == [
        "cerebras:gpt-oss-120b answered: finish_reason=stop usage={'prompt_tokens': 3061, "
        "'completion_tokens': 3071, 'completion_tokens_details': {'reasoning_tokens': 2666}}",
    ]
