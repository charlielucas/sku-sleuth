$ErrorActionPreference = "Stop"
uv sync --frozen --group dev; if ($LASTEXITCODE -ne 0) { exit 1 }
uv run --frozen ruff check .; if ($LASTEXITCODE -ne 0) { exit 1 }
uv run --frozen ruff format --check .; if ($LASTEXITCODE -ne 0) { exit 1 }
uv run --frozen python -m pytest -q; if ($LASTEXITCODE -ne 0) { exit 1 }
uv run --frozen python scripts/run_pipeline.py --scenario passing --workdir ci_out/passing; if ($LASTEXITCODE -ne 0) { exit 1 }
uv run --frozen python scripts/run_pipeline.py --scenario failing --workdir ci_out/failing; if ($LASTEXITCODE -eq 0) { Write-Error "expected nonzero exit from the failing scenario"; exit 1 }
