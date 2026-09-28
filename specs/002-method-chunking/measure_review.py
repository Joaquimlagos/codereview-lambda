#!/usr/bin/env python3
"""Reconstructs what one PR-review execution actually sent and received.

For each Step Functions execution given, reports:
  - the prompt InvokeLLM built, split into instructions / diff / RAG context, counted with
    two tokenizers (o200k_base as a proxy for gpt-oss on Groq/Cerebras, and Gemini's own
    countTokens for gemini-3.5-flash), next to the provider's own logged prompt_tokens;
  - the diff's size in embedding tokens (gemini-embedding-001 silently truncates at 2,048);
  - the full RAG ranking: cosine similarity of every indexed chunk against the diff, using
    the index *version that was live when the execution started*, and whether the top-3
    reproduces what RetrieveContext actually returned;
  - the model that answered, fallbacks, and every inline comment (category/severity).

Similarity is not logged by RetrieveContext, so it is recomputed: the diff is re-embedded
with the same model, task type and dimensionality. Embeddings are deterministic, so the
recomputed top-3 must match the execution's; the script says so explicitly when it doesn't.

Usage (from codereview-lambda's root, AWS profile with read access to the account):
    AWS_PROFILE=codereview python specs/002-method-chunking/measure_review.py \
        --env .env --out results.json <execution-name-prefix> [...]
Requires: boto3, requests, tiktoken, pydantic (pydantic only for importing build_prompt).
"""

import argparse
import json
import math
import os
import re
import sys
from datetime import timedelta
from pathlib import Path

import boto3
import requests
import tiktoken

REGION = "us-east-1"
STATE_MACHINE = "codereview-pr-review"
INDEX_KEY = "index/develop/index.json"
INVOKE_LLM_LOG_GROUP = "/aws/lambda/codereview-invoke-llm"
EMBEDDING_MODEL = "gemini-embedding-001"
EMBEDDING_TOKEN_LIMIT = 2048
GEMINI_COUNT_MODEL = "gemini-3.5-flash"
DIFF_MARKER = "=== DIFF UNDER REVIEW ===\n"
CONTEXT_MARKER = "\n=== ADDITIONAL PROJECT CONTEXT ===\n"


def load_env(path: Path) -> dict:
    env = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            env[key.strip()] = value.strip()
    return env


def gemini_key_usable(api_base: str, key: str | None) -> bool:
    if not key:
        return False
    return gemini_count(api_base, key, GEMINI_COUNT_MODEL, "ping") is not None


def gemini_count(api_base: str, key: str | None, model: str, text: str) -> int | None:
    if not key:
        return None
    response = requests.post(
        f"{api_base}/models/{model}:countTokens",
        headers={"x-goog-api-key": key},
        json={"contents": [{"parts": [{"text": text}]}]},
        timeout=30,
    )
    if response.status_code != 200:
        return None
    return response.json().get("totalTokens")


def gemini_embed_query(api_base: str, key: str, text: str) -> list[float]:
    response = requests.post(
        f"{api_base}/models/{EMBEDDING_MODEL}:embedContent",
        headers={"x-goog-api-key": key},
        json={
            "model": f"models/{EMBEDDING_MODEL}",
            "content": {"parts": [{"text": text}]},
            "taskType": "RETRIEVAL_QUERY",
            "outputDimensionality": 768,
        },
        timeout=30,
    )
    response.raise_for_status()
    return response.json()["embedding"]["values"]


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return dot / norm if norm else 0.0


def find_execution(sfn, selector: str) -> dict:
    """`pr:<n>` picks the most recent *succeeded* execution for PR n; anything else is taken
    as a prefix of the execution name."""
    arn = next(
        m["stateMachineArn"]
        for m in sfn.list_state_machines()["stateMachines"]
        if m["name"] == STATE_MACHINE
    )
    wanted_pr = int(selector[3:]) if selector.startswith("pr:") else None
    for page in sfn.get_paginator("list_executions").paginate(stateMachineArn=arn):
        for execution in page["executions"]:  # newest first
            if wanted_pr is None:
                if execution["name"].startswith(selector):
                    return execution
                continue
            if execution["status"] != "SUCCEEDED":
                continue
            started = sfn.describe_execution(executionArn=execution["executionArn"])
            payload = json.loads(started["input"])
            if payload.get("detail", payload).get("prNumber") == wanted_pr:
                return execution
    sys.exit(f"No execution of {STATE_MACHINE} matches {selector!r}")


def state_io(sfn, execution_arn: str) -> tuple[dict, dict]:
    entered, exited = {}, {}
    pages = sfn.get_paginator("get_execution_history").paginate(
        executionArn=execution_arn, includeExecutionData=True
    )
    for page in pages:
        for event in page["events"]:
            if details := event.get("stateEnteredEventDetails"):
                entered[details["name"]] = json.loads(details["input"])
            if details := event.get("stateExitedEventDetails"):
                exited[details["name"]] = json.loads(details["output"])
    return entered, exited


def index_version_at(s3, bucket: str, when) -> tuple[dict, str]:
    """The index object version that was current when the execution started."""
    versions = s3.list_object_versions(Bucket=bucket, Prefix=INDEX_KEY).get("Versions", [])
    live = [v for v in versions if v["Key"] == INDEX_KEY and v["LastModified"] <= when]
    if not live:
        sys.exit(f"No version of {INDEX_KEY} predates {when}")
    version = max(live, key=lambda v: v["LastModified"])
    body = s3.get_object(Bucket=bucket, Key=INDEX_KEY, VersionId=version["VersionId"])["Body"]
    return json.loads(body.read()), version["VersionId"]


def invoke_llm_log_lines(logs, start, stop, pr_number: int, model_used: str) -> list[str]:
    """This execution's own InvokeLLM log lines. Executions for different PRs can overlap in
    time, so a time window alone is not enough: a line is kept when it names this PR
    ("failed for PR n") or shares a Lambda request id with one that does. An `answered` line
    carries no PR number, so when no failure ties the request id to this PR, it is matched by
    the label of the model that answered (GeneratedReview.model_used)."""
    kwargs = {
        "logGroupName": INVOKE_LLM_LOG_GROUP,
        "startTime": int(start.timestamp() * 1000),
        "endTime": int((stop + timedelta(seconds=5)).timestamp() * 1000),
    }
    events = []
    for page in logs.get_paginator("filter_log_events").paginate(**kwargs):
        events += [e["message"] for e in page["events"]
                   if "answered" in e["message"] or "failed for PR" in e["message"]]

    def request_id(message: str) -> str | None:
        parts = message.split("	")
        return parts[2] if len(parts) > 2 else None

    ours = {request_id(m) for m in events if f"failed for PR {pr_number} " in m}
    answered_label = model_used.rsplit(":", 1)[0] if model_used.count(":") >= 2 else model_used
    return [
        m for m in events
        if request_id(m) in ours
        or (not ours and f"{answered_label} answered" in m)
    ]


def measure(prefix: str, env: dict, lambda_src: Path) -> dict:
    sys.path.insert(0, str(lambda_src))
    from contracts.models import ContextChunk
    from integrations.llm_router import build_prompt

    session = boto3.Session(region_name=REGION)
    sfn, s3, logs = session.client("stepfunctions"), session.client("s3"), session.client("logs")
    api_base = env.get("GEMINI_API_BASE", "https://generativelanguage.googleapis.com/v1beta")
    key = env.get("GEMINI_API_KEY")
    if not gemini_key_usable(api_base, key):
        print("GEMINI_API_KEY missing or rejected: skipping Gemini token counts and RAG "
              "similarities (recorded as null)", file=sys.stderr)
        key = None
    encoder = tiktoken.get_encoding("o200k_base")

    execution = find_execution(sfn, prefix)
    entered, exited = state_io(sfn, execution["executionArn"])
    event = entered["InvokeLLM"]
    analysis = exited["InvokeLLM"]["analysis"]
    raw_chunks = (event.get("context") or {}).get("chunks", [])
    chunks = [ContextChunk.model_validate(c) for c in raw_chunks]

    diff_text = s3.get_object(Bucket=event["diffBucket"], Key=event["diffKey"])["Body"].read()
    diff_text = diff_text.decode("utf-8")

    # Prompt split. Built with this checkout's build_prompt, so run the script from the same
    # commit that was deployed when the execution ran (baseline.md records it).
    prompt = build_prompt(diff_text, chunks, event.get("paths"))
    head, _, rest = prompt.partition(DIFF_MARKER)
    diff_part, _, context_part = rest.partition(CONTEXT_MARKER)
    context_part = (CONTEXT_MARKER + context_part) if chunks else ""
    instructions = head + DIFF_MARKER
    sections = {"instructions": instructions, "diff": diff_part, "context": context_part}
    tokens = {
        name: {
            "o200k": len(encoder.encode(text)),
            "gemini": gemini_count(api_base, key, GEMINI_COUNT_MODEL, text) if text else 0,
        }
        for name, text in sections.items()
    }
    tokens["total"] = {
        "o200k": len(encoder.encode(prompt)),
        "gemini": gemini_count(api_base, key, GEMINI_COUNT_MODEL, prompt),
    }

    # RAG ranking, recomputed against the index version live at execution start.
    index, index_version = index_version_at(s3, event["diffBucket"], execution["startDate"])
    returned = [c.path for c in chunks]
    if key:
        query = gemini_embed_query(api_base, key, diff_text)
        ranking = sorted(
            ({"path": c["path"], "similarity": round(cosine(c["vector"], query), 4),
              "chars": len(c["text"])} for c in index["chunks"]),
            key=lambda r: r["similarity"],
            reverse=True,
        )
        reproduced = [r["path"] for r in ranking[: len(returned)]] == returned
    else:
        ranking = [{"path": c["path"], "similarity": None, "chars": len(c["text"])}
                   for c in index["chunks"]]
        reproduced = None
    diff_embed_tokens = gemini_count(api_base, key, EMBEDDING_MODEL, diff_text)

    usage_lines = invoke_llm_log_lines(
        logs, execution["startDate"], execution["stopDate"], event["prNumber"],
        analysis["model_used"],
    )
    usage = None
    for line in usage_lines:
        if match := re.search(r"'prompt_tokens': (\d+)", line) or re.search(
            r"'promptTokenCount': (\d+)", line
        ):
            usage = int(match.group(1))

    return {
        "execution": execution["name"],
        "started": execution["startDate"].isoformat(),
        "status": execution["status"],
        "prNumber": event["prNumber"],
        "sha": event["sha"],
        "diffKey": event["diffKey"],
        "complexity": event["routing"]["complexity"],
        "tokens": tokens,
        "provider_prompt_tokens": usage,
        "diff_embedding_tokens": diff_embed_tokens,
        "diff_truncated_for_embedding": (
            None if diff_embed_tokens is None else diff_embed_tokens > EMBEDDING_TOKEN_LIMIT
        ),
        "diff_o200k_tokens": len(encoder.encode(diff_text)),
        "index": {"commit": index["commit"], "generatedAt": index["generatedAt"],
                  "s3VersionId": index_version, "chunks": len(index["chunks"])},
        "rag_returned": returned,
        "rag_reproduced": reproduced,
        "rag_ranking": ranking,
        "model_used": analysis["model_used"],
        "fell_back": analysis["fell_back"],
        "parse_fallback": analysis["parse_fallback"],
        "log_lines": usage_lines,
        "summary": analysis["summary"],
        "comments": analysis["comments"],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "executions", nargs="+",
        help="execution name prefixes, or pr:<n> for the latest succeeded run of PR n",
    )
    parser.add_argument("--env", type=Path, default=Path(".env"),
                        help="dotenv file holding GEMINI_API_KEY (env var wins if set)")
    parser.add_argument("--src", type=Path, default=Path("src"))
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    env = load_env(args.env) if args.env.is_file() else {}
    if os.environ.get("GEMINI_API_KEY"):
        env["GEMINI_API_KEY"] = os.environ["GEMINI_API_KEY"]
    results = [measure(prefix, env, args.src.resolve()) for prefix in args.executions]
    args.out.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
