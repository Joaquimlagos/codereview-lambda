"""Ranking of v2 chunks against per-file queries (specs/002-method-chunking FR-018–FR-022)."""

import json
import logging

from retrieve_context.diff_queries import DiffQuery
from retrieve_context.ranking import (
    TOP_N,
    log_ranking,
    overlaps_changed_lines,
    rank,
    score_distribution,
)

from ..conftest import vector


def _chunk(chunk_id, path, start, end, vec):
    return {"id": chunk_id, "path": path, "startLine": start, "endLine": end, "vector": vec}


A_QUERY = DiffQuery(path="src/A.java", text="diff a")
B_QUERY = DiffQuery(path="src/B.java", text="diff b")


def test_score_is_the_best_match_over_all_queries_and_names_that_query():
    chunk = _chunk("c", "src/C.java", 1, 5, vector(0.0, 1.0))

    ranking = rank([chunk], [A_QUERY, B_QUERY], [vector(1.0), vector(0.0, 1.0)], changed={})

    [ranked] = ranking.selected
    assert ranked.score == 1.0
    assert ranked.matched_query == "src/B.java"


def test_only_chunks_overlapping_changed_lines_are_excluded_not_the_whole_file():
    changed = {"src/A.java": {12}}
    touched = _chunk("touched", "src/A.java", 10, 14, vector(1.0))
    same_file_untouched = _chunk("untouched", "src/A.java", 20, 30, vector(0.9, 0.1))
    other_file = _chunk("other", "src/X.java", 10, 14, vector(0.5, 0.5))

    ranking = rank(
        [touched, same_file_untouched, other_file], [A_QUERY], [vector(1.0)], changed
    )

    assert [r.chunk["id"] for r in ranking.selected] == ["untouched", "other"]
    assert ranking.excluded_overlapping == 1


def test_overlap_boundaries_are_inclusive():
    changed = {"src/A.java": {10, 20}}
    assert overlaps_changed_lines(_chunk("s", "src/A.java", 10, 15, []), changed)
    assert overlaps_changed_lines(_chunk("e", "src/A.java", 15, 20, []), changed)
    assert not overlaps_changed_lines(_chunk("n", "src/A.java", 11, 19, []), changed)


def test_top_n_is_eight_and_ties_keep_index_order():
    chunks = [_chunk(f"c{i}", "src/C.java", i * 10, i * 10 + 5, vector(1.0)) for i in range(12)]

    ranking = rank(chunks, [A_QUERY], [vector(1.0)], changed={})

    assert TOP_N == 8
    assert [r.chunk["id"] for r in ranking.selected] == [f"c{i}" for i in range(8)]
    assert len(ranking.candidates) == 12


def test_score_distribution_reports_the_gap_between_selected_and_rest():
    chunks = [
        _chunk("hi1", "p", 1, 1, vector(1.0)),
        _chunk("hi2", "p", 2, 2, vector(1.0)),
        _chunk("lo1", "p", 3, 3, vector(0.0, 1.0)),
        _chunk("lo2", "p", 4, 4, vector(0.0, 1.0)),
    ]

    stats = score_distribution(rank(chunks, [A_QUERY], [vector(1.0)], {}, top_n=2))

    assert stats["min"] == 0.0 and stats["max"] == 1.0
    assert stats["margin_at_cut"] == 1.0
    # mean gap 1.0, population σ of [1, 1, 0, 0] is 0.5
    assert stats["standardised_gap"] == 2.0


def test_log_lines_are_json_with_pr_scores_and_counts(caplog):
    chunks = [
        _chunk("src/A.java#A.run():10-14", "src/A.java", 10, 14, vector(1.0)),
        _chunk("src/A.java#A.other():20-30", "src/A.java", 20, 30, vector(0.6, 0.8)),
    ]
    index = {"version": 2, "commit": "88801e4", "chunks": chunks}
    ranking = rank(chunks, [A_QUERY], [vector(1.0)], {"src/A.java": {12}})

    with caplog.at_level(logging.INFO, logger="retrieve_context.ranking"):
        log_ranking(3, index, [A_QUERY], ranking)

    lines = [json.loads(record.getMessage()) for record in caplog.records]
    query_line, chunk_line = lines
    assert query_line["event"] == "rag_query"
    assert query_line["pr"] == 3
    assert query_line["index_commit"] == "88801e4"
    assert query_line["excluded_overlapping"] == 1
    assert query_line["candidates"] == 1 and query_line["selected"] == 1
    assert chunk_line == {
        "event": "rag_chunk",
        "pr": 3,
        "rank": 1,
        "id": "src/A.java#A.other():20-30",
        "score": 0.6,
        "matched_query": "src/A.java",
    }
