"""Shared pytest fixtures: every Lambda handler is exercised against local stubs by default,
so the suite makes zero real network calls (SC-003, FR-011).
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from integrations.decision_engine import StubDecisionEngine  # noqa: E402
from integrations.embeddings import (  # noqa: E402
    EMBEDDING_DIMENSIONS,
    EMBEDDING_MODEL,
    StubEmbeddingClient,
)
from integrations.github import StubGitHubClient  # noqa: E402
from integrations.llm_router import StubLlmRouter  # noqa: E402
from integrations.storage import StubStorage  # noqa: E402
from retrieve_context.handler import INDEX_KEY  # noqa: E402


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
    """The real event shape published by codereview-app — camelCase on the wire, matching
    PullRequestEvent's alias_generator (contracts/models.py). Every handler (RouteModel,
    RetrieveContext, InvokeLLM) now validates against this same shape.
    """
    return {
        "prNumber": 42,
        "repository": "octocat/example",
        "sha": "abc123",
        "diffBucket": "codereview-artifacts",
        "diffKey": "diffs/42-abc123.diff",
        "filesChanged": 1,
        "linesAdded": 8,
        "linesRemoved": 2,
        "paths": ["src/app.py"],
    }


def vector(*leading: float) -> list[float]:
    """A full-length embedding vector whose first values are `leading`, rest zeros."""
    return list(leading) + [0.0] * (EMBEDDING_DIMENSIONS - len(leading))


@pytest.fixture
def query_vector() -> list[float]:
    """What the stub embedding client returns for the diff — the retrieval query side."""
    return vector(1.0)


@pytest.fixture
def rag_index() -> dict:
    """An index.json matching codereview-app's build_index.py contract.

    Chunk vectors are ordered by cosine similarity against `query_vector` ([1, 0, 0, ...]):
    Alpha (1.0) > Beta (~0.99) > Gamma (~0.71) > Delta (0.0), so the expected top-3 is
    Alpha/Beta/Gamma and Delta must be left out.
    """
    return {
        "version": 1,
        "branch": "develop",
        "commit": "f00ba7",
        "generatedAt": "2026-09-23T12:00:00Z",
        "model": EMBEDDING_MODEL,
        "dimensions": EMBEDDING_DIMENSIONS,
        "chunks": [
            {"path": "src/Delta.java", "text": "class Delta {}", "vector": vector(0.0, 1.0)},
            {"path": "src/Alpha.java", "text": "class Alpha {}", "vector": vector(1.0)},
            {"path": "src/Gamma.java", "text": "class Gamma {}", "vector": vector(0.5, 0.5)},
            {"path": "src/Beta.java", "text": "class Beta {}", "vector": vector(0.9, 0.1)},
        ],
    }


@pytest.fixture
def stub_storage(pr_event, small_diff, rag_index) -> StubStorage:
    return StubStorage(
        initial={
            pr_event["diffKey"]: small_diff,
            INDEX_KEY: json.dumps(rag_index),
        }
    )


@pytest.fixture
def stub_embedding_client(query_vector) -> StubEmbeddingClient:
    return StubEmbeddingClient(vector=query_vector)


@pytest.fixture
def stub_decision_engine() -> StubDecisionEngine:
    return StubDecisionEngine()


@pytest.fixture
def stub_llm_router() -> StubLlmRouter:
    return StubLlmRouter()


@pytest.fixture
def stub_github_client() -> StubGitHubClient:
    return StubGitHubClient()
