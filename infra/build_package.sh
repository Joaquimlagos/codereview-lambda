#!/usr/bin/env bash
# Builds infra/.build/package/: production dependencies (requirements-lambda.txt) plus this
# repo's own src/ code, combined into one directory so a single archive_file zips a
# self-contained deployment artifact — code + dependencies together, no Lambda Layer.
#
# Wheels are pulled for manylinux2014_x86_64 / cp314 explicitly, regardless of the host OS
# running this script (e.g. Windows) — pydantic ships a compiled extension (pydantic-core)
# and a wheel built for the host platform would not load on Lambda's Linux runtime.
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
  --platform manylinux2014_x86_64 --python-version 3.14 --implementation cp --abi cp314 \
  --only-binary=:all: \
  --target "$BUILD_DIR" \
  -r "$INFRA_DIR/requirements-lambda.txt"

cp -r "$INFRA_DIR/../src/." "$BUILD_DIR/"
find "$BUILD_DIR" -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true

echo "Built Lambda package at $BUILD_DIR ($(du -sh "$BUILD_DIR" | cut -f1))"
