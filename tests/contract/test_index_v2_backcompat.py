"""A reversed deploy order must not break reviews (specs/002-method-chunking, FR-029).

If codereview-app starts publishing version 2 before this Lambda is updated, the *old*
RetrieveContext reads it. The old code never looked at `version`: it ranks
`chunks[].vector` and returns `chunks[].path`/`text`. Index v2 keeps those three fields with
the same meaning (contracts/index-v2.md, rule 1), so the old ranking still works, degraded
to method bodies without headers. The v1 ranking function kept in the handler *is* that old
code, unchanged, so running it on a v2 index is the reversed-order scenario.
"""

from contracts.models import ContextChunk
from retrieve_context.handler import _top_chunks

from ..conftest import vector


def test_old_v1_ranking_reads_a_v2_index_without_error(rag_index_v2):
    top = _top_chunks(rag_index_v2["chunks"], vector(1.0), 3)

    assert all(isinstance(chunk, ContextChunk) for chunk in top)
    assert [chunk.path.rsplit("/", 1)[-1] for chunk in top] == [
        "AuthController.java",
        "InMemoryUsers.java",
        "JwtValidator.java",
    ]
    # Only the v1 fields are carried: the old InvokeLLM would print these as flat context.
    assert top[0].model_dump(exclude_none=True).keys() == {"path", "text"}


def test_every_v2_chunk_keeps_the_v1_field_names(rag_index_v2):
    for chunk in rag_index_v2["chunks"]:
        assert chunk["path"] and chunk["text"]
        assert len(chunk["vector"]) == rag_index_v2["dimensions"]
