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


FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture
def pr_small_diff() -> str:
    """A one-file diff inserting a log line inside AuthController.login (old lines 16/17)."""
    return (FIXTURES / "pr_small.diff").read_text(encoding="utf-8")


@pytest.fixture
def pr_small_event() -> dict:
    return json.loads((FIXTURES / "pr_small_event.json").read_text(encoding="utf-8"))


def _v2_chunk(path, kind, symbol, start, end, header, text, vec, part=None):
    method = f".{symbol[1]}" if symbol and symbol[1] else ""
    chunk_id = (
        f"{path}#L{start}-{end}"
        if kind == "block"
        else f"{path}#{symbol[0]}{method}:{start}-{end}"
    )
    if part:
        chunk_id += f"#part-{part[0]}"
    return {
        "id": chunk_id,
        "path": path,
        "kind": kind,
        "symbol": {"type": symbol[0], "method": symbol[1]} if symbol else None,
        "startLine": start,
        "endLine": end,
        "part": {"index": part[0], "count": part[1]} if part else None,
        "header": header,
        "text": text,
        "vector": vec,
    }


@pytest.fixture
def rag_index_v2() -> dict:
    """A version 2 index (specs/002-method-chunking/contracts/index-v2.md) for the same code
    as `pr_small_diff`. Against the stub's default query vector [1, 0, ...] the chunks rank:

        login (1.00, but overlaps the diff's changed lines -> excluded)
        > isValid (~0.995) > JwtValidator part 1 (0.80) > LoginResponse type (~0.71)
        > JwtValidator part 2 (~0.71, second on ties by index order) > TaskService (~0.10)
        > pom.xml block (0.0)
    """
    auth = "src/main/java/com/codereview/app/auth/"
    auth_header = "package com.codereview.app.auth;\n\n"
    return {
        "version": 2,
        "branch": "develop",
        "commit": "88801e485be2a743cf44006830e2740e1345cf76",
        "generatedAt": "2026-09-28T12:00:00Z",
        "model": EMBEDDING_MODEL,
        "dimensions": EMBEDDING_DIMENSIONS,
        "chunks": [
            _v2_chunk(
                "pom.xml", "block", None, 1, 12, "", "<project>...</project>", vector(0.0, 1.0)
            ),
            _v2_chunk(
                f"{auth}AuthController.java", "method", ("AuthController", "login"), 14, 20,
                auth_header + "@RestController\npublic class AuthController {\n"
                "    private final JwtValidator jwtValidator;",
                "public ResponseEntity<LoginResponse> login(LoginRequest request) { ... }",
                vector(1.0),
            ),
            _v2_chunk(
                f"{auth}InMemoryUsers.java", "method", ("InMemoryUsers", "isValid"), 10, 12,
                auth_header + "final class InMemoryUsers {\n"
                "    private static final Map<String, String> CREDENTIALS = Map.of();",
                "static boolean isValid(String username, String password) { ... }",
                vector(0.99, 0.1),
            ),
            _v2_chunk(
                f"{auth}JwtValidator.java", "method", ("JwtValidator", "isValid"), 40, 60,
                auth_header + "public class JwtValidator {",
                "public boolean isValid(String token) { // part 1",
                vector(0.8, 0.6), part=(1, 2),
            ),
            _v2_chunk(
                f"{auth}LoginResponse.java", "type", ("LoginResponse", None), 3, 4,
                auth_header + "public record LoginResponse(String token) {",
                "public record LoginResponse(String token) {\n}",
                vector(0.7, 0.7),
            ),
            _v2_chunk(
                f"{auth}JwtValidator.java", "method", ("JwtValidator", "isValid"), 61, 75,
                auth_header + "public class JwtValidator {",
                "    // part 2 }",
                vector(0.7, 0.7), part=(2, 2),
            ),
            _v2_chunk(
                "src/main/java/com/codereview/app/tasks/TaskService.java", "method",
                ("TaskService", "findAll"), 8, 10,
                "package com.codereview.app.tasks;\n\n@Service\npublic class TaskService {",
                "public List<Task> findAll() { ... }",
                vector(0.1, 0.99),
            ),
        ],
    }
