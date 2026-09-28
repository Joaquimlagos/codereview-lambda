"""Per-file diff queries and the changed-line set (specs/002-method-chunking FR-016, FR-020;
research R7, R8)."""

from contracts.token_estimate import EMBEDDING_SPLIT_THRESHOLD_TOKENS, estimate_tokens
from retrieve_context.diff_queries import changed_lines, split_queries


def _file(old: str, new: str, *hunks: str, extra_header: str = "") -> str:
    a = "/dev/null" if old is None else f"a/{old}"
    b = "/dev/null" if new is None else f"b/{new}"
    name_a, name_b = old or new, new or old
    return (
        f"diff --git a/{name_a} b/{name_b}\n{extra_header}index 111..222 100644\n"
        f"--- {a}\n+++ {b}\n" + "".join(hunks)
    )


MODIFY = _file(
    "src/A.java",
    "src/A.java",
    "@@ -10,6 +10,6 @@ class A {\n"
    "     int x;\n"
    "     void run() {\n"
    "-        old();\n"
    "+        replacement();\n"
    "     }\n"
    "     void other() {}\n",
)
INSERT = _file(
    "src/B.java",
    "src/B.java",
    "@@ -20,4 +20,6 @@\n"
    "     a();\n"
    "     b();\n"
    "+    inserted1();\n"
    "+    inserted2();\n"
    "     c();\n"
    "     d();\n",
)
ADD = _file(None, "src/New.java", "@@ -0,0 +1,2 @@\n+class New {\n+}\n")
DELETE = _file("src/Gone.java", None, "@@ -1,2 +0,0 @@\n-class Gone {\n-}\n")


# --- split_queries -----------------------------------------------------------------------


def test_one_query_per_changed_file_in_diff_order():
    queries = split_queries(MODIFY + INSERT + ADD + DELETE)

    assert [q.path for q in queries] == [
        "src/A.java",
        "src/B.java",
        "src/New.java",
        "src/Gone.java",
    ]
    assert all(q.part is None for q in queries)
    assert queries[0].text == MODIFY


def test_a_deleted_file_is_queried_by_its_old_path():
    [query] = split_queries(DELETE)
    assert query.path == "src/Gone.java"


def test_a_renamed_file_is_queried_by_its_new_path():
    rename = _file(
        "src/Old.java", "src/Renamed.java", "@@ -1,1 +1,1 @@\n-a\n+b\n",
        extra_header="similarity index 90%\nrename from src/Old.java\nrename to src/Renamed.java\n",
    )
    [query] = split_queries(rename)
    assert query.path == "src/Renamed.java"


def test_a_file_over_the_threshold_is_split_at_hunk_boundaries_keeping_its_header():
    hunk_body = "".join(f"+    line{i}();\n" for i in range(150))  # ~2,700 chars per hunk
    hunks = [f"@@ -{i * 200},0 +{i * 200},150 @@\n{hunk_body}" for i in range(1, 5)]
    big = _file("src/Big.java", "src/Big.java", *hunks)
    assert estimate_tokens(big) > EMBEDDING_SPLIT_THRESHOLD_TOKENS

    queries = split_queries(big)

    assert len(queries) > 1
    assert [q.part for q in queries] == list(range(1, len(queries) + 1))
    assert all(q.parts == len(queries) for q in queries)
    for query in queries:
        assert query.path == "src/Big.java"
        assert query.text.startswith("diff --git a/src/Big.java b/src/Big.java\n")
        assert "--- a/src/Big.java\n+++ b/src/Big.java\n" in query.text
        assert estimate_tokens(query.text) <= EMBEDDING_SPLIT_THRESHOLD_TOKENS
    # Nothing is lost: every hunk header appears in exactly one part.
    joined = "".join(q.text for q in queries)
    assert all(joined.count(h.splitlines()[0]) == 1 for h in hunks)


def test_a_single_hunk_over_the_threshold_is_split_by_lines():
    body = "".join(f"+    statement{i}();\n" for i in range(600))  # one ~12,000-char hunk
    big = _file("src/Huge.java", "src/Huge.java", f"@@ -1,0 +1,600 @@\n{body}")

    queries = split_queries(big)

    assert len(queries) > 1
    for query in queries:
        assert estimate_tokens(query.text) <= EMBEDDING_SPLIT_THRESHOLD_TOKENS
        assert "@@ -1,0 +1,600 @@" in query.text  # each piece keeps its hunk's header
    assert sum(q.text.count("statement") for q in queries) == 600


def test_text_without_any_file_header_is_one_query_for_the_whole_diff():
    [query] = split_queries("just some text\n")
    assert query.text == "just some text\n" and query.path == ""


# --- changed_lines ------------------------------------------------------------------------


def test_removed_lines_are_changed_and_context_lines_are_not():
    # old side: 10 "int x", 11 "void run() {", 12 "old();" (removed), 13 "}", 14 other
    assert changed_lines(MODIFY) == {"src/A.java": {12}}


def test_a_pure_insertion_marks_both_neighbours_of_the_insertion_point():
    # old side: 20 a(), 21 b(), [insert], 22 c(), 23 d()
    assert changed_lines(INSERT) == {"src/B.java": {21, 22}}


def test_a_new_file_changes_nothing_in_the_index_and_a_deleted_one_changes_every_line():
    assert changed_lines(ADD) == {}
    assert changed_lines(DELETE) == {"src/Gone.java": {1, 2}}


def test_a_zero_context_insertion_hunk_marks_the_line_it_follows_and_the_next():
    zero_context = _file("src/C.java", "src/C.java", "@@ -5,0 +6,2 @@\n+x();\n+y();\n")
    assert changed_lines(zero_context) == {"src/C.java": {5, 6}}


def test_changed_lines_are_keyed_by_old_path_across_files():
    result = changed_lines(MODIFY + INSERT)
    assert set(result) == {"src/A.java", "src/B.java"}


def test_no_newline_marker_is_ignored():
    diff = _file(
        "src/D.java", "src/D.java",
        "@@ -1,2 +1,2 @@\n a\n-b\n\\ No newline at end of file\n+c\n\\ No newline at end of file\n",
    )
    assert changed_lines(diff) == {"src/D.java": {2}}


def test_a_single_line_longer_than_the_threshold_is_cut_by_characters():
    minified = "+" + "x" * 20_000 + "\n"
    big = _file("src/min.js", "src/min.js", f"@@ -1,0 +1,1 @@\n{minified}")

    queries = split_queries(big)

    assert len(queries) > 1
    assert all(estimate_tokens(q.text) <= EMBEDDING_SPLIT_THRESHOLD_TOKENS for q in queries)
    bodies = [q.text.split("@@ -1,0 +1,1 @@\n", 1)[1] for q in queries]
    assert sum(body.count("x") for body in bodies) == 20_000
