"""Shared pytest fixtures: every Lambda handler is exercised against local stubs by default,
so the suite makes zero real network calls (SC-003, FR-011).
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from integrations.decision_engine import StubDecisionEngine  # noqa: E402
from integrations.github import StubGitHubClient  # noqa: E402
from integrations.llm_router import StubLlmRouter  # noqa: E402
from integrations.storage import StubStorage  # noqa: E402


@pytest.fixture
def small_diff() -> str:
    """A ~10-line-changed fixture diff touching one file (low complexity, spec.md thresholds)."""
    return (
        "diff --git a/src/app.py b/src/app.py\n"
        "index 111..222 100644\n"
        "--- a/src/app.py\n"
        "+++ b/src/app.py\n"
        "@@ -1,3 +1,5 @@\n"
        "+import logging\n"
        "+\n"
        " def main():\n"
        "-    pass\n"
        "+    logging.info('starting')\n"
    )


@pytest.fixture
def large_diff() -> str:
    """A 450-line-changed fixture diff (high complexity, per spec.md thresholds)."""
    body = "\n".join(f"+line {i}" for i in range(450))
    return (
        "diff --git a/src/big_module.py b/src/big_module.py\n"
        "index 111..222 100644\n"
        "--- a/src/big_module.py\n"
        "+++ b/src/big_module.py\n"
        f"@@ -1,0 +1,450 @@\n{body}\n"
    )


@pytest.fixture
def pr_event() -> dict:
    return {
        "pr_id": "42",
        "repository": "octocat/example",
        "revision": "abc123",
        "diff_ref": "diffs/42-abc123.diff",
    }


@pytest.fixture
def stub_storage(pr_event, small_diff) -> StubStorage:
    return StubStorage(initial={pr_event["diff_ref"]: small_diff})


@pytest.fixture
def stub_decision_engine() -> StubDecisionEngine:
    return StubDecisionEngine()


@pytest.fixture
def stub_llm_router() -> StubLlmRouter:
    return StubLlmRouter()


@pytest.fixture
def stub_github_client() -> StubGitHubClient:
    return StubGitHubClient()
