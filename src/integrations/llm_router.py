"""LLM review generation across providers (Principle III).

Each complexity tier resolves to an ordered fallback list from LLM_MODELS_LOW/MEDIUM/HIGH.
Every entry is `provider:model[:reasoning]` — e.g. `groq:openai/gpt-oss-120b:low` or
`gemini:gemini-3.5-flash:low` — so no model name is hardcoded in code. One client per
provider (GeminiClient, GroqClient, CerebrasClient) calls that provider's HTTP API directly;
there is no routing service in front of them. Groq and Cerebras both serve the same
OpenAI-compatible `gpt-oss-120b` model, so GroqClient and CerebrasClient share their request/
response handling via `_openai_compatible_generate` and differ only in base URL, API key, and
per-effort output cap — Cerebras' free tier has a much larger 30K TPM ceiling than Groq's 8K,
so it gets a bigger `max_completion_tokens` at `medium` effort (see
`CEREBRAS_MAX_COMPLETION_TOKENS`). MultiProviderLlmRouter tries the entries in order, falling
back across models and providers, and stops early when the Lambda's remaining time can't fit
another attempt (per-attempt budget, since Gemini's "high" reasoning needs much longer than
everything else — see `_attempt_budget_ms`). See research.md's "Multi-provider model
fallback" for the measurements behind the lists.

build_prompt's rubric requires every inline comment to be a real problem with a `category`
and `severity` — never praise or description, which belong in `summary` only — and appends a
security checklist when any changed path looks auth-adjacent (research.md's "Review quality
rubric"). Every successful call is logged (`finish_reason`/`finishReason` + token usage), not
only failed ones, and every call that reaches a provider also gets one JSON `llm_call` line
(outcome, HTTP status, time, normalised token counts) for the CloudWatch dashboard.

Retrieved context from a version 2 index (method-level chunks, specs/002-method-chunking)
is sized per model attempt: each provider has a prompt budget (`PROMPT_TOKEN_BUDGET`), the
context is packed best-first into what the instructions and diff leave of it, and a
provider whose budget the diff alone already exceeds is skipped without being called. A
version 1 context (whole files) keeps the pre-002 prompt exactly, so deploying this code
before the index switches to version 2 changes nothing.
"""

import json
import logging
import re
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass

from contracts.models import Complexity, ContextChunk, GeneratedReview, ReviewCommentDraft
from contracts.token_estimate import estimate_tokens
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

# Gemini's "high" thinkingLevel is dramatically slower than "low" — measured 63s (vs. 9-31s at
# "low") on a realistic diff-plus-context prompt, with real variance observed between runs.
# It needs its own, much larger read timeout and attempt budget; every other client/reasoning
# combination still uses READ_TIMEOUT_SECONDS/ATTEMPT_TIME_BUDGET_MS above. See research.md,
# "High-tier reasoning: why Gemini, not Groq".
GEMINI_HIGH_REASONING_READ_TIMEOUT_SECONDS = 90
GEMINI_HIGH_REASONING_ATTEMPT_BUDGET_MS = (
    CONNECT_TIMEOUT_SECONDS + GEMINI_HIGH_REASONING_READ_TIMEOUT_SECONDS
) * 1000

PROVIDERS = ("gemini", "groq", "cerebras")

# Path substrings (case-insensitive) that trigger the security checklist in build_prompt.
# Matched against every path on the incoming event, not only the ones in the diff, so a
# checklist added to context files still counts. Generic on purpose, not tied to any one
# PR's specific defects.
SECURITY_SENSITIVE_PATH_PATTERN = re.compile(
    r"auth|security|jwt|crypto|password|session|login|token", re.IGNORECASE
)


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


def _attempt_budget_ms(spec: ModelSpec) -> int:
    """How much Lambda time a specific model+reasoning attempt needs in the worst case —
    used for the pre-attempt deadline check. Only Gemini's "high" reasoning gets the larger
    budget; every other entry uses the default."""
    if spec.provider == "gemini" and spec.reasoning == "high":
        return GEMINI_HIGH_REASONING_ATTEMPT_BUDGET_MS
    return ATTEMPT_TIME_BUDGET_MS


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
    Stops the fallback list immediately.

    `http_status` (when the provider answered with an error status) and `empty_response`
    (the provider answered but wrote no text) only feed the `llm_call` log line; they never
    change how an error is classified or handled."""

    def __init__(
        self, message: str = "", *, http_status: int | None = None, empty_response: bool = False
    ):
        super().__init__(message)
        self.http_status = http_status
        self.empty_response = empty_response


class LlmTransientError(LlmRouterError):
    """Raised when a model fails in a way that typically clears up on its own: HTTP 429 (rate
    limit), 500/502/503/504 (503 "high demand" was seen on real calls), or a network
    timeout/connection error — and, from the router, when every model in the tier failed
    this way or the Lambda ran out of time for another attempt.

    HTTP 413 (request too large) is grouped here too, but for a different reason: it does not
    clear up on its own for that model (Groq's free tier answers it when a single request
    exceeds the model's tokens-per-minute limit), yet a *different* model with a larger limit
    may well accept the same prompt, so the router moves on to the next entry rather than
    stopping.

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


class LlmPromptTooLargeError(LlmRouterError):
    """Raised when every model in the tier was skipped because the instructions and diff
    alone exceed each provider's prompt budget (PROMPT_TOKEN_BUDGET). Not transient: the diff
    will be just as large on a retry, so Step Functions must not spend retries on it (its
    Retry matches LlmTransientError by name, which this is not)."""


class LlmModelNotFoundError(LlmRouterError):
    """Raised when a provider says the model doesn't exist or was retired (HTTP 404, or
    Groq's `model_not_found`/`model_decommissioned` error codes). Providers remove models
    from their free tiers without notice, so the router moves on to the next model instead
    of stopping. If *every* model in the list is gone, this reaches Step Functions as-is: a
    configuration problem, which retrying won't fix."""


# Retried by moving to the next model, and at the Step Functions level (LlmTransientError).
# 413 is here so a prompt too large for one model's limit (e.g. the diff plus the retrieved RAG
# files exceeding Groq's tokens-per-minute limit) falls back to the next model instead of
# aborting the review. Unlike the others it will not clear up by retrying the *same* model.
_TRANSIENT_STATUS_CODES = frozenset({413, 429, 500, 502, 503, 504})
# Groq's (OpenAI-style) error codes for a model that no longer exists; Groq can return them
# with HTTP 400 rather than 404.
_MODEL_GONE_ERROR_CODES = frozenset({"model_not_found", "model_decommissioned"})


def _error_code(response) -> str | None:
    try:
        return (response.json().get("error") or {}).get("code")
    except Exception:
        return None


def _post_json(
    session,
    url: str,
    payload: dict,
    headers: dict,
    label: str,
    read_timeout: int = READ_TIMEOUT_SECONDS,
) -> dict:
    """POST one generation request and classify failures the same way for every provider:
    transient (next model, and Step Functions retry), model gone (next model), or permanent
    (stop). `read_timeout` defaults to READ_TIMEOUT_SECONDS; callers whose reasoning setting
    is known to run long (Gemini's "high" thinkingLevel) pass a larger value."""
    import requests

    try:
        response = session.post(
            url,
            json=payload,
            headers=headers,
            timeout=(CONNECT_TIMEOUT_SECONDS, read_timeout),
        )
    except (requests.Timeout, requests.ConnectionError) as exc:
        raise LlmTransientError(f"{label} request failed: {exc}") from exc
    except Exception as exc:
        raise LlmRouterError(f"{label} request failed: {exc}") from exc

    status = response.status_code
    if status in _TRANSIENT_STATUS_CODES:
        raise LlmTransientError(
            f"{label} returned HTTP {status}: {response.text[:300]}", http_status=status
        )
    if status == 404 or (status == 400 and _error_code(response) in _MODEL_GONE_ERROR_CODES):
        raise LlmModelNotFoundError(
            f"{label} not found (HTTP {status}): {response.text[:300]}", http_status=status
        )
    if status >= 400:
        raise LlmRouterError(
            f"{label} returned HTTP {status}: {response.text[:300]}", http_status=status
        )

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


class ModelAnswer(str):
    """A model's answer text that also carries the provider's `finish_reason` and raw token
    `usage`, for the `llm_call` log line. It *is* the answer string, so every caller that
    treats the result as plain text keeps working unchanged."""

    finish_reason: str | None
    usage: dict | None

    def __new__(cls, text: str, finish_reason: str | None = None, usage: dict | None = None):
        answer = super().__new__(cls, text)
        answer.finish_reason = finish_reason
        answer.usage = usage
        return answer


class ModelClient(ABC):
    @abstractmethod
    def generate(self, model: str, prompt: str, reasoning: str | None = None) -> str:
        """Return the model's answer text for `prompt` (a ModelAnswer for the real clients).
        Raises LlmTransientError, LlmModelNotFoundError or LlmRouterError (see _post_json)."""


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
        # "high" thinkingLevel measured 63s on a realistic prompt (vs. 9-31s at "low") — the
        # default READ_TIMEOUT_SECONDS would time this out as a false LlmTransientError.
        read_timeout = (
            GEMINI_HIGH_REASONING_READ_TIMEOUT_SECONDS
            if reasoning == "high"
            else READ_TIMEOUT_SECONDS
        )
        data = _post_json(self._session, url, payload, headers, label, read_timeout=read_timeout)

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
                f"{label} returned an empty response (finishReason {finish_reason})",
                empty_response=True,
            )
        # Observability on the success path too, not only on failure (research.md, "Per-call
        # observability"): usageMetadata's thoughtsTokenCount is what actually explains a slow
        # or truncated-looking call after the fact.
        logger.info(
            "%s answered: finishReason=%s usage=%s",
            label,
            candidates[0].get("finishReason"),
            data.get("usageMetadata"),
        )
        return ModelAnswer(text, candidates[0].get("finishReason"), data.get("usageMetadata"))


# Explicit output budget per Groq reasoning effort. Without one, gpt-oss stops at about 3,072
# output tokens (reasoning + answer), and at medium effort real reviews used 3,111-3,668, so
# the answer was cut off. 5,500 is ~1.5x the largest measured output; Groq charges the free
# tier's 8,000 tokens/minute by tokens actually used, not by this reservation (a 5,500 cap
# with a 2,794-token prompt was accepted). Low effort used at most 1,073 output tokens, well
# under the default, so it gets no explicit cap. See research.md, "Multi-provider model
# fallback".
GROQ_MAX_COMPLETION_TOKENS: dict[str, int] = {"medium": 5500}

# Cerebras serves the same gpt-oss-120b model with a much larger free-tier ceiling (30,000
# uncached TPM vs. Groq's 8,000 — see research.md, "Cerebras as a third fallback provider"),
# and — confirmed with a real test call — enforces it against *actual* usage, not a preflight
# check on the requested cap the way Groq does (a tiny prompt with
# max_completion_tokens=29000 still succeeded, using only the tokens it actually generated).
# 12,000 is sized against the largest real prompt measured so far: PR #8's diff (6,531 tokens)
# plus the rubric (256) and base instructions (269) is ~7,056 prompt tokens; 7,056 + 12,000 =
# 19,056, comfortably under the 30,000 ceiling with ~11,000 tokens of margin for an even
# larger diff or a longer reasoning pass, while still being more than double Groq's 5,500 cap.
# Low effort gets no explicit cap, mirroring Groq's own low-effort entries.
CEREBRAS_MAX_COMPLETION_TOKENS: dict[str, int] = {"medium": 12000}

# Largest prompt, in *estimated* tokens (contracts/token_estimate.py, which over-counts), each
# provider is sent — what its free-tier tokens-per-minute ceiling leaves after reserving room
# for the answer. Derived next to the output caps above on purpose: change one, revisit the
# other. See specs/002-method-chunking/research.md, R9.
#   groq medium: 8,000 TPM - 3,700 (largest measured medium output: 3,668). A starting point,
#     not a proven ceiling — Groq accepted 8,758 *reserved* tokens on PR #7, so its limit is
#     not a strict pre-flight on prompt + max_completion_tokens; T036 measures it. Too low is
#     not "safe": every skipped Groq attempt lands on Cerebras, which allows only 5 RPM.
#   groq low / no reasoning: 8,000 - 1,200 (largest measured low output: 1,073).
#   cerebras: 30,000 TPM - 12,000 (CEREBRAS_MAX_COMPLETION_TOKENS["medium"]).
#   gemini: context window ~1M; a ceiling well below it, to be confirmed against the current
#     free-tier TPM.
PROMPT_TOKEN_BUDGET: dict[tuple[str, str | None], int] = {
    ("groq", "medium"): 4300,
    ("groq", None): 6800,
    ("cerebras", None): 18000,
    ("gemini", None): 100000,
}

# Upper bound on retrieved context for every provider: more context dilutes a review as much
# as too little starves it. ~2.5x the largest whole-file context in baseline.md.
CONTEXT_TOKEN_CAP = 3000


def prompt_budget(spec: ModelSpec) -> int:
    """The budget for this entry: its reasoning level's, else the provider's default."""
    return PROMPT_TOKEN_BUDGET.get(
        (spec.provider, spec.reasoning), PROMPT_TOKEN_BUDGET[(spec.provider, None)]
    )


def _openai_compatible_generate(
    session,
    api_base: str,
    api_key: str,
    label: str,
    model: str,
    prompt: str,
    reasoning: str | None,
    max_completion_tokens_by_effort: dict[str, int],
) -> ModelAnswer:
    """Shared request/response handling for Groq and Cerebras: both serve an OpenAI-compatible
    `chat/completions` endpoint for the same gpt-oss family of models, with identical request
    shape, error shape, and truncation signal (`finish_reason: "length"` with empty content).
    They differ only in base URL, API key, and `max_completion_tokens_by_effort` — passed in
    by each client rather than duplicating this ~30 lines of parsing/error-classification logic
    per provider."""
    payload: dict = {"model": model, "messages": [{"role": "user", "content": prompt}]}
    if reasoning:
        # Both providers' reasoning control for models that support it (e.g. gpt-oss:
        # low/medium/high); omitted otherwise, since models without it reject the parameter.
        payload["reasoning_effort"] = reasoning
        if reasoning in max_completion_tokens_by_effort:
            payload["max_completion_tokens"] = max_completion_tokens_by_effort[reasoning]
    headers = {"Authorization": f"Bearer {api_key}"}
    url = f"{api_base}/chat/completions"
    data = _post_json(session, url, payload, headers, label)

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
            f"{label} returned an empty response (finish_reason {finish_reason})",
            empty_response=True,
        )
    logger.info(
        "%s answered: finish_reason=%s usage=%s",
        label,
        choices[0].get("finish_reason"),
        data.get("usage"),
    )
    return ModelAnswer(text, choices[0].get("finish_reason"), data.get("usage"))


class GroqClient(ModelClient):
    """Groq's OpenAI-compatible `chat/completions` endpoint."""

    def __init__(self, api_base: str, api_key: str, session=None):
        import requests

        self._api_base = api_base
        self._api_key = api_key
        self._session = session or requests.Session()

    def generate(self, model: str, prompt: str, reasoning: str | None = None) -> str:
        return _openai_compatible_generate(
            self._session,
            self._api_base,
            self._api_key,
            f"groq:{model}",
            model,
            prompt,
            reasoning,
            GROQ_MAX_COMPLETION_TOKENS,
        )


class CerebrasClient(ModelClient):
    """Cerebras' OpenAI-compatible `chat/completions` endpoint — same gpt-oss-120b model as
    Groq, with a much larger free-tier TPM ceiling (research.md, "Cerebras as a third fallback
    provider"). Confirmed via a real test call: reasoning_effort (low/medium/high) works the
    same as Groq's, and its error shapes (429 `token_quota_exceeded` for too many tokens, 404
    `model_not_found` for an unknown model) both already fall into the router's existing
    transient/model-gone classification — no special-casing needed beyond a different base
    URL, key, and output cap."""

    def __init__(self, api_base: str, api_key: str, session=None):
        import requests

        self._api_base = api_base
        self._api_key = api_key
        self._session = session or requests.Session()

    def generate(self, model: str, prompt: str, reasoning: str | None = None) -> str:
        return _openai_compatible_generate(
            self._session,
            self._api_base,
            self._api_key,
            f"cerebras:{model}",
            model,
            prompt,
            reasoning,
            CEREBRAS_MAX_COMPLETION_TOKENS,
        )


def _touches_security_sensitive_path(paths: list[str] | None) -> bool:
    """Whether any changed path looks auth/security-adjacent (SECURITY_SENSITIVE_PATH_PATTERN),
    gating the security checklist in build_prompt below."""
    return bool(paths) and any(SECURITY_SENSITIVE_PATH_PATTERN.search(p) for p in paths)


# The rubric section of the prompt: what counts as an inline comment, and what to actively
# check per category (see research.md, "Review quality rubric"). Written to make the model
# hunt for problems in each category rather than default to an empty list, and to keep praise
# and description out of `comments` entirely — both were observed in real reviews (PR #7's
# "which is appropriate", "good for consistency"; PR #8's and #9's genuinely empty comments on
# diffs that, per the rubric below, had at least a minor maintainability point available).
_RUBRIC = (
    "Every entry in `comments` MUST be a real, actionable problem — never praise, never a "
    "description of what the code does. If you would only say something positive or merely "
    "restate the change, put that in `summary` instead and leave it out of `comments`.\n\n"
    "Each comment MUST have a `category`, one of:\n"
    '- "bug": incorrect logic, unhandled edge case, wrong error handling, broken behavior.\n'
    '- "security": credential/secret exposure, injection, auth/authorization bypass, unsafe '
    "deserialization, or similar.\n"
    '- "performance": unnecessary work in a hot path, an avoidable O(n^2) or worse, a leak.\n'
    '- "maintainability": duplication, a misleading name, missing test coverage for new '
    "behavior, a magic value that should be named or configurable.\n\n"
    "Each comment MUST also have a `severity`, one of \"low\", \"medium\", \"high\", reflecting "
    "how much it matters, not how confident you are.\n\n"
    "Actively check every category above against the diff before answering — do not default "
    "to an empty `comments` list just because nothing is obviously broken. A clean diff can "
    "still have a real, if minor, maintainability point; only return `comments: []` after "
    "genuinely checking and finding nothing in any category.\n\n"
)

# Appended only when a changed path matches SECURITY_SENSITIVE_PATH_PATTERN. Generic
# categories, not tied to any specific PR's planted defects, so it helps a real auth change
# without just memorizing one demo's answer key.
_SECURITY_CHECKLIST = (
    "This change touches an authentication/security-sensitive path. In addition to the "
    "rubric above, specifically check for (as `category: \"security\"` comments where "
    "applicable):\n"
    "- Credentials, tokens, or passwords written to logs, error messages, or responses.\n"
    "- User enumeration or timing differences: does the response (message, status code, or "
    "latency) differ in a way that reveals whether a username/account exists, independent of "
    "whether the password was correct?\n"
    "- Signature, expiry, or clock-skew bypass: does any exception handler, broad catch, or "
    "relaxed tolerance cause an invalid, expired, or unverifiable token/credential to be "
    "accepted?\n"
    "- Missing authorization: is the caller's identity checked, but not whether that identity "
    "is *allowed* to do this specific action?\n"
    "- Hardcoded secrets, keys, or credentials in source.\n\n"
)


def build_prompt(
    diff_text: str,
    context_chunks: list[ContextChunk] | None = None,
    paths: list[str] | None = None,
) -> str:
    """Diff under review, plus retrieved project files clearly labelled as *not* the diff.

    Without that separation the model tends to review the context files as if they were part
    of the change. An absent/empty context (no RAG index yet) simply omits the section.

    Instructs the model to reply with ONLY the structured JSON shape `parse_review_response`
    below expects — no markdown fences, no preamble — so review comments can be anchored to
    specific diff lines (inline PR comments) instead of landing as one undifferentiated block
    of text. The diff is passed through with its hunk headers intact (`@@ -a,b +c,d @@`),
    which is what lets the model work out each line's post-change number at all.

    `paths` (the event's full changed-file list, not just what happens to be in `diff_text`)
    gates an additional security checklist — see `_touches_security_sensitive_path`.
    """
    prompt = (
        "You are reviewing a pull request diff.\n\n"
        "Respond with ONLY valid JSON — no markdown code fences, no preamble or trailing "
        "text — in exactly this shape:\n"
        '{"summary": "<2-3 sentence overview of the change>", '
        '"comments": [{"path": "<file path exactly as it appears in the diff>", '
        '"line": <line number, integer>, "body": "<the problem, and why it matters>", '
        '"category": "bug | security | performance | maintainability", '
        '"severity": "low | medium | high"}]}\n\n'
        '`comments` MAY be an empty list ("comments": []) when there is genuinely nothing to '
        "flag after actively checking — see the rubric below.\n\n"
        + _RUBRIC
        + '`line` MUST be a line number on the file\'s state AFTER the change (the diff\'s "+" '
        "side), and MUST refer only to a line that actually appears in the diff below. Use "
        "each hunk's header (`@@ -old_start,old_count +new_start,new_count @@`) to work out "
        "line numbers: the first line following a hunk header is `new_start`, and the number "
        "increments for every following context line (starts with a space) or added line "
        "(starts with `+`). Removed lines (start with `-`) do not exist on the post-change "
        "side and MUST NOT be used as `line`.\n\n"
    )
    if _touches_security_sensitive_path(paths):
        prompt += _SECURITY_CHECKLIST
    prompt += "=== DIFF UNDER REVIEW ===\n" f"{diff_text}\n"
    return prompt + context_section(context_chunks)


def _is_located(chunks: list[ContextChunk] | None) -> bool:
    """Chunks from a version 2 index know where they sit in their file; whole files don't."""
    return bool(chunks) and any(chunk.start_line is not None for chunk in chunks)


def context_section(context_chunks: list[ContextChunk] | None) -> str:
    """The retrieved-context part of the prompt ("" when there is none).

    Whole files (version 1 index) are listed one after another, exactly as before 002.
    Method-level chunks (version 2) are grouped by file: files in order of their best
    chunk, the file's header printed once (again only if a later chunk's header differs,
    e.g. a nested type), then each chunk in line order with its line range.
    """
    if not context_chunks:
        return ""
    if not _is_located(context_chunks):
        section = (
            "\n=== ADDITIONAL PROJECT CONTEXT ===\n"
            "Existing files from the repository, retrieved for reference only. They are NOT "
            "part of the change under review — do not report issues in them.\n"
        )
        for chunk in context_chunks:
            section += f"\n--- {chunk.path} ---\n{chunk.text}\n"
        return section

    by_file: dict[str, list[ContextChunk]] = {}
    for chunk in context_chunks:  # rank order, so dict order = files by best chunk
        by_file.setdefault(chunk.path, []).append(chunk)
    section = (
        "\n=== ADDITIONAL PROJECT CONTEXT ===\n"
        "Existing code from the repository, retrieved for reference only. It is NOT part of "
        "the change under review — do not report issues in it.\n"
    )
    for path, chunks in by_file.items():
        section += f"\n--- {path} ---\n"
        printed_header = None
        for chunk in sorted(chunks, key=lambda c: c.start_line or 0):
            if chunk.header and chunk.header != printed_header:
                section += f"{chunk.header}\n"
                printed_header = chunk.header
            section += f"\n    [lines {chunk.start_line}-{chunk.end_line}]\n{chunk.text}\n"
    return section


@dataclass(frozen=True)
class PackedPrompt:
    """The prompt for one model attempt, and how its context was sized."""

    prompt: str | None  # None when the attempt is skipped
    budget: int
    estimated_prompt: int
    chunks_kept: int
    chunks_dropped: int

    @property
    def skipped(self) -> bool:
        return self.prompt is None


def pack_prompt(
    spec: ModelSpec,
    diff_text: str,
    context_chunks: list[ContextChunk] | None,
    paths: list[str] | None,
) -> PackedPrompt:
    """Build the prompt for one attempt within that provider's budget (spec FR-023–FR-026).

    Located (version 2) context is packed best-first: a chunk is kept only if the whole
    context section still fits `min(CONTEXT_TOKEN_CAP, budget − instructions − diff)`, and a
    chunk that doesn't fit is skipped whole, never truncated. When the instructions and diff
    alone exceed the budget, the attempt is skipped (`prompt` is None) — the diff is never
    trimmed. Version 1 context is passed through untouched (pre-002 behaviour).
    """
    chunks = list(context_chunks or [])
    budget = prompt_budget(spec)
    base = build_prompt(diff_text, None, paths)
    base_tokens = estimate_tokens(base)

    if not _is_located(chunks):
        prompt = build_prompt(diff_text, chunks, paths)
        return PackedPrompt(prompt, budget, estimate_tokens(prompt), len(chunks), 0)
    if base_tokens > budget:
        return PackedPrompt(None, budget, base_tokens, 0, len(chunks))

    room = min(CONTEXT_TOKEN_CAP, budget - base_tokens)
    kept: list[ContextChunk] = []
    for chunk in chunks:
        if estimate_tokens(context_section(kept + [chunk])) <= room:
            kept.append(chunk)
    prompt = base + context_section(kept)
    return PackedPrompt(
        prompt, budget, estimate_tokens(prompt), len(kept), len(chunks) - len(kept)
    )


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


def _parse_comment(raw_comment) -> ReviewCommentDraft:
    """Build one ReviewCommentDraft from the model's raw dict. Raises on any missing/invalid
    field (KeyError, ValueError, or pydantic's ValidationError for a bad `category`/
    `severity` value) — the caller catches this per comment, not for the whole response."""
    return ReviewCommentDraft(
        path=raw_comment["path"],
        line=int(raw_comment["line"]),
        body=raw_comment["body"],
        category=raw_comment["category"],
        severity=raw_comment["severity"],
    )


def parse_review_response(raw_text: str, pr_id: str, model_used: str) -> GeneratedReview:
    """Parse a model's response text against the structured-JSON contract `build_prompt`
    instructs.

    Defensive at two levels, for two different kinds of failure:

    - **Whole-response fallback** (`parse_fallback: true`): the raw text isn't valid JSON, or
      isn't an object, or `summary` is missing/empty, or `comments` isn't a list. None of
      that can be salvaged, so the whole raw response becomes `summary` and `comments` is
      forced empty — degrading to the pre-structured-comments behavior rather than raising
      and losing the review entirely (FR-008 still governs a genuinely *empty* response;
      the clients reject that before this function ever runs).
    - **Per-comment discard** (`parse_fallback` stays `false`): once `summary` and the
      `comments` list shape are confirmed valid, each entry is parsed independently. A
      comment missing `category`/`severity`, or with an invalid value for either, is dropped
      — it does not invalidate `summary` or any other, otherwise-valid comment. How many were
      dropped, and why, is logged so a model that does this often is observable.

    Either way, a warning is logged so a persistently malformed model output is observable
    rather than silently swallowed.
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

    comments: list[ReviewCommentDraft] = []
    discarded: list[str] = []
    for raw_comment in raw_comments:
        try:
            comments.append(_parse_comment(raw_comment))
        except Exception as exc:
            discarded.append(f"{raw_comment!r}: {exc}")
    if discarded:
        logger.warning(
            "%s response for PR %s: discarded %d of %d comment(s) missing/invalid "
            "category or severity, keeping the %d valid one(s): %s",
            model_used,
            pr_id,
            len(discarded),
            len(raw_comments),
            len(comments),
            "; ".join(discarded),
        )

    return GeneratedReview(
        pr_id=pr_id,
        summary=summary,
        comments=comments,
        model_used=model_used,
        parse_fallback=False,
    )


def _log_attempt(pr_id: str, spec: ModelSpec, packed: PackedPrompt) -> None:
    """One machine-readable line per model attempt (specs/002-method-chunking/contracts/
    retrieve-context-v2.md): what the prompt cost against that provider's budget."""
    logger.info(
        json.dumps(
            {
                "event": "llm_attempt",
                "pr": int(pr_id) if pr_id.isdigit() else pr_id,
                "model": spec.label,
                "budget": packed.budget,
                "estimated_prompt": packed.estimated_prompt,
                "context_chunks_kept": packed.chunks_kept,
                "context_chunks_dropped": packed.chunks_dropped,
                "skipped": packed.skipped,
            }
        )
    )


def _token_counts(usage: dict | None) -> dict:
    """Provider token usage under one set of names. `output_tokens` counts everything the
    model generated, reasoning included: OpenAI-compatible `completion_tokens` already does;
    Gemini reports the answer (`candidatesTokenCount`) and its thoughts separately."""
    usage = usage or {}
    if "promptTokenCount" in usage or "candidatesTokenCount" in usage:
        answer = usage.get("candidatesTokenCount")
        thoughts = usage.get("thoughtsTokenCount")
        output = None if answer is None and thoughts is None else (answer or 0) + (thoughts or 0)
        return {
            "prompt_tokens": usage.get("promptTokenCount"),
            "output_tokens": output,
            "reasoning_tokens": thoughts,
        }
    return {
        "prompt_tokens": usage.get("prompt_tokens"),
        "output_tokens": usage.get("completion_tokens"),
        "reasoning_tokens": (usage.get("completion_tokens_details") or {}).get("reasoning_tokens"),
    }


def _call_outcome(exc: LlmRouterError) -> str:
    """`truncated` (output limit reached, or an empty answer), `model_not_found`, `transient`,
    or `permanent` (400/401/403 and every other non-retryable failure)."""
    if isinstance(exc, LlmOutputTruncatedError) or exc.empty_response:
        return "truncated"
    if isinstance(exc, LlmModelNotFoundError):
        return "model_not_found"
    if isinstance(exc, LlmTransientError):
        return "transient"
    return "permanent"


def _log_call(
    pr_id: str,
    complexity: Complexity,
    spec: ModelSpec,
    attempt: int,
    elapsed_ms: int,
    answer: str | None = None,
    error: LlmRouterError | None = None,
) -> None:
    """One machine-readable line per call that reached a provider, success or not. Only
    identifiers, counts and timings: never the prompt, the diff, the answer, an error message
    (provider error bodies can echo the prompt) or a key."""
    record = {
        "event": "llm_call",
        "pr": int(pr_id) if pr_id.isdigit() else pr_id,
        "tier": complexity.value,
        "model": spec.label,
        "attempt": attempt,
        "fell_back": attempt > 0,
        "outcome": "ok" if error is None else _call_outcome(error),
        "http_status": None if error is None else error.http_status,
        "llm_ms": elapsed_ms,
        "finish_reason": getattr(answer, "finish_reason", None),
        **_token_counts(getattr(answer, "usage", None)),
    }
    logger.info(json.dumps(record))


class LlmRouter(ABC):
    @abstractmethod
    def generate_review(
        self,
        pr_id: str,
        diff_text: str,
        complexity: Complexity,
        context_chunks: list[ContextChunk] | None = None,
        paths: list[str] | None = None,
    ) -> GeneratedReview:
        """Generate a review for `diff_text` (plus optional retrieved `context_chunks`).
        `paths` is the event's full changed-file list, used only to gate the security
        checklist in build_prompt — it does not affect which model is selected."""


class MultiProviderLlmRouter(LlmRouter):
    """Tries a tier's models in order, across providers.

    - LlmTransientError (429/5xx/timeout, or LlmOutputTruncatedError: output limit reached
      with no answer) or LlmModelNotFoundError (model gone): log it and try the next model.
    - Any other LlmRouterError (400/401/403, a blocked or otherwise empty response): stop
      immediately — a bad request or key won't be fixed by switching models.
    - Before each attempt, if the Lambda's remaining time is under that attempt's budget
      (_attempt_budget_ms — larger for Gemini's "high" reasoning, ATTEMPT_TIME_BUDGET_MS
      otherwise), stop with LlmTransientError instead of starting an attempt Lambda would
      cut off.
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
        paths: list[str] | None = None,
    ) -> GeneratedReview:
        specs = models_for_complexity(complexity)
        failures: list[str] = []
        skipped: list[str] = []
        every_failure_was_model_gone = True

        for attempt, spec in enumerate(specs):
            packed = pack_prompt(spec, diff_text, context_chunks, paths)
            _log_attempt(pr_id, spec, packed)
            if packed.skipped:
                reason = (
                    f"skipped, estimated prompt {packed.estimated_prompt} > budget "
                    f"{packed.budget} without context"
                )
                skipped.append(spec.label)
                failures.append(f"{spec.label}: {reason}")
                logger.warning(
                    "Skipping %s for PR %s: %s; %s",
                    spec.label,
                    pr_id,
                    reason,
                    "trying next model" if attempt + 1 < len(specs) else "no models left",
                )
                continue
            prompt = packed.prompt
            budget_ms = _attempt_budget_ms(spec)
            if self._remaining_time_ms is not None and self._remaining_time_ms() < budget_ms:
                failed = "; ".join(failures) or "none"
                not_attempted = ", ".join(s.label for s in specs[attempt:])
                raise LlmTransientError(
                    f"Stopped before {spec.label}: less than {budget_ms // 1000}s "
                    f"of Lambda time left for another attempt. Failed: {failed}. "
                    f"Not attempted: {not_attempted}"
                )
            client = self._client(spec.provider)
            started = time.monotonic()
            try:
                text = client.generate(spec.model, prompt, spec.reasoning)
            except LlmRouterError as exc:
                elapsed_ms = round((time.monotonic() - started) * 1000)
                _log_call(pr_id, complexity, spec, attempt, elapsed_ms, error=exc)
                if not isinstance(exc, (LlmTransientError, LlmModelNotFoundError)):
                    raise
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
            elapsed_ms = round((time.monotonic() - started) * 1000)
            _log_call(pr_id, complexity, spec, attempt, elapsed_ms, answer=text)
            review = parse_review_response(text, pr_id=pr_id, model_used=spec.label)
            return review.model_copy(update={"fell_back": attempt > 0})

        summary = f"All {len(specs)} model(s) for tier {complexity.value!r} failed: " + "; ".join(
            failures
        )
        if len(skipped) == len(specs):
            raise LlmPromptTooLargeError(summary)
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
        paths: list[str] | None = None,
    ) -> GeneratedReview:
        model = STUB_MODEL_BY_COMPLEXITY[complexity]
        self.calls.append(
            {
                "pr_id": pr_id,
                "diff_text": diff_text,
                "complexity": complexity,
                "context_chunks": context_chunks,
                "paths": paths,
                "prompt": build_prompt(diff_text, context_chunks, paths),
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
