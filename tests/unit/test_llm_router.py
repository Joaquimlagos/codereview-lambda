"""Unit tests for the multi-provider LLM router, with no network:

- model-list parsing (`provider:model[:reasoning]`);
- GeminiClient and GroqClient against a fake HTTP session: error classification (transient,
  model gone, permanent), reasoning configuration sent only when requested, answer
  extraction;
- MultiProviderLlmRouter against fake clients: fallback across providers, stop on permanent
  errors, the all-models-gone case, and the Lambda-deadline guard.
"""

import json

import pytest
import requests

from contracts.models import Complexity
from integrations.config import ConfigError
from integrations.llm_router import (
    ATTEMPT_TIME_BUDGET_MS,
    GEMINI_HIGH_REASONING_ATTEMPT_BUDGET_MS,
    GEMINI_HIGH_REASONING_READ_TIMEOUT_SECONDS,
    GeminiClient,
    GroqClient,
    LlmModelNotFoundError,
    LlmOutputTruncatedError,
    LlmRouterError,
    LlmTransientError,
    ModelClient,
    ModelSpec,
    MultiProviderLlmRouter,
    build_prompt,
    models_for_complexity,
    parse_model_spec,
    parse_review_response,
)

REVIEW_JSON = json.dumps({"summary": "Adds logging.", "comments": []})


# --- fakes -------------------------------------------------------------------------------


class FakeResponse:
    def __init__(self, status_code: int, body: dict | None = None):
        self.status_code = status_code
        self._body = body or {}
        self.text = json.dumps(self._body)

    def json(self):
        return self._body


class FakeSession:
    """Returns `response` (or raises `exc`) and records each request's URL and payload."""

    def __init__(self, response: FakeResponse | None = None, exc: Exception | None = None):
        self._response = response
        self._exc = exc
        self.requests: list[dict] = []

    def post(self, url, json, headers, timeout):
        self.requests.append({"url": url, "payload": json, "headers": headers, "timeout": timeout})
        if self._exc:
            raise self._exc
        return self._response


class FakeClient(ModelClient):
    """Per-model outcome: answer text, or an exception to raise. Records models called."""

    def __init__(self, outcomes: dict[str, str | Exception]):
        self._outcomes = outcomes
        self.calls: list[tuple[str, str | None]] = []

    def generate(self, model, prompt, reasoning=None):
        self.calls.append((model, reasoning))
        outcome = self._outcomes[model]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _gemini_ok(*parts: dict) -> FakeResponse:
    return FakeResponse(200, {"candidates": [{"content": {"parts": list(parts)}}]})


def _groq_ok(content: str) -> FakeResponse:
    return FakeResponse(
        200,
        {"choices": [{"message": {"content": content, "reasoning": "thinking..."}}]},
    )


def _gemini(session: FakeSession) -> GeminiClient:
    return GeminiClient(api_base="https://gemini.test/v1beta", api_key="k", session=session)


def _groq(session: FakeSession) -> GroqClient:
    return GroqClient(api_base="https://groq.test/openai/v1", api_key="k", session=session)


def _router(clients: dict[str, ModelClient], remaining_time_ms=None) -> MultiProviderLlmRouter:
    factories = {provider: (lambda c=client: c) for provider, client in clients.items()}
    return MultiProviderLlmRouter(client_factories=factories, remaining_time_ms=remaining_time_ms)


def _review(router: MultiProviderLlmRouter, complexity=Complexity.MEDIUM):
    return router.generate_review(pr_id="42", diff_text="+x", complexity=complexity)


# --- model list parsing ------------------------------------------------------------------


def test_parse_model_spec_with_and_without_reasoning():
    assert parse_model_spec("gemini:gemini-3.5-flash:low") == ModelSpec(
        "gemini", "gemini-3.5-flash", "low"
    )
    assert parse_model_spec("groq:openai/gpt-oss-120b") == ModelSpec(
        "groq", "openai/gpt-oss-120b", None
    )
    assert parse_model_spec("groq:openai/gpt-oss-120b:medium").label == (
        "groq:openai/gpt-oss-120b:medium"
    )


@pytest.mark.parametrize(
    "entry", ["gemini-3.5-flash", "openai:gpt-4o", "gemini::low", "groq:a:b:c", "groq:"]
)
def test_parse_model_spec_rejects_malformed_entries(entry):
    with pytest.raises(ConfigError):
        parse_model_spec(entry)


def test_models_for_complexity_reads_ordered_mixed_provider_list(monkeypatch):
    monkeypatch.setenv(
        "LLM_MODELS_LOW", " groq:openai/gpt-oss-120b:low , gemini:gemini-3.5-flash:low ,"
    )

    assert models_for_complexity(Complexity.LOW) == [
        ModelSpec("groq", "openai/gpt-oss-120b", "low"),
        ModelSpec("gemini", "gemini-3.5-flash", "low"),
    ]


def test_models_for_complexity_rejects_a_list_with_no_models(monkeypatch):
    monkeypatch.setenv("LLM_MODELS_LOW", " , ")

    with pytest.raises(ConfigError, match="LLM_MODELS_LOW"):
        models_for_complexity(Complexity.LOW)


# --- Gemini client -----------------------------------------------------------------------


def test_gemini_sends_thinking_level_only_when_requested():
    with_reasoning = FakeSession(_gemini_ok({"text": REVIEW_JSON}))
    without = FakeSession(_gemini_ok({"text": REVIEW_JSON}))

    _gemini(with_reasoning).generate("gemini-3.5-flash", "p", reasoning="low")
    _gemini(without).generate("gemma-4-26b-a4b-it", "p")

    assert with_reasoning.requests[0]["payload"]["generationConfig"] == {
        "thinkingConfig": {"thinkingLevel": "low"}
    }
    # Gemma rejects any thinkingConfig with 400, so nothing may be sent without a level.
    assert "generationConfig" not in without.requests[0]["payload"]


def test_gemini_uses_45s_read_timeout():
    session = FakeSession(_gemini_ok({"text": REVIEW_JSON}))

    _gemini(session).generate("gemini-3.5-flash", "p")

    assert session.requests[0]["timeout"] == (5, 45)


def test_gemini_skips_reasoning_parts_and_joins_answer_text():
    session = FakeSession(
        _gemini_ok(
            {"text": "Let me think...", "thought": True},
            {"text": REVIEW_JSON[:20], "thoughtSignature": "opaque"},
            {"text": REVIEW_JSON[20:]},
        )
    )

    assert _gemini(session).generate("gemini-3.5-flash", "p") == REVIEW_JSON


@pytest.mark.parametrize("status", [413, 429, 500, 502, 503, 504])
def test_gemini_transient_statuses(status):
    session = FakeSession(FakeResponse(status, {"error": {"message": "overloaded"}}))

    with pytest.raises(LlmTransientError, match=str(status)):
        _gemini(session).generate("gemini-3.5-flash", "p")


def test_gemini_timeout_is_transient():
    with pytest.raises(LlmTransientError):
        _gemini(FakeSession(exc=requests.Timeout("read timed out"))).generate("m", "p")


def test_gemini_404_means_model_gone():
    session = FakeSession(FakeResponse(404, {"error": {"message": "models/x is not found"}}))

    with pytest.raises(LlmModelNotFoundError):
        _gemini(session).generate("gemini-2.5-flash-lite", "p")


@pytest.mark.parametrize("status", [400, 401, 403])
def test_gemini_permanent_statuses_are_not_retryable(status):
    session = FakeSession(FakeResponse(status, {"error": {"message": "bad request"}}))

    with pytest.raises(LlmRouterError) as exc_info:
        _gemini(session).generate("gemini-3.5-flash", "p")

    assert not isinstance(exc_info.value, (LlmTransientError, LlmModelNotFoundError))


def test_gemini_reasoning_only_response_is_an_empty_response_error():
    session = FakeSession(_gemini_ok({"text": "thinking only", "thought": True}))

    with pytest.raises(LlmRouterError, match="empty response"):
        _gemini(session).generate("gemini-3.5-flash", "p")


# --- Groq client -------------------------------------------------------------------------


def test_groq_calls_chat_completions_with_reasoning_effort_only_when_requested():
    with_reasoning = FakeSession(_groq_ok(REVIEW_JSON))
    without = FakeSession(_groq_ok(REVIEW_JSON))

    _groq(with_reasoning).generate("openai/gpt-oss-120b", "the prompt", reasoning="low")
    _groq(without).generate("openai/gpt-oss-120b", "the prompt")

    request = with_reasoning.requests[0]
    assert request["url"] == "https://groq.test/openai/v1/chat/completions"
    assert request["headers"]["Authorization"] == "Bearer k"
    assert request["payload"] == {
        "model": "openai/gpt-oss-120b",
        "messages": [{"role": "user", "content": "the prompt"}],
        "reasoning_effort": "low",
    }
    assert "reasoning_effort" not in without.requests[0]["payload"]


def test_groq_returns_content_not_the_separate_reasoning_field():
    assert _groq(FakeSession(_groq_ok(REVIEW_JSON))).generate("m", "p") == REVIEW_JSON


@pytest.mark.parametrize("status", [413, 429, 503])
def test_groq_transient_statuses(status):
    session = FakeSession(FakeResponse(status, {"error": {"message": "rate limited"}}))

    with pytest.raises(LlmTransientError):
        _groq(session).generate("openai/gpt-oss-120b", "p")


@pytest.mark.parametrize(
    ("status", "code"),
    [(404, "model_not_found"), (400, "model_decommissioned"), (400, "model_not_found")],
)
def test_groq_model_gone_responses(status, code):
    session = FakeSession(FakeResponse(status, {"error": {"message": "gone", "code": code}}))

    with pytest.raises(LlmModelNotFoundError):
        _groq(session).generate("some/retired-model", "p")


@pytest.mark.parametrize("status", [400, 401, 403])
def test_groq_other_client_errors_are_permanent(status):
    session = FakeSession(
        FakeResponse(status, {"error": {"message": "bad", "code": "invalid_request"}})
    )

    with pytest.raises(LlmRouterError) as exc_info:
        _groq(session).generate("openai/gpt-oss-120b", "p")

    assert not isinstance(exc_info.value, (LlmTransientError, LlmModelNotFoundError))


def test_groq_empty_content_is_an_empty_response_error():
    with pytest.raises(LlmRouterError, match="empty response"):
        _groq(FakeSession(_groq_ok(""))).generate("openai/gpt-oss-120b", "p")


# --- router ------------------------------------------------------------------------------


@pytest.fixture
def two_provider_tier(monkeypatch):
    monkeypatch.setenv(
        "LLM_MODELS_MEDIUM", "gemini:gemini-3.5-flash:low,groq:openai/gpt-oss-120b:low"
    )


def test_first_model_success_uses_it_and_never_builds_the_other_client(two_provider_tier):
    gemini = FakeClient({"gemini-3.5-flash": REVIEW_JSON})
    built = []
    router = MultiProviderLlmRouter(
        client_factories={"gemini": lambda: gemini, "groq": lambda: built.append("groq")},
    )

    review = _review(router)

    assert gemini.calls == [("gemini-3.5-flash", "low")]
    assert built == []  # the Groq key is never fetched
    assert review.model_used == "gemini:gemini-3.5-flash:low"
    assert review.fell_back is False


def test_transient_failure_falls_back_across_providers(two_provider_tier):
    gemini = FakeClient({"gemini-3.5-flash": LlmTransientError("503 high demand")})
    groq = FakeClient({"openai/gpt-oss-120b": REVIEW_JSON})

    review = _review(_router({"gemini": gemini, "groq": groq}))

    assert groq.calls == [("openai/gpt-oss-120b", "low")]
    assert review.model_used == "groq:openai/gpt-oss-120b:low"
    assert review.fell_back is True
    assert review.summary == "Adds logging."


def test_model_gone_falls_back_to_the_next_model(two_provider_tier):
    gemini = FakeClient({"gemini-3.5-flash": LlmModelNotFoundError("404 not found")})
    groq = FakeClient({"openai/gpt-oss-120b": REVIEW_JSON})

    review = _review(_router({"gemini": gemini, "groq": groq}))

    assert review.model_used == "groq:openai/gpt-oss-120b:low"
    assert review.fell_back is True


def test_permanent_error_stops_without_trying_the_next_model(two_provider_tier):
    gemini = FakeClient({"gemini-3.5-flash": LlmRouterError("HTTP 401 bad key")})
    groq = FakeClient({"openai/gpt-oss-120b": REVIEW_JSON})

    with pytest.raises(LlmRouterError) as exc_info:
        _review(_router({"gemini": gemini, "groq": groq}))

    assert type(exc_info.value) is LlmRouterError
    assert groq.calls == []


def test_all_models_transient_raises_transient_naming_each(two_provider_tier):
    gemini = FakeClient({"gemini-3.5-flash": LlmTransientError("HTTP 503")})
    groq = FakeClient({"openai/gpt-oss-120b": LlmTransientError("HTTP 429")})

    with pytest.raises(LlmTransientError) as exc_info:
        _review(_router({"gemini": gemini, "groq": groq}))

    message = str(exc_info.value)
    assert "gemini:gemini-3.5-flash:low: HTTP 503" in message
    assert "groq:openai/gpt-oss-120b:low: HTTP 429" in message


def test_all_models_gone_is_a_non_retryable_model_not_found_error(two_provider_tier):
    gemini = FakeClient({"gemini-3.5-flash": LlmModelNotFoundError("404")})
    groq = FakeClient({"openai/gpt-oss-120b": LlmModelNotFoundError("decommissioned")})

    with pytest.raises(LlmModelNotFoundError) as exc_info:
        _review(_router({"gemini": gemini, "groq": groq}))

    assert not isinstance(exc_info.value, LlmTransientError)


def test_mix_of_gone_and_transient_is_retryable(two_provider_tier):
    gemini = FakeClient({"gemini-3.5-flash": LlmModelNotFoundError("404")})
    groq = FakeClient({"openai/gpt-oss-120b": LlmTransientError("HTTP 503")})

    with pytest.raises(LlmTransientError):
        _review(_router({"gemini": gemini, "groq": groq}))


def test_deadline_stops_before_an_attempt_that_would_not_fit(two_provider_tier):
    """After the first model fails, too little Lambda time is left for the second: the router
    must raise LlmTransientError (retried by Step Functions) instead of starting a request
    Lambda would kill with Sandbox.Timedout."""
    remaining = iter([150_000, ATTEMPT_TIME_BUDGET_MS - 1])
    gemini = FakeClient({"gemini-3.5-flash": LlmTransientError("read timed out")})
    groq = FakeClient({"openai/gpt-oss-120b": REVIEW_JSON})

    router = _router({"gemini": gemini, "groq": groq}, remaining_time_ms=lambda: next(remaining))

    with pytest.raises(LlmTransientError) as exc_info:
        _review(router)

    assert groq.calls == []
    message = str(exc_info.value)
    assert "gemini:gemini-3.5-flash:low: read timed out" in message
    assert "Not attempted: groq:openai/gpt-oss-120b:low" in message


def test_enough_time_left_lets_the_next_attempt_run(two_provider_tier):
    gemini = FakeClient({"gemini-3.5-flash": LlmTransientError("HTTP 503")})
    groq = FakeClient({"openai/gpt-oss-120b": REVIEW_JSON})

    review = _review(
        _router({"gemini": gemini, "groq": groq}, remaining_time_ms=lambda: ATTEMPT_TIME_BUDGET_MS)
    )

    assert review.model_used == "groq:openai/gpt-oss-120b:low"


def test_gemma_style_fenced_response_parses(monkeypatch):
    monkeypatch.setenv("LLM_MODELS_LOW", "gemini:gemma-4-26b-a4b-it")
    fenced = "```json\n" + json.dumps({"summary": "Gemma review.", "comments": []}) + "\n```"
    gemini = FakeClient({"gemma-4-26b-a4b-it": fenced})

    review = _review(_router({"gemini": gemini}), complexity=Complexity.LOW)

    assert gemini.calls == [("gemma-4-26b-a4b-it", None)]
    assert review.parse_fallback is False
    assert review.summary == "Gemma review."


def test_comment_missing_severity_is_discarded_not_the_whole_response(caplog):
    """A single malformed comment must not nuke a good summary and the other, valid comment:
    only that one comment is dropped, parse_fallback stays False, and the drop is logged."""
    raw = json.dumps(
        {
            "summary": "Two comments, one malformed.",
            "comments": [
                {
                    "path": "src/a.py",
                    "line": 10,
                    "body": "Missing null check.",
                    "category": "bug",
                    "severity": "high",
                },
                {
                    "path": "src/b.py",
                    "line": 20,
                    "body": "No severity here.",
                    "category": "bug",
                    # 'severity' missing.
                },
            ],
        }
    )

    with caplog.at_level("WARNING"):
        review = parse_review_response(raw, pr_id="42", model_used="groq:openai/gpt-oss-120b:low")

    assert review.parse_fallback is False
    assert review.summary == "Two comments, one malformed."
    assert len(review.comments) == 1
    assert review.comments[0].path == "src/a.py"
    assert "discarded 1 of 2 comment(s)" in caplog.text


# --- output-limit truncation and Groq output budget --------------------------------------


def test_groq_length_with_empty_content_is_truncation_not_permanent():
    body = {"choices": [{"message": {"content": ""}, "finish_reason": "length"}], "usage": {}}

    with pytest.raises(LlmOutputTruncatedError) as exc_info:
        _groq(FakeSession(FakeResponse(200, body))).generate("openai/gpt-oss-120b", "p", "high")

    assert isinstance(exc_info.value, LlmTransientError)


def test_groq_empty_content_that_was_not_cut_off_stays_permanent():
    body = {"choices": [{"message": {"content": ""}, "finish_reason": "stop"}]}

    with pytest.raises(LlmRouterError) as exc_info:
        _groq(FakeSession(FakeResponse(200, body))).generate("openai/gpt-oss-120b", "p")

    assert not isinstance(exc_info.value, LlmTransientError)


def test_gemini_max_tokens_with_empty_text_is_truncation_not_permanent():
    body = {
        "candidates": [
            {"content": {"parts": [{"text": "reasoning", "thought": True}]},
             "finishReason": "MAX_TOKENS"}
        ]
    }

    with pytest.raises(LlmOutputTruncatedError):
        _gemini(FakeSession(FakeResponse(200, body))).generate("gemini-3.5-flash", "p", "low")


def test_gemini_empty_text_that_was_not_cut_off_stays_permanent():
    body = {"candidates": [{"content": {"parts": []}, "finishReason": "STOP"}]}

    with pytest.raises(LlmRouterError) as exc_info:
        _gemini(FakeSession(FakeResponse(200, body))).generate("gemini-3.5-flash", "p")

    assert not isinstance(exc_info.value, LlmTransientError)


def test_groq_sends_explicit_output_budget_only_for_medium_effort():
    medium, low, none = (FakeSession(_groq_ok(REVIEW_JSON)) for _ in range(3))

    _groq(medium).generate("openai/gpt-oss-120b", "p", reasoning="medium")
    _groq(low).generate("openai/gpt-oss-120b", "p", reasoning="low")
    _groq(none).generate("openai/gpt-oss-120b", "p")

    assert medium.requests[0]["payload"]["max_completion_tokens"] == 5500
    assert "max_completion_tokens" not in low.requests[0]["payload"]
    assert "max_completion_tokens" not in none.requests[0]["payload"]


def test_truncated_output_falls_back_to_the_next_model(two_provider_tier):
    gemini = FakeClient({"gemini-3.5-flash": LlmOutputTruncatedError("MAX_TOKENS, no answer")})
    groq = FakeClient({"openai/gpt-oss-120b": REVIEW_JSON})

    review = _review(_router({"gemini": gemini, "groq": groq}))

    assert review.model_used == "groq:openai/gpt-oss-120b:low"
    assert review.fell_back is True


def test_whole_list_truncated_raises_transient_with_details(two_provider_tier):
    gemini = FakeClient({"gemini-3.5-flash": LlmOutputTruncatedError("MAX_TOKENS, no answer")})
    groq = FakeClient({"openai/gpt-oss-120b": LlmOutputTruncatedError("length, no answer")})

    with pytest.raises(LlmTransientError) as exc_info:
        _review(_router({"gemini": gemini, "groq": groq}))

    # The summary reaching Step Functions is the plain, retryable LlmTransientError.
    assert type(exc_info.value) is LlmTransientError
    message = str(exc_info.value)
    assert "gemini:gemini-3.5-flash:low: MAX_TOKENS, no answer" in message
    assert "groq:openai/gpt-oss-120b:low: length, no answer" in message


# --- HTTP 413: request too large for one model's limit ------------------------------------

# Groq's real response (captured from the API) when a single request exceeds a model's
# tokens-per-minute limit. Its `code` is the same "rate_limit_exceeded" a 429 carries, so only
# the HTTP status tells the two apart.
GROQ_413_BODY = {
    "error": {
        "message": (
            "Request too large for model `openai/gpt-oss-120b` in organization `org_test` "
            "service tier `on_demand` on tokens per minute (TPM): Limit 8000, Requested 18340, "
            "please reduce your message size and try again."
        ),
        "type": "tokens",
        "code": "rate_limit_exceeded",
    }
}


def test_groq_413_request_too_large_is_transient_not_permanent():
    session = FakeSession(FakeResponse(413, GROQ_413_BODY))

    with pytest.raises(LlmTransientError, match="413") as exc_info:
        _groq(session).generate("openai/gpt-oss-120b", "a very large prompt", "medium")

    assert "Request too large" in str(exc_info.value)


def test_413_from_the_first_model_advances_to_the_next_model(monkeypatch):
    """The diff plus the retrieved RAG files can exceed one model's limit. The review must not
    abort: the router moves on to the next entry, whose model may accept the same prompt."""
    monkeypatch.setenv(
        "LLM_MODELS_MEDIUM", "groq:openai/gpt-oss-120b:medium,gemini:gemini-3.5-flash:low"
    )
    groq_session = FakeSession(FakeResponse(413, GROQ_413_BODY))
    gemini = FakeClient({"gemini-3.5-flash": REVIEW_JSON})
    router = _router({"groq": _groq(groq_session), "gemini": gemini})

    review = _review(router)

    assert len(groq_session.requests) == 1  # tried once, not retried against the same model
    assert gemini.calls == [("gemini-3.5-flash", "low")]
    assert review.model_used == "gemini:gemini-3.5-flash:low"
    assert review.fell_back is True
    assert review.summary == "Adds logging."


def test_413_on_every_model_raises_transient_naming_each_model(monkeypatch):
    monkeypatch.setenv(
        "LLM_MODELS_MEDIUM", "groq:openai/gpt-oss-120b:medium,gemini:gemini-3.5-flash:low"
    )
    groq_session = FakeSession(FakeResponse(413, GROQ_413_BODY))
    gemini_session = FakeSession(FakeResponse(413, {"error": {"message": "payload too large"}}))
    router = _router({"groq": _groq(groq_session), "gemini": _gemini(gemini_session)})

    with pytest.raises(LlmTransientError) as exc_info:
        _review(router)

    message = str(exc_info.value)
    assert "groq:openai/gpt-oss-120b:medium" in message and "Request too large" in message
    assert "gemini:gemini-3.5-flash:low" in message and "payload too large" in message


# --- Review quality rubric: category/severity, no praise, security checklist -------------


def test_build_prompt_requires_category_and_severity_and_forbids_praise():
    prompt = build_prompt("+diff", paths=["src/App.java"])

    assert '"category"' in prompt
    assert '"severity"' in prompt
    assert "never praise" in prompt
    for category in ("bug", "security", "performance", "maintainability"):
        assert category in prompt


def test_build_prompt_omits_security_checklist_for_non_sensitive_paths():
    prompt = build_prompt("+diff", paths=["src/main/java/app/tasks/TaskController.java"])

    assert "user enumeration" not in prompt.lower()


@pytest.mark.parametrize(
    "path",
    [
        "src/main/java/app/auth/AuthController.java",
        "src/main/java/app/security/SecurityConfig.java",
        "src/main/java/app/JwtValidator.java",
        "src/main/java/app/CryptoUtils.java",
        "src/main/java/app/PasswordEncoder.java",
        "src/main/java/app/SessionManager.java",
        "src/main/java/app/LoginController.java",
        "src/main/java/app/TokenService.java",
    ],
)
def test_build_prompt_adds_security_checklist_for_sensitive_paths(path):
    prompt = build_prompt("+diff", paths=[path])

    assert "user enumeration" in prompt.lower()
    assert "credentials, tokens, or passwords written to logs" in prompt.lower()


def test_build_prompt_security_checklist_is_generic_not_pr3_specific():
    """The checklist must describe categories of security issue, not name this repo's own
    demo PR's specific bugs (clock skew hours, InMemoryUsers, etc.) — otherwise it would only
    help that one PR, not a real auth change."""
    prompt = build_prompt("+diff", paths=["src/main/java/app/auth/JwtValidator.java"])

    assert "clock skew" not in prompt.lower()
    assert "InMemoryUsers" not in prompt
    assert "24" not in prompt.split("=== DIFF")[0]


def test_build_prompt_checklist_triggers_on_any_path_not_only_the_diff():
    """`paths` is the event's full changed-file list — a security-sensitive file elsewhere in
    the same PR must still trigger the checklist, even if `diff_text` itself is something
    else (e.g. this file came from context, or diff_text covers multiple files)."""
    prompt = build_prompt("+diff touching README.md only", paths=["README.md", "src/auth/Jwt.java"])

    assert "user enumeration" in prompt.lower()


# --- Per-call observability: finish_reason/usage logged on success, not only on failure ---


def test_gemini_logs_finish_reason_and_usage_on_success(caplog):
    body = _gemini_ok({"text": REVIEW_JSON})
    body._body["usageMetadata"] = {"promptTokenCount": 10, "candidatesTokenCount": 5}

    with caplog.at_level("INFO"):
        _gemini(FakeSession(body)).generate("gemini-3.5-flash", "p")

    assert any(
        "gemini:gemini-3.5-flash" in r.message and "usage=" in r.message for r in caplog.records
    )


def test_groq_logs_finish_reason_and_usage_on_success(caplog):
    body = _groq_ok(REVIEW_JSON)
    body._body["usage"] = {"prompt_tokens": 10, "completion_tokens": 5}

    with caplog.at_level("INFO"):
        _groq(FakeSession(body)).generate("openai/gpt-oss-120b", "p")

    assert any(
        "groq:openai/gpt-oss-120b" in r.message and "usage=" in r.message for r in caplog.records
    )


# --- Gemini "high" reasoning: its own (much larger) timeout and attempt budget ------------


def test_gemini_high_reasoning_uses_a_larger_read_timeout():
    session = FakeSession(_gemini_ok({"text": REVIEW_JSON}))

    _gemini(session).generate("gemini-3.5-flash", "p", reasoning="high")

    assert session.requests[0]["timeout"] == (5, GEMINI_HIGH_REASONING_READ_TIMEOUT_SECONDS)


def test_gemini_low_reasoning_still_uses_the_default_read_timeout():
    session = FakeSession(_gemini_ok({"text": REVIEW_JSON}))

    _gemini(session).generate("gemini-3.5-flash", "p", reasoning="low")

    assert session.requests[0]["timeout"] == (5, 45)


def test_deadline_check_uses_the_larger_budget_for_a_gemini_high_attempt(monkeypatch):
    """Enough time for a normal (50s) attempt but not for Gemini's high-reasoning (95s)
    attempt must still stop before starting it — the per-spec budget, not the flat default,
    is what the router checks. This order (Groq first) now matches the real LLM_MODELS_HIGH
    (see the next test), so the budget check here is exercised on the *second* attempt."""
    monkeypatch.setenv(
        "LLM_MODELS_HIGH", "groq:openai/gpt-oss-120b:medium,gemini:gemini-3.5-flash:high"
    )
    groq = FakeClient({"openai/gpt-oss-120b": LlmTransientError("HTTP 503")})
    gemini = FakeClient({"gemini-3.5-flash": REVIEW_JSON})
    remaining = iter([150_000, GEMINI_HIGH_REASONING_ATTEMPT_BUDGET_MS - 1])
    router = _router({"groq": groq, "gemini": gemini}, remaining_time_ms=lambda: next(remaining))

    with pytest.raises(LlmTransientError) as exc_info:
        router.generate_review(pr_id="42", diff_text="+x", complexity=Complexity.HIGH)

    assert gemini.calls == []
    assert "Not attempted: gemini:gemini-3.5-flash:high" in str(exc_info.value)


def test_enough_time_for_gemini_high_budget_lets_the_attempt_run(monkeypatch):
    monkeypatch.setenv(
        "LLM_MODELS_HIGH", "groq:openai/gpt-oss-120b:medium,gemini:gemini-3.5-flash:high"
    )
    groq = FakeClient({"openai/gpt-oss-120b": LlmTransientError("HTTP 503")})
    gemini = FakeClient({"gemini-3.5-flash": REVIEW_JSON})
    router = _router(
        {"groq": groq, "gemini": gemini},
        remaining_time_ms=lambda: GEMINI_HIGH_REASONING_ATTEMPT_BUDGET_MS,
    )

    review = router.generate_review(pr_id="42", diff_text="+x", complexity=Complexity.HIGH)

    assert review.model_used == "gemini:gemini-3.5-flash:high"
    assert review.fell_back is True


def test_real_high_tier_order_leads_with_groq_medium_falling_back_to_gemini_high(monkeypatch):
    """LLM_MODELS_HIGH's actual order: groq:openai/gpt-oss-120b:medium first,
    gemini:gemini-3.5-flash:high as the fallback — the same lead-with-Groq pattern as
    low/medium (see research.md's "High-tier order reverted: Groq leads again"). When Groq's
    first attempt succeeds, Gemini's deeper high-reasoning pass is never reached; it only runs
    when Groq's attempt fails, and then needs its own 95s budget before the router will even
    start it."""
    monkeypatch.setenv(
        "LLM_MODELS_HIGH", "groq:openai/gpt-oss-120b:medium,gemini:gemini-3.5-flash:high"
    )
    gemini = FakeClient({"gemini-3.5-flash": REVIEW_JSON})
    groq = FakeClient({"openai/gpt-oss-120b": REVIEW_JSON})

    groq_succeeds = _router(
        {"gemini": gemini, "groq": groq},
        remaining_time_ms=lambda: GEMINI_HIGH_REASONING_ATTEMPT_BUDGET_MS,
    )
    review = groq_succeeds.generate_review(pr_id="42", diff_text="+x", complexity=Complexity.HIGH)
    assert review.model_used == "groq:openai/gpt-oss-120b:medium"
    assert review.fell_back is False
    assert gemini.calls == []

    groq_fails = FakeClient({"openai/gpt-oss-120b": LlmTransientError("HTTP 503")})
    too_little_time = _router(
        {"gemini": gemini, "groq": groq_fails},
        remaining_time_ms=lambda: GEMINI_HIGH_REASONING_ATTEMPT_BUDGET_MS - 1,
    )
    with pytest.raises(LlmTransientError) as exc_info:
        too_little_time.generate_review(pr_id="42", diff_text="+x", complexity=Complexity.HIGH)
    assert "Not attempted: gemini:gemini-3.5-flash:high" in str(exc_info.value)
    assert gemini.calls == []


def test_paths_are_threaded_from_generate_review_into_the_prompt(monkeypatch):
    monkeypatch.setenv("LLM_MODELS_MEDIUM", "gemini:gemini-3.5-flash:low")
    session = FakeSession(_gemini_ok({"text": REVIEW_JSON}))
    router = _router({"gemini": _gemini(session)})

    router.generate_review(
        pr_id="42",
        diff_text="+diff",
        complexity=Complexity.MEDIUM,
        paths=["src/auth/Jwt.java"],
    )

    sent_prompt = session.requests[0]["payload"]["contents"][0]["parts"][0]["text"]
    assert "user enumeration" in sent_prompt.lower()
