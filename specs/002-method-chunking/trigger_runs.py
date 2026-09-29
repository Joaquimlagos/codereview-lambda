#!/usr/bin/env python3
"""Triggers the measurement runs for 002-method-chunking, one review at a time.

For each PR in `--order`: push an empty commit to its branch (no checkout; `git
commit-tree` on the remote branch's tree), wait until that PR's Step Functions execution
finishes, then wait `--gap` seconds before the next one. One review at a time keeps the
fallback provider inside its free-tier request rate: Cerebras allows 5 requests per minute
(research.md R9), and PRs #3 and #8 are answered by it whenever Gemini `:high` returns 503,
which it did on every high-tier baseline run.

Prints one line per finished run: `<n> PR<pr> <commit> <execution-prefix> <status> <start>`.
Feed the execution prefixes to measure_review.py.

Usage (from codereview-lambda's root, AWS profile with Step Functions read access, and git
credentials that can push to codereview-app):
    AWS_PROFILE=codereview python specs/002-method-chunking/trigger_runs.py \\
        --app ../codereview-app --order 3 7 8 3 7 8 3 7 8 --label "after-measurement"
"""

import argparse
import json
import subprocess
import time
from datetime import UTC, datetime

import boto3

STATE_MACHINE = "codereview-pr-review"
BRANCH = {3: "feature/auth-resilience", 7: "test/task-title-validation", 8: "test/projects-module"}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--app", required=True, help="path to a codereview-app clone")
    parser.add_argument("--order", nargs="+", type=int, required=True, choices=sorted(BRANCH))
    parser.add_argument("--label", required=True, help="goes into each commit message")
    parser.add_argument("--gap", type=int, default=60, help="seconds between runs")
    parser.add_argument("--timeout", type=int, default=15 * 60, help="per-run wait, seconds")
    args = parser.parse_args()

    sfn = boto3.Session(region_name="us-east-1").client("stepfunctions")
    arn = next(
        m["stateMachineArn"]
        for m in sfn.list_state_machines()["stateMachines"]
        if m["name"] == STATE_MACHINE
    )

    def git(*git_args: str) -> str:
        return subprocess.run(
            ["git", *git_args], cwd=args.app, check=True, capture_output=True, text=True
        ).stdout.strip()

    def pr_of(execution_arn: str) -> int | None:
        payload = json.loads(sfn.describe_execution(executionArn=execution_arn)["input"])
        return payload.get("detail", payload).get("prNumber")

    def wait_for_run(pr: int, after: datetime) -> dict:
        deadline = time.time() + args.timeout
        while time.time() < deadline:
            for ex in sfn.list_executions(stateMachineArn=arn, maxResults=10)["executions"]:
                if ex["startDate"] > after and pr_of(ex["executionArn"]) == pr:
                    if ex["status"] != "RUNNING":
                        return ex
                    break
            time.sleep(15)
        raise TimeoutError(f"no finished execution for PR {pr} within {args.timeout}s")

    for n, pr in enumerate(args.order, start=1):
        branch = BRANCH[pr]
        git("fetch", "-q", "origin", branch)
        message = (
            f"chore: retrigger AI review ({args.label}, run {n})\n\n"
            "Empty commit: re-runs the review pipeline for specs/002-method-chunking's "
            "measurement."
        )
        tree, parent = f"origin/{branch}^{{tree}}", f"origin/{branch}"
        sha = git("commit-tree", tree, "-p", parent, "-m", message)
        pushed_at = datetime.now(UTC)
        git("push", "-q", "origin", f"{sha}:refs/heads/{branch}")
        ex = wait_for_run(pr, pushed_at)
        print(
            f"{n} PR{pr} {sha[:7]} {ex['name'][:8]} {ex['status']} {ex['startDate'].isoformat()}",
            flush=True,
        )
        if n < len(args.order):
            time.sleep(args.gap)


if __name__ == "__main__":
    main()
