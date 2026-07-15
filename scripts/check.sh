#!/usr/bin/env bash
set -euo pipefail
uv sync --frozen --group dev
uv run --frozen ruff check .
uv run --frozen ruff format --check .
uv run --frozen python -m pytest -q
uv run --frozen python scripts/run_pipeline.py --scenario passing --workdir ci_out/passing
if uv run --frozen python scripts/run_pipeline.py --scenario failing --workdir ci_out/failing; then
  echo "expected nonzero exit from the failing scenario" && exit 1
fi
