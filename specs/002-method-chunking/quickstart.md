# Quickstart: validating method-level chunking

Each step names the spec item it proves. Steps 1–3 need no network or credentials; steps
4–7 run against real AWS and the free-tier APIs.

## 1. Lambda tests (codereview-lambda)

```sh
pip install -e ".[dev]"
ruff check src tests && pytest
```
Expected: green. The new tests cover v1/v2 dispatch and v1 byte-for-byte equality (FR-015),
a reader for any unknown version failing, a v1-only reader ranking a v2 index (FR-029),
per-file queries and hunk splitting (FR-016), overlap exclusion (FR-020), top-8 and the log
lines (FR-018, FR-022), per-attempt packing and the budget skip (FR-023–FR-026), and the
grouped prompt (FR-027).

## 2. Index-script tests (codereview-app)

```sh
pip install -r scripts/requirements-index.txt
python -m unittest discover -s scripts/tests -v
```
Expected: green. The tests cover chunking of a record, a nested class and overloads; the
trivial rule; `type` chunks; blocks; splitting above 1,800 estimated tokens; batch sizing;
retry on 429/503/timeout against a local stub server; no index file written after retries
run out.

## 3. Dry-run chunk counts (codereview-app, no API calls)

```sh
python scripts/build_index.py --dry-run
```
Expected: counts by kind, the largest estimated chunk, and the number split. The counts on
`develop` go into research.md R1 as FR-003's reference.

## 4. Lambda deploy is behaviour-neutral (after merging the lambda PR to develop)

```sh
cd infra && terraform apply
```
Then re-trigger PR #7 (empty commit, as in baseline.md step 2) and run
`measure_review.py pr:7`. Expected: the RAG files and order are identical to the baseline
(TaskControllerTest, TaskServiceTest, TaskService), and the log has a `rag_query` line with
`"index_version": 1`.

## 5. First v2 build (after merging the app PR to develop)

- Before the merge, note the day's value in AI Studio's **daily chart** of
  `gemini-embedding-001` requests (not the rate-limit page, which shows 28-day peaks).
- The merge triggers `index-codebase`. In its log, expect `version 2`, the counts from
  step 3, 1 embedding call (for ~60 chunks) and 0 retries.
- Check the daily chart again. Record in research R4 whether the build added ~1 request
  or ~41 (one per input).
- Check the published object:
  ```sh
  aws s3 cp s3://codereview-artifacts/index/develop/index.json - | python -c "import json,sys; i=json.load(sys.stdin); print(i['version'], i['commit'], len(i['chunks']))"
  ```

## 6. Groq budget probe (research R9)

With the medium tier, review a PR whose estimated prompt is ~4,000 tokens (PR #3 with
context will do). Expected: Groq answers, or returns 413. On 413, set `groq:medium` to
2,500 and record the result in research R9.

## 7. After-measurement

Follow baseline.md's "How to repeat this measurement" unchanged, and write `after.md`.
Expected, against the spec's success criteria: SC-002 (PR #8: 11 queries), SC-005/SC-011
(no attempt over budget), SC-006 (PR #3 ≥ 4/5), SC-007 (0 overlapping chunks), SC-010
(scores taken from the `rag_chunk` log lines).
