"""Per-attempt context budgets and the over-budget skip (specs/002-method-chunking
FR-023–FR-026, FR-028; research R9, R10)."""

import json
import logging

import pytest

from contracts.models import Complexity, ContextChunk
from contracts.token_estimate import estimate_tokens
from integrations.llm_router import (
    CONTEXT_TOKEN_CAP,
    LlmModelNotFoundError,
    LlmPromptTooLargeError,
    LlmTransientError,
    ModelClient,
    ModelSpec,
    MultiProviderLlmRouter,
    build_prompt,
    pack_prompt,
    prompt_budget,
)

REVIEW_JSON = json.dumps({"summary": "Looks fine.", "comments": []})
GROQ_MEDIUM = ModelSpec("groq", "openai/gpt-oss-120b", "medium")
CEREBRAS_MEDIUM = ModelSpec("cerebras", "gpt-oss-120b", "medium")


class RecordingClient(ModelClient):
    """Records every prompt it is sent; answers or raises per model."""

    def __init__(self, outcome: str | Exception = REVIEW_JSON):
        self._outcome = outcome
        self.prompts: list[str] = []

    def generate(self, model, prompt, reasoning=None):
        self.prompts.append(prompt)
        if isinstance(self._outcome, Exception):
            raise self._outcome
        return self._outcome


def _chunks(count: int, chars: int = 600) -> list[ContextChunk]:
    return [
        ContextChunk(
            path=f"src/F{i}.java",
            text=f"void m{i}() {{ {'y' * chars} }}",
            id=f"src/F{i}.java#F{i}.m{i}():1-3",
            start_line=1,
            end_line=3,
            header=f"package p;\n\nclass F{i} {{",
            score=1.0 - i / 100,
        )
        for i in range(count)
    ]


def _diff_leaving_room(spec: ModelSpec, room_tokens: int) -> str:
    """A diff that leaves about `room_tokens` of `spec`'s budget after instructions + diff."""
    instructions = estimate_tokens(build_prompt("", None, None))
    target = prompt_budget(spec) - room_tokens - instructions
    return "+" + "x" * (target * 3 - 10)


@pytest.fixture
def medium_tier(monkeypatch):
    monkeypatch.setenv(
        "LLM_MODELS_MEDIUM", "groq:openai/gpt-oss-120b:medium,cerebras:gpt-oss-120b:medium"
    )


def _router(groq: ModelClient, cerebras: ModelClient) -> MultiProviderLlmRouter:
    return MultiProviderLlmRouter(
        client_factories={"groq": lambda: groq, "cerebras": lambda: cerebras}
    )


# --- pack_prompt -------------------------------------------------------------------------


def test_groq_gets_fewer_chunks_than_cerebras_for_the_same_review():
    diff = _diff_leaving_room(GROQ_MEDIUM, room_tokens=600)
    chunks = _chunks(8)

    on_groq = pack_prompt(GROQ_MEDIUM, diff, chunks, None)
    on_cerebras = pack_prompt(CEREBRAS_MEDIUM, diff, chunks, None)

    assert 0 < on_groq.chunks_kept < on_cerebras.chunks_kept
    assert on_groq.estimated_prompt <= on_groq.budget
    assert on_cerebras.estimated_prompt <= on_cerebras.budget


def test_context_never_exceeds_the_cap_even_with_room_to_spare():
    packed = pack_prompt(CEREBRAS_MEDIUM, "+x", _chunks(40), None)
    context_tokens = packed.estimated_prompt - estimate_tokens(build_prompt("+x", None, None))

    assert packed.chunks_dropped > 0
    assert context_tokens <= CONTEXT_TOKEN_CAP


def test_packing_is_best_first_and_skips_whole_chunks_that_do_not_fit():
    big, small = _chunks(1, chars=3000)[0], _chunks(2, chars=60)[1]
    diff = _diff_leaving_room(GROQ_MEDIUM, room_tokens=400)

    packed = pack_prompt(GROQ_MEDIUM, diff, [big, small], None)

    # The big one (ranked first) does not fit and is skipped whole; the small one still does.
    assert packed.chunks_kept == 1 and packed.chunks_dropped == 1
    assert small.text in packed.prompt and big.text not in packed.prompt


def test_a_diff_over_the_budget_on_its_own_skips_the_attempt():
    diff = _diff_leaving_room(GROQ_MEDIUM, room_tokens=-100)

    packed = pack_prompt(GROQ_MEDIUM, diff, _chunks(3), None)

    assert packed.skipped
    assert packed.estimated_prompt > packed.budget


def test_whole_file_v1_context_is_passed_through_unchanged():
    """Version 1 context keeps the pre-002 prompt exactly, even over the budget: deploying
    this Lambda before the index switches to v2 must change nothing (FR-029)."""
    whole_files = [ContextChunk(path="src/A.java", text="class A {" + "z" * 30_000 + "}")]

    packed = pack_prompt(GROQ_MEDIUM, "+x", whole_files, None)

    assert packed.prompt == build_prompt("+x", whole_files, None)
    assert packed.chunks_kept == 1 and not packed.skipped


# --- router ------------------------------------------------------------------------------


def test_router_skips_groq_without_calling_it_and_cerebras_answers(medium_tier, caplog):
    groq, cerebras = RecordingClient(), RecordingClient()
    diff = _diff_leaving_room(GROQ_MEDIUM, room_tokens=-100)

    with caplog.at_level(logging.INFO, logger="integrations.llm_router"):
        review = _router(groq, cerebras).generate_review(
            pr_id="8", diff_text=diff, complexity=Complexity.MEDIUM, context_chunks=_chunks(2)
        )

    assert groq.prompts == []
    assert len(cerebras.prompts) == 1
    assert review.model_used == "cerebras:gpt-oss-120b:medium" and review.fell_back is True
    messages = [r.getMessage() for r in caplog.records]
    assert any("Skipping groq:openai/gpt-oss-120b:medium for PR 8" in m for m in messages)
    attempts = [json.loads(m) for m in messages if m.startswith('{"event": "llm_attempt"')]
    assert [(a["model"], a["skipped"]) for a in attempts] == [
        ("groq:openai/gpt-oss-120b:medium", True),
        ("cerebras:gpt-oss-120b:medium", False),
    ]
    assert attempts[1]["context_chunks_kept"] == 2 and attempts[1]["pr"] == 8


def test_every_model_skipped_is_a_non_retryable_prompt_too_large_error(medium_tier):
    groq, cerebras = RecordingClient(), RecordingClient()
    diff = _diff_leaving_room(CEREBRAS_MEDIUM, room_tokens=-100)

    with pytest.raises(LlmPromptTooLargeError) as exc_info:
        _router(groq, cerebras).generate_review(
            pr_id="8", diff_text=diff, complexity=Complexity.MEDIUM, context_chunks=_chunks(1)
        )

    assert not isinstance(exc_info.value, LlmTransientError)
    assert "budget 4300" in str(exc_info.value) and "budget 18000" in str(exc_info.value)
    assert groq.prompts == [] and cerebras.prompts == []


def test_a_skip_plus_a_transient_failure_stays_retryable(medium_tier):
    groq = RecordingClient()
    cerebras = RecordingClient(LlmTransientError("HTTP 429"))
    diff = _diff_leaving_room(GROQ_MEDIUM, room_tokens=-100)

    with pytest.raises(LlmTransientError):
        _router(groq, cerebras).generate_review(
            pr_id="8", diff_text=diff, complexity=Complexity.MEDIUM, context_chunks=_chunks(1)
        )


def test_a_skip_plus_a_missing_model_is_not_retryable(medium_tier):
    groq = RecordingClient()
    cerebras = RecordingClient(LlmModelNotFoundError("404"))
    diff = _diff_leaving_room(GROQ_MEDIUM, room_tokens=-100)

    with pytest.raises(LlmModelNotFoundError):
        _router(groq, cerebras).generate_review(
            pr_id="8", diff_text=diff, complexity=Complexity.MEDIUM, context_chunks=_chunks(1)
        )


def test_each_attempt_gets_its_own_packed_prompt(medium_tier):
    groq = RecordingClient(LlmTransientError("HTTP 503"))
    cerebras = RecordingClient()
    diff = _diff_leaving_room(GROQ_MEDIUM, room_tokens=600)

    _router(groq, cerebras).generate_review(
        pr_id="7", diff_text=diff, complexity=Complexity.MEDIUM, context_chunks=_chunks(8)
    )

    [groq_prompt], [cerebras_prompt] = groq.prompts, cerebras.prompts
    assert groq_prompt.count("[lines 1-3]") < cerebras_prompt.count("[lines 1-3]")
