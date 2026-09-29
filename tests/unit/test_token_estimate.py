"""The shared token estimate must never under-count relative to the measured tokenizers."""

from pathlib import Path

from contracts.token_estimate import (
    CHARS_PER_TOKEN,
    EMBEDDING_SPLIT_THRESHOLD_TOKENS,
    estimate_tokens,
)

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


def test_empty_text_is_zero_tokens():
    assert estimate_tokens("") == 0


def test_rounds_up_so_a_single_character_counts():
    assert estimate_tokens("x") == 1
    assert estimate_tokens("abc") == 1
    assert estimate_tokens("abcd") == 2


def test_ratio_is_below_every_measured_tokenizer():
    """baseline.md measured ~3.4 chars/token (Gemini) and 4.12-4.36 (gpt-oss) on real diffs;
    an estimate at or above 3.4 would under-count for Gemini."""
    assert CHARS_PER_TOKEN < 3.4


def test_split_threshold_leaves_room_under_the_embedding_limit():
    # Worst case: the real tokenizer packs 3.4 chars per token.
    worst_real_tokens = EMBEDDING_SPLIT_THRESHOLD_TOKENS * CHARS_PER_TOKEN / 3.4
    assert worst_real_tokens < 2048


def test_real_diff_is_overestimated_against_its_measured_gpt_oss_count():
    diff = (FIXTURES / "pr_small.diff").read_text(encoding="utf-8")
    # At the lowest measured gpt-oss ratio (4.12), the real count is below the estimate.
    assert estimate_tokens(diff) > len(diff) / 4.12
