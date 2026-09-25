"""LLM review generation across providers (Principle III).

Each complexity tier resolves to an ordered fallback list from LLM_MODELS_LOW/MEDIUM/HIGH.
Every entry is `provider:model[:reasoning]` — e.g. `groq:openai/gpt-oss-120b:low` or
`gemini:gemini-3.5-flash:low` — so no model name is hardcoded in code. One client per
provider (GeminiClient, GroqClient) calls that provider's HTTP API directly; there is no
routing service in front of them. MultiProviderLlmRouter tries the entries in order, falling
back across models and providers, and stops early when the Lambda's remaining time can't fit
another attempt. See research.md's "Multi-provider model fallback" for the measurements
behind the lists.
"""

import json
import logging
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass

from contracts.models import Complexity, ContextChunk, GeneratedReview, ReviewCommentDraft
from integrations.config import ConfigError, require_env

logger = logging.getLogger(__name__)

# Per-request HTTP timeouts. The read timeout bounds how long one model may take to answer;
# a reasoning model reviewing a real diff took ~10s in measurements, and slow *failures*
# (500/503 after 90-108s) are cut off here rather than waited out.
CONNECT_TIMEOUT_SECONDS = 5
READ_TIMEOUT_SECONDS = 45

# An attempt only starts if at least this much Lambda time remains — one full connect + read
# timeout — so an attempt that runs to its limit still finishes before Lambda's own timeout.
# Otherwise Lambda would kill the function mid-request and Step Functions would see
# Sandbox.Timedout (not retried, and without the per-model failure details).
ATTEMPT_TIME_BUDGET_MS = (CONNECT_TIMEOUT_SECONDS + READ_TIMEOUT_SECONDS) * 1000

PROVIDERS = ("gemini", "groq")

# Complexity tier -> the env var suffix holding that tier's model fallback list.
_TIER_ENV_SUFFIX: dict[Complexity, str] = {
    Complexity.LOW: "LOW",
    Complexity.MEDIUM: "MEDIUM",
    Complexity.HIGH: "HIGH",
}


@dataclass(frozen=True)
class ModelSpec:
    """One entry of a tier's fallback list: which provider, which model, and optionally which
    reasoning level to request. No reasoning level means no reasoning configuration is sent
    at all — some models (e.g. Gemma) reject any such configuration with HTTP 400."""

    provider: str
    model: str
    reasoning: str | None = None

    @property
    def label(self) -> str:
        """The entry as written in config; reported as GeneratedReview.model_used."""
        base = f"{self.provider}:{self.model}"
        return f"{base}:{self.reasoning}" if self.reasoning else base


def parse_model_spec(entry: str) -> ModelSpec:
    """Parse `provider:model[:reasoning]`. Model ids may contain `/` (e.g.
    `openai/gpt-oss-120b`) but never `:`, so splitting on `:` is unambiguous."""
    parts = [part.strip() for part in entry.split(":")]
    if len(parts) not in (2, 3) or not all(parts):
        raise ConfigError(f"Invalid model entry {entry!r}: expected provider:model[:reasoning]")
    if parts[0] not in PROVIDERS:
        raise ConfigError(
            f"Unknown provider {parts[0]!r} in model entry {entry!r}; expected one of {PROVIDERS}"
        )
    reasoning = parts[2] if len(parts) == 3 else None
    return ModelSpec(provider=parts[0], model=parts[1], reasoning=reasoning)


def models_for_complexity(complexity: Complexity) -> list[ModelSpec]:
    """Resolve a tier's fallback list from its LLM_MODELS_{tier} env var: comma-separated
    `provider:model[:reasoning]` entries, in order of preference."""
    name = f"LLM_MODELS_{_TIER_ENV_SUFFIX[complexity]}"
    entries = [entry.strip() for entry in require_env(name).split(",") if entry.strip()]
    if not entries:
        raise ConfigError(f"Environment variable {name} lists no models")
    return [parse_model_spec(entry) for entry in entries]


# Fixed model labels for the stub only, so tests stay deterministic and network/env-var-free
# (Principle IV, FR-011) — never read by the real clients.
STUB_MODEL_BY_COMPLEXITY: dict[Complexity, str] = {
    Complexity.LOW: "groq:openai/gpt-oss-120b:low",
    Complexity.MEDIUM: "groq:openai/gpt-oss-120b:medium",
    Complexity.HIGH: "groq:openai/gpt-oss-120b:medium",
}


class LlmRouterError(Exception):
    """Raised when a provider rejects the request or returns an unusable response — a
    failure neither a retry nor another model will fix (400, 401/403, a blocked response, or
    an empty one that was not cut off by the output limit — see LlmOutputTruncatedError).
    Stops the fallback list immediately."""


class LlmTransientError(LlmRouterError):
    """Raised when a model fails in a way that typically clears up on its own: HTTP 429 (rate
    limit), 500/502/503/504 (503 "high demand" was seen on real calls), or a network
    timeout/connection error — and, from the router, when every model in the tier failed
    this way or the Lambda ran out of time for another attempt.

    The class name is a cross-repo contract: Lambda reports it as the invocation's
    errorType, and codereview-infra's Step Functions definition matches its Retry on that
    exact string — so this MUST NOT be renamed. Subclassing LlmRouterError keeps any
    `except LlmRouterError` in this repo catching both; Step Functions matches the concrete
    class name only, so the two stay distinguishable there.
    """


class LlmOutputTruncatedError(LlmTransientError):
    """Raised when a model hit its output token limit before writing any answer — Groq's
    `finish_reason: "length"` or Gemini's `finishReason: "MAX_TOKENS"` with empty content,
    typically because reasoning used the whole output budget. Another model may well
    succeed, so the router moves on to the next one. It subclasses LlmTransientError so that
    if the whole list ends this way the router's summary error is retryable; it is never
    raised to Step Functions directly (the router always wraps failures in a summary)."""


class LlmModelNotFoundError(LlmRouterError):
    """Raised when a provider says the model doesn't exist or was retired (HTTP 404, or
    Groq's `model_not_found`/`model_decommissioned` error codes). Providers remove models
    from their free tiers without notice, so the router moves on to the next model instead
    of stopping. If *every* model in the list is gone, this reaches Step Functions as-is: a
    configuration problem, which retrying won't fix."""


# Retried by moving to the next model, and at the Step Functions level (LlmTransientError).
_TRANSIENT_STATUS_CODES = frozenset({429, 500, 502, 503, 504})
# Groq's (OpenAI-style) error codes for a model that no longer exists; Groq can return them
# with HTTP 400 rather than 404.
_MODEL_GONE_ERROR_CODES = frozenset({"model_not_found", "model_decommissioned"})


def _error_code(response) -> str | None:
    try:
        return (response.json().get("error") or {}).get("code")
    except Exception:
        return None


def _post_json(session, url: str, payload: dict, headers: dict, label: str) -> dict:
    """POST one generation request and classify failures the same way for every provider:
    transient (next model, and Step Functions retry), model gone (next model), or permanent
    (stop)."""
    import requests

    try:
        response = session.post(
            url,
            json=payload,
            headers=headers,
            timeout=(CONNECT_TIMEOUT_SECONDS, READ_TIMEOUT_SECONDS),
        )
    except (requests.Timeout, requests.ConnectionError) as exc:
        raise LlmTransientError(f"{label} request failed: {exc}") from exc
    except Exception as exc:
        raise LlmRouterError(f"{label} request failed: {exc}") from exc

    status = response.status_code
    if status in _TRANSIENT_STATUS_CODES:
        raise LlmTransientError(f"{label} returned HTTP {status}: {response.text[:300]}")
    if status == 404 or (status == 400 and _error_code(response) in _MODEL_GONE_ERROR_CODES):
        raise LlmModelNotFoundError(
            f"{label} not found (HTTP {status}): {response.text[:300]}"
        )
    if status >= 400:
        raise LlmRouterError(f"{label} returned HTTP {status}: {response.text[:300]}")

    try:
        return response.json()
    except Exception as exc:
        raise LlmRouterError(f"{label} returned a non-JSON response: {exc}") from exc


def _answer_text(candidate: dict) -> str:
    """Concatenate the answer text of all of a Gemini candidate's parts, skipping reasoning.

    Thinking models (e.g. gemini-3.5-flash) can split a response across several parts and
    mark internal reasoning with `"thought": true`; reading only `parts[0].text` could return
    the reasoning instead of the answer, or miss part of the answer. A `thoughtSignature` on a
    part is an opaque token for multi-turn continuity, not a reasoning marker, so parts that
    carry one are still read. Parts with no `text` (e.g. a signature-only part) contribute
    nothing.
    """
    parts = (candidate.get("content") or {}).get("parts") or []
    return "".join(part.get("text", "") for part in parts if not part.get("thought"))


class ModelClient(ABC):
    @abstractmethod
    def generate(self, model: str, prompt: str, reasoning: str | None = None) -> str:
        """Return the model's answer text for `prompt`. Raises LlmTransientError,
        LlmModelNotFoundError or LlmRouterError (see _post_json)."""


class GeminiClient(ModelClient):
    """Gemini API `generateContent`. The prompt carries all instructions as plain user text
    (no `systemInstruction`), which Gemma models served by the same API also accept."""

    def __init__(self, api_base: str, api_key: str, session=None):
        import requests

        self._api_base = api_base
        self._api_key = api_key
        self._session = session or requests.Session()

    def generate(self, model: str, prompt: str, reasoning: str | None = None) -> str:
        label = f"gemini:{model}"
        payload: dict = {"contents": [{"parts": [{"text": prompt}]}]}
        if reasoning:
            # Only sent when the list entry asks for it: Gemma rejects any thinkingConfig
            # with HTTP 400 ("Thinking level is not supported for this model").
            payload["generationConfig"] = {"thinkingConfig": {"thinkingLevel": reasoning}}
        # Gemini authenticates with this header, not an OAuth-style Bearer token.
        headers = {"x-goog-api-key": self._api_key}
        url = f"{self._api_base}/models/{model}:generateContent"
        data = _post_json(self._session, url, payload, headers, label)

        # An empty/missing `candidates` list (HTTP 200) means the response was blocked by a
        # safety filter: a generation failure (FR-008), so PostComment never receives it.
        candidates = data.get("candidates") or []
        if not candidates:
            raise LlmRouterError(
                f"{label} returned no candidates (response likely safety-filtered)"
            )
        try:
            text = _answer_text(candidates[0])
        except Exception as exc:
            raise LlmRouterError(f"{label} returned an unreadable candidate: {exc}") from exc
        if not text.strip():
            finish_reason = candidates[0].get("finishReason")
            if finish_reason == "MAX_TOKENS":
                raise LlmOutputTruncatedError(
                    f"{label} hit its output token limit before answering (finishReason "
                    f"MAX_TOKENS, usage {data.get('usageMetadata')})"
                )
            raise LlmRouterError(
                f"{label} returned an empty response (finishReason {finish_reason})"
            )
        return text


# Explicit output budget per Groq reasoning effort. Without one, gpt-oss stops at about 3,072
# output tokens (reasoning + answer), and at medium effort real reviews used 3,111-3,668, so
# the answer was cut off. 5,500 is ~1.5x the largest measured output; Groq charges the free
# tier's 8,000 tokens/minute by tokens actually used, not by this reservation (a 5,500 cap
# with a 2,794-token prompt was accepted). Low effort used at most 1,073 output tokens, well
# under the default, so it gets no explicit cap. See research.md, "Multi-provider model
# fallback".
GROQ_MAX_COMPLETION_TOKENS: dict[str, int] = {"medium": 5500}


class GroqClient(ModelClient):
    """Groq's OpenAI-compatible `chat/completions` endpoint."""

    def __init__(self, api_base: str, api_key: str, session=None):
        import requests

        self._api_base = api_base
        self._api_key = api_key
        self._session = session or requests.Session()

    def generate(self, model: str, prompt: str, reasoning: str | None = None) -> str:
        label = f"groq:{model}"
        payload: dict = {"model": model, "messages": [{"role": "user", "content": prompt}]}
        if reasoning:
            # Groq's reasoning control for models that support it (e.g. gpt-oss: low/medium/
            # high); omitted otherwise, since models without it reject the parameter.
            payload["reasoning_effort"] = reasoning
            if reasoning in GROQ_MAX_COMPLETION_TOKENS:
                payload["max_completion_tokens"] = GROQ_MAX_COMPLETION_TOKENS[reasoning]
        headers = {"Authorization": f"Bearer {self._api_key}"}
        url = f"{self._api_base}/chat/completions"
        data = _post_json(self._session, url, payload, headers, label)

        choices = data.get("choices") or []
        if not choices:
            raise LlmRouterError(f"{label} returned no choices")
        # gpt-oss returns its reasoning in a separate `message.reasoning` field, so `content`
        # holds only the answer.
        text = (choices[0].get("message") or {}).get("content") or ""
        if not text.strip():
            finish_reason = choices[0].get("finish_reason")
            if finish_reason == "length":
                raise LlmOutputTruncatedError(
                    f"{label} hit its output token limit before answering (finish_reason "
                    f"length, usage {data.get('usage')})"
                )
            raise LlmRouterError(
                f"{label} returned an empty response (finish_reason {finish_reason})"
            )
        return text


def build_prompt(diff_text: str, context_chunks: list[ContextChunk] | None = None) -> str:
    """Diff under review, plus retrieved project files clearly labelled as *not* the diff.

    Without that separation the model tends to review the context files as if they were part
    of the change. An absent/empty context (no RAG index yet) simply omits the section.

    Instructs the model to reply with ONLY the structured JSON shape `parse_review_response`
    below expects — no markdown fences, no preamble — so review comments can be anchored to
    specific diff lines (inline PR comments) instead of landing as one undifferentiated block
    of text. The diff is passed through with its hunk headers intact (`@@ -a,b +c,d @@`),
    which is what lets the model work out each line's post-change number at all.
    """
    prompt = (
        "You are reviewing a pull request diff.\n\n"
        "Respond with ONLY valid JSON — no markdown code fences, no preamble or trailing "
        "text — in exactly this shape:\n"
        '{"summary": "<2-3 sentence overview of the change>", '
        '"comments": [{"path": "<file path exactly as it appears in the diff>", '
        '"line": <line number, integer>, "body": "<observation about that line>"}]}\n\n'
        '`comments` MAY be an empty list ("comments": []) when there is nothing line-specific '
        "to flag — that is a valid, complete review, not an error.\n\n"
        '`line` MUST be a line number on the file\'s state AFTER the change (the diff\'s "+" '
        "side), and MUST refer only to a line that actually appears in the diff below. Use "
        "each hunk's header (`@@ -old_start,old_count +new_start,new_count @@`) to work out "
        "line numbers: the first line following a hunk header is `new_start`, and the number "
        "increments for every following context line (starts with a space) or added line "
        "(starts with `+`). Removed lines (start with `-`) do not exist on the post-change "
        "side and MUST NOT be used as `line`.\n\n"
        "=== DIFF UNDER REVIEW ===\n"
        f"{diff_text}\n"
    )
    if context_chunks:
        prompt += (
            "\n=== ADDITIONAL PROJECT CONTEXT ===\n"
            "Existing files from the repository, retrieved for reference only. They are NOT "
            "part of the change under review — do not report issues in them.\n"
        )
        for chunk in context_chunks:
            prompt += f"\n--- {chunk.path} ---\n{chunk.text}\n"
    return prompt


def _strip_code_fence(text: str) -> str:
    """Drop a wrapping ```/```json fence if present. The prompt explicitly forbids code
    fences, but models don't always comply, and stripping one is cheap defensive insurance
    before attempting json.loads.
    """
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    lines = stripped.splitlines()[1:]  # drop the opening fence (with optional language tag)
    if lines and lines[-1].strip().startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines)


def parse_review_response(raw_text: str, pr_id: str, model_used: str) -> GeneratedReview:
    """Parse a model's response text against the structured-JSON contract `build_prompt`
    instructs.

    Defensive by design: a model can still reply with prose, truncated JSON, or the wrong
    shape despite the prompt. Any such failure degrades to the pre-structured-comments
    behavior — the whole raw response becomes `summary`, `comments` is forced empty — rather
    than raising and losing the review entirely (FR-008 still governs a genuinely *empty*
    response; the clients reject that before this function ever runs). `parse_fallback`
    on the returned `GeneratedReview` records that this happened, and a warning is logged, so
    a persistently malformed model output is observable rather than silently swallowed.
    """
    try:
        parsed = json.loads(_strip_code_fence(raw_text))
        if not isinstance(parsed, dict):
            raise ValueError("top-level JSON value is not an object")

        summary = parsed["summary"]
        if not isinstance(summary, str) or not summary.strip():
            raise ValueError("'summary' is missing or empty")

        raw_comments = parsed.get("comments", [])
        if not isinstance(raw_comments, list):
            raise ValueError("'comments' is not a list")

        comments = [
            ReviewCommentDraft(path=c["path"], line=int(c["line"]), body=c["body"])
            for c in raw_comments
        ]
        return GeneratedReview(
            pr_id=pr_id,
            summary=summary,
            comments=comments,
            model_used=model_used,
            parse_fallback=False,
        )
    except Exception as exc:
        logger.warning(
            "%s response for PR %s was not the instructed structured-JSON shape (%s); "
            "falling back to summary-only with no inline comments",
            model_used,
            pr_id,
            exc,
        )
        return GeneratedReview(
            pr_id=pr_id,
            summary=raw_text.strip() or "(unparseable review response)",
            comments=[],
            model_used=model_used,
            parse_fallback=True,
        )


class LlmRouter(ABC):
    @abstractmethod
    def generate_review(
        self,
        pr_id: str,
        diff_text: str,
        complexity: Complexity,
        context_chunks: list[ContextChunk] | None = None,
    ) -> GeneratedReview:
        """Generate a review for `diff_text` (plus optional retrieved `context_chunks`)."""


class MultiProviderLlmRouter(LlmRouter):
    """Tries a tier's models in order, across providers.

    - LlmTransientError (429/5xx/timeout, or LlmOutputTruncatedError: output limit reached
      with no answer) or LlmModelNotFoundError (model gone): log it and try the next model.
    - Any other LlmRouterError (400/401/403, a blocked or otherwise empty response): stop
      immediately — a bad request or key won't be fixed by switching models.
    - Before each attempt, if the Lambda's remaining time is under ATTEMPT_TIME_BUDGET_MS,
      stop with LlmTransientError instead of starting an attempt Lambda would cut off.
    - When the list is exhausted: LlmModelNotFoundError if every model was gone, otherwise
      LlmTransientError, so Step Functions' Retry re-runs the whole list.

    `client_factories` maps each provider to a function that builds its client; a client is
    only built — and its API key only fetched — the first time one of its models is tried.
    """

    def __init__(
        self,
        client_factories: dict[str, Callable[[], ModelClient]],
        remaining_time_ms: Callable[[], int] | None = None,
    ):
        self._client_factories = client_factories
        self._clients: dict[str, ModelClient] = {}
        self._remaining_time_ms = remaining_time_ms

    def _client(self, provider: str) -> ModelClient:
        if provider not in self._clients:
            if provider not in self._client_factories:
                raise ConfigError(f"No client configured for provider {provider!r}")
            self._clients[provider] = self._client_factories[provider]()
        return self._clients[provider]

    def generate_review(
        self,
        pr_id: str,
        diff_text: str,
        complexity: Complexity,
        context_chunks: list[ContextChunk] | None = None,
    ) -> GeneratedReview:
        specs = models_for_complexity(complexity)
        prompt = build_prompt(diff_text, context_chunks)
        failures: list[str] = []
        every_failure_was_model_gone = True

        for attempt, spec in enumerate(specs):
            if (
                self._remaining_time_ms is not None
                and self._remaining_time_ms() < ATTEMPT_TIME_BUDGET_MS
            ):
                failed = "; ".join(failures) or "none"
                not_attempted = ", ".join(s.label for s in specs[attempt:])
                raise LlmTransientError(
                    f"Stopped before {spec.label}: less than {ATTEMPT_TIME_BUDGET_MS // 1000}s "
                    f"of Lambda time left for another attempt. Failed: {failed}. "
                    f"Not attempted: {not_attempted}"
                )
            try:
                text = self._client(spec.provider).generate(spec.model, prompt, spec.reasoning)
            except (LlmTransientError, LlmModelNotFoundError) as exc:
                if not isinstance(exc, LlmModelNotFoundError):
                    every_failure_was_model_gone = False
                failures.append(f"{spec.label}: {exc}")
                logger.warning(
                    "Model %s failed for PR %s (%s); %s",
                    spec.label,
                    pr_id,
                    exc,
                    "trying next model" if attempt + 1 < len(specs) else "no models left",
                )
                continue
            review = parse_review_response(text, pr_id=pr_id, model_used=spec.label)
            return review.model_copy(update={"fell_back": attempt > 0})

        summary = f"All {len(specs)} model(s) for tier {complexity.value!r} failed: " + "; ".join(
            failures
        )
        if every_failure_was_model_gone:
            raise LlmModelNotFoundError(summary)
        raise LlmTransientError(summary)


class StubLlmRouter(LlmRouter):
    """Network-free stand-in for tests (Principle IV, FR-011).

    Runs its fixed response text through the same `parse_review_response` the real router
    uses, so the stub exercises the identical structured-parsing/fallback path rather than
    hand-building a GeneratedReview that could drift from what the real router actually
    produces. Configure `.comments` for a response with inline comments, or
    `.malformed_response = True` to exercise the defensive parse-fallback path.
    """

    def __init__(self):
        self.calls: list[dict] = []
        self.fail: bool = False
        self.empty_output: bool = False
        self.malformed_response: bool = False
        self.comments: list[dict] = []

    def generate_review(
        self,
        pr_id: str,
        diff_text: str,
        complexity: Complexity,
        context_chunks: list[ContextChunk] | None = None,
    ) -> GeneratedReview:
        model = STUB_MODEL_BY_COMPLEXITY[complexity]
        self.calls.append(
            {
                "pr_id": pr_id,
                "diff_text": diff_text,
                "complexity": complexity,
                "context_chunks": context_chunks,
                "prompt": build_prompt(diff_text, context_chunks),
                "model": model,
            }
        )
        if self.fail:
            raise LlmRouterError("stub configured to simulate an LLM failure")
        if self.empty_output:
            # Mirrors the real clients: a genuinely empty response is a hard failure (FR-008).
            raise LlmRouterError("stub configured to simulate an empty LLM response")

        if self.malformed_response:
            raw_text = f"Automated review ({model}): {diff_text[:80]}"
        else:
            raw_text = json.dumps(
                {
                    "summary": f"Automated review ({model}): {diff_text[:80]}",
                    "comments": self.comments,
                }
            )
        return parse_review_response(raw_text, pr_id=pr_id, model_used=model)
