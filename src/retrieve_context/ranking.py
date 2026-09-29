"""Ranks a version 2 index's chunks against the diff's per-file queries.

Specs/002-method-chunking, FR-018 to FR-022:

- a chunk's score is its **best** cosine similarity against any query (max-fusion): one
  strongly matching changed file must not be averaged away by the others (research R7);
- chunks whose lines overlap lines the diff changes are **excluded** before ranking — the
  diff already shows that code. Only the overlapping chunks go, never the whole file:
  unchanged methods of a changed class are often the most useful context;
- the top `TOP_N` are returned, and every selection is **logged** as one JSON line, so the
  scores are observable without recomputing them (baseline.md had to).
"""

import json
import logging
import math
import statistics
from dataclasses import dataclass

from retrieve_context.diff_queries import DiffQuery

logger = logging.getLogger(__name__)

# 8 chunks of at most ~680 tokens each (the largest measured method) is at most ~5,400
# tokens before InvokeLLM's per-provider budget trims it; see spec FR-018.
TOP_N = 8


def cosine_similarity(vector_a: list[float], vector_b: list[float]) -> float:
    """Plain-Python cosine similarity — no numpy: at a few hundred 768-float vectors per run
    this is tens of milliseconds, and skipping numpy keeps the other three Lambdas from
    carrying its ~57 MB of vendored OpenBLAS in the shared deployment zip for no benefit.

    A zero-norm vector scores 0 rather than raising a ZeroDivisionError — such a vector is
    degenerate and should simply never rank, not blow up the whole retrieval.
    """
    dot_product = sum(a * b for a, b in zip(vector_a, vector_b, strict=True))
    norm_a = math.sqrt(sum(a * a for a in vector_a))
    norm_b = math.sqrt(sum(b * b for b in vector_b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot_product / (norm_a * norm_b)


@dataclass(frozen=True)
class RankedChunk:
    chunk: dict
    score: float
    matched_query: str


@dataclass(frozen=True)
class Ranking:
    selected: list[RankedChunk]
    # Every chunk that was scored (i.e. not excluded), best first — selected ones included.
    candidates: list[RankedChunk]
    excluded_overlapping: int


def overlaps_changed_lines(chunk: dict, changed: dict[str, set[int]]) -> bool:
    lines = changed.get(chunk.get("path"))
    start, end = chunk.get("startLine"), chunk.get("endLine")
    if not lines or start is None or end is None:
        return False
    return any(start <= line <= end for line in lines)


def rank(
    chunks: list[dict],
    queries: list[DiffQuery],
    query_vectors: list[list[float]],
    changed: dict[str, set[int]],
    top_n: int = TOP_N,
) -> Ranking:
    kept = [chunk for chunk in chunks if not overlaps_changed_lines(chunk, changed)]
    candidates = []
    for chunk in kept:
        scores = [cosine_similarity(chunk["vector"], vector) for vector in query_vectors]
        best = max(range(len(scores)), key=scores.__getitem__)
        candidates.append(RankedChunk(chunk, scores[best], queries[best].path))
    # sort() is stable, so equal scores keep the index's own order: deterministic output.
    candidates.sort(key=lambda ranked: ranked.score, reverse=True)
    return Ranking(
        selected=candidates[:top_n],
        candidates=candidates,
        excluded_overlapping=len(chunks) - len(kept),
    )


def score_distribution(ranking: Ranking) -> dict:
    """Spread of all candidate scores, and how far the selected ones stand above the rest.
    The standardised gap — (mean selected − mean rest) / σ(all) — is comparable across runs
    with different N and candidate counts; baseline.md recorded 1.78 / 1.93 / 1.78 for whole
    files (PRs #3 / #7 / #8)."""
    scores = [c.score for c in ranking.candidates]
    if not scores:
        return {}
    top = scores[: len(ranking.selected)]
    rest = scores[len(ranking.selected) :]
    stats = {
        "min": round(min(scores), 4),
        "max": round(max(scores), 4),
        "mean_selected": round(statistics.fmean(top), 4),
        "min_selected": round(min(top), 4),
    }
    if rest:
        spread = statistics.pstdev(scores)
        gap = statistics.fmean(top) - statistics.fmean(rest)
        stats |= {
            "mean_rest": round(statistics.fmean(rest), 4),
            "max_rest": round(max(rest), 4),
            "margin_at_cut": round(min(top) - max(rest), 4),
            "standardised_gap": round(gap / spread, 2) if spread else None,
        }
    return stats


def log_ranking(
    pr_number: int,
    index: dict,
    queries: list[DiffQuery],
    ranking: Ranking,
) -> None:
    """One `rag_query` line per run and one `rag_chunk` line per selected chunk
    (specs/002-method-chunking/contracts/retrieve-context-v2.md)."""
    logger.info(
        json.dumps(
            {
                "event": "rag_query",
                "pr": pr_number,
                "index_version": index.get("version"),
                "index_commit": index.get("commit"),
                "queries": len(queries),
                "query_parts_split": sum(1 for q in queries if q.part == 1),
                "excluded_overlapping": ranking.excluded_overlapping,
                "candidates": len(ranking.candidates),
                "selected": len(ranking.selected),
                "scores": score_distribution(ranking),
            }
        )
    )
    for rank_position, ranked in enumerate(ranking.selected, start=1):
        logger.info(
            json.dumps(
                {
                    "event": "rag_chunk",
                    "pr": pr_number,
                    "rank": rank_position,
                    "id": ranked.chunk.get("id"),
                    "score": round(ranked.score, 4),
                    "matched_query": ranked.matched_query,
                }
            )
        )
