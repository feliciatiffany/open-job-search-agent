#!/usr/bin/env bash
# Start the job dashboard. First run sets up the Python environment.
set -euo pipefail
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  echo "First run: setting up Python environment…"
  python3 -m venv .venv && .venv/bin/pip install -q duckdb playwright
fi
if [ "${1:-}" = "--refresh" ] || [ ! -f data/jobs.json ]; then
  .venv/bin/python scripts/fetch_jobs.py
fi
exec .venv/bin/python app/server.py
