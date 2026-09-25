#!/usr/bin/env bash
# Builds infra/.build/package/: production dependencies (requirements-lambda.txt) plus this
# repo's own src/ code, combined into one directory so a single archive_file zips a
# self-contained deployment artifact — code + dependencies together, no Lambda Layer.
#
# Wheels are pulled for Linux / cp314 explicitly, regardless of the host OS running this script
# (e.g. Windows) — pydantic (pydantic-core) and cryptography ship compiled extensions, and a
# wheel built for the host platform would not load on Lambda's Linux runtime.
#
# Two --platform values are passed because dependencies do not always agree on one: today's
# wheels are all manylinux2014 (glibc 2.17), but some compiled packages (numpy's cp314 wheels
# were an example) publish only manylinux_2_28 wheels, and would otherwise fail to resolve.
# pip takes the best match per package, and Lambda's python3.14 runtime is Amazon Linux 2023
# (glibc 2.34), so both tags load there.
#
# boto3 is intentionally NOT installed here: the AWS Lambda Python runtime provides it
# preinstalled, so vendoring it would only bloat the zip.
set -euo pipefail

INFRA_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUILD_DIR="$INFRA_DIR/.build/package"
PYTHON_BIN="${PYTHON_BIN:-python3}"

rm -rf "$BUILD_DIR"
mkdir -p "$BUILD_DIR"

"$PYTHON_BIN" -m pip install --no-cache-dir \
  --platform manylinux2014_x86_64 --platform manylinux_2_28_x86_64 \
  --python-version 3.14 --implementation cp --abi cp314 \
  --only-binary=:all: \
  --target "$BUILD_DIR" \
  -r "$INFRA_DIR/requirements-lambda.txt"

cp -r "$INFRA_DIR/../src/." "$BUILD_DIR/"
find "$BUILD_DIR" -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true

echo "Built Lambda package at $BUILD_DIR ($(du -sh "$BUILD_DIR" | cut -f1))"
