"""Prompt layout for retrieved context (specs/002-method-chunking FR-027).

`golden_v1_prompt.txt` was produced by the pre-002 `build_prompt` (commit 8e0ee47's tree)
from `pr_small.diff` and the golden v1 context, so a version 1 context must still yield
exactly that prompt.
"""

import json
from pathlib import Path

from contracts.models import ContextChunk
from integrations.llm_router import build_prompt

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
HEADER_A = "package a;\n\npublic class A {\n    private int count;"


def _located(path, start, end, text, header=HEADER_A, score=0.8):
    return ContextChunk(
        path=path, text=text, id=f"{path}#{start}", start_line=start, end_line=end,
        header=header, score=score,
    )


def test_v1_context_prompt_is_byte_for_byte_the_pre_002_prompt(pr_small_event, pr_small_diff):
    golden_context = json.loads((FIXTURES / "golden_v1_context.json").read_text("utf-8"))
    chunks = [ContextChunk.model_validate(c) for c in golden_context["chunks"]]

    prompt = build_prompt(pr_small_diff, chunks, pr_small_event["paths"])

    assert prompt.encode("utf-8") == (FIXTURES / "golden_v1_prompt.txt").read_bytes()


def test_v2_chunks_are_grouped_by_file_with_the_header_printed_once():
    chunks = [
        _located("src/A.java", 30, 35, "void late() {}", score=0.9),
        _located("src/B.java", 5, 9, "void b() {}", header="package b;\n\nclass B {", score=0.85),
        _located("src/A.java", 10, 14, "void early() {}", score=0.8),
    ]

    prompt = build_prompt("+diff", chunks)
    context = prompt.split("=== ADDITIONAL PROJECT CONTEXT ===\n", 1)[1]

    # Files in order of their best chunk (A: 0.9, then B), each file's chunks in line order.
    assert context.index("--- src/A.java ---") < context.index("--- src/B.java ---")
    assert context.index("[lines 10-14]") < context.index("[lines 30-35]")
    assert context.count(HEADER_A) == 1
    assert "It is NOT part of the change under review" in context


def test_a_different_header_within_the_same_file_is_printed_again():
    inner = "package a;\n\npublic class A {\n    static class Inner {"
    chunks = [
        _located("src/A.java", 10, 14, "void outer() {}"),
        _located("src/A.java", 20, 24, "void inner() {}", header=inner),
    ]

    context = build_prompt("+diff", chunks).split("=== ADDITIONAL PROJECT CONTEXT ===\n", 1)[1]

    assert context.count(HEADER_A) == 1 and context.count(inner) == 1


def test_markers_the_measurement_script_splits_on_are_unchanged():
    prompt = build_prompt("+diff", [_located("src/A.java", 1, 2, "x")])
    assert "=== DIFF UNDER REVIEW ===\n" in prompt
    assert "\n=== ADDITIONAL PROJECT CONTEXT ===\n" in prompt


def test_no_context_means_no_context_section():
    assert "ADDITIONAL PROJECT CONTEXT" not in build_prompt("+diff", [])
